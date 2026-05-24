"""
Valvo AI v5 -- Flask blueprint.

Mirrors valvo_ai_v3_routes.py exactly. Five endpoints:
  POST /api/valvo-ai-v5/query          one-shot chat
  POST /api/valvo-ai-v5/stream         SSE streaming chat
  GET  /api/valvo-ai-v5/history-list   grouped conversations
  GET  /api/valvo-ai-v5/health         provider health
  DELETE /api/valvo-ai-v5/history      clear history
"""
from __future__ import annotations

from flask import Blueprint, Response, g, jsonify, request, stream_with_context
from extensions import limiter

valvo_ai_v5_bp = Blueprint("valvo_ai_v5", __name__)


def _format_action_response(result: dict, *, resolved_pending_action_id: str | None = None) -> dict:
    payload = dict(result or {})
    payload["skip_history_reuse"] = True
    if resolved_pending_action_id:
        payload["resolved_pending_action_id"] = resolved_pending_action_id
    if payload.get("error"):
        return payload

    if not payload.get("response"):
        payload["response"] = payload.get("message") or "Action completed."
    return payload


def _persist_action_outcome(result: dict, *, page_context: str | None, was_confirm: bool) -> None:
    """Write a one-line outcome turn into chat_messages so the next AI query
    sees that the user confirmed/cancelled the staged action. Without this,
    follow-ups like "keep existing sl" after a pyramid confirm lose the
    just-happened context (the confirm path bypasses engine.query, so nothing
    else persists it)."""
    from services.valvo_ai_v5.engine import _save_message
    try:
        if result.get("error"):
            msg = f"Action did not run: {result['error']}"
        elif result.get("cancelled"):
            target = (result.get("target_ref") or result.get("after", {}).get("stock_name") or "").strip()
            msg = f"Cancelled the staged action{(' on ' + target) if target else ''}."
        else:
            action = result.get("target_table") or result.get("action_name") or "Action"
            after = result.get("after") or {}
            target = (after.get("stock_name") or result.get("target_ref") or "").strip()
            verb = "Confirmed" if was_confirm else "Ran"
            msg = f"{verb}: {result.get('message') or action}"
            if target and target not in msg:
                msg += f" ({target})"
        _save_message("assistant", msg, page_context=page_context)
    except Exception as exc:
        # Never let history persistence break the action response itself.
        print(f"[v5-routes] outcome persist failed: {exc}")


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/query", methods=["POST"])
@limiter.limit("20 per minute")
def query():
    data = request.get_json() or {}
    confirm_pending_action_id = (data.get("confirm_pending_action_id") or "").strip()
    cancel_pending_action_id = (data.get("cancel_pending_action_id") or "").strip()
    if confirm_pending_action_id:
        from services.valvo_ai_v2.actions import confirm_pending_action

        result = confirm_pending_action(confirm_pending_action_id, request_text=(data.get("message") or "").strip())
        _persist_action_outcome(result, page_context=(data.get("page_context") or None), was_confirm=True)
        status_code = 400 if result.get("error") else 200
        return jsonify(_format_action_response(result, resolved_pending_action_id=confirm_pending_action_id)), status_code

    if cancel_pending_action_id:
        from services.valvo_ai_v2.actions import cancel_pending_action

        result = cancel_pending_action(cancel_pending_action_id)
        _persist_action_outcome(result, page_context=(data.get("page_context") or None), was_confirm=False)
        status_code = 400 if result.get("error") else 200
        return jsonify(_format_action_response(result, resolved_pending_action_id=cancel_pending_action_id)), status_code

    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    page_context = data.get("page_context", "")
    stock_context = data.get("stock_context", "")
    voice = bool(data.get("voice", False))
    model = data.get("model")
    history = data.get("history") if isinstance(data.get("history"), list) else None
    load_history = bool(data.get("load_history", True))

    from services.valvo_ai_v5.engine import ValvoAIV5Engine

    engine = ValvoAIV5Engine()
    result = engine.query(
        message=message,
        page_context=page_context,
        stock_context=stock_context,
        voice=voice,
        model=model,
        load_history=load_history if history is None else False,
        history_override=history,
    )
    status_code = 400 if result.get("error") else 200
    return jsonify(result), status_code


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/stream", methods=["POST"])
@limiter.limit("20 per minute")
def stream():
    """SSE endpoint — streams tool steps in real-time, then final answer."""
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    page_context = data.get("page_context", "")
    stock_context = data.get("stock_context", "")
    voice = bool(data.get("voice", False))
    model = data.get("model")
    history = data.get("history") if isinstance(data.get("history"), list) else None
    load_history = bool(data.get("load_history", True))

    from services.valvo_ai_v5.engine import ValvoAIV5Engine

    engine = ValvoAIV5Engine()

    def generate():
        for event in engine.query_stream(
            message=message,
            page_context=page_context,
            stock_context=stock_context,
            voice=voice,
            model=model,
            load_history=load_history if history is None else False,
            history_override=history,
        ):
            yield event

    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/history-list", methods=["GET"])
@limiter.limit("60 per minute")
def history_list():
    """Return conversations grouped by session gaps (>30 min = new conversation)."""
    from database.database import get_db
    from services.valvo_ai_v5.engine import _history_scope

    page_context = (request.args.get("page_context") or "valvo-ai-v5").strip() or "valvo-ai-v5"
    scope = _history_scope(page_context)

    conn = get_db()
    if not conn:
        return jsonify({"conversations": []})
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, role, content, created_at
            FROM chat_messages
            WHERE page_context = %s AND (user_id = %s OR user_id IS NULL)
            ORDER BY created_at ASC
        """, (scope, g.user_id))
        rows = cur.fetchall()
        if not rows:
            return jsonify({"conversations": []})

        convos = []
        current = []
        last_time = None
        from datetime import timedelta
        for r in rows:
            t = r["created_at"]
            if last_time and t and (t - last_time) > timedelta(minutes=30):
                if current:
                    convos.append(current)
                current = []
            current.append({"id": r["id"], "role": r["role"], "content": r["content"], "time": str(t)})
            last_time = t
        if current:
            convos.append(current)

        result = []
        for msgs in reversed(convos):
            first_user = next((m for m in msgs if m["role"] == "user"), None)
            if not first_user:
                continue
            msg_count = len(msgs)
            result.append({
                "preview": (first_user["content"] or "")[:80],
                "date": first_user["time"][:10] if first_user.get("time") else "",
                "time": first_user["time"][:16] if first_user.get("time") else "",
                "message_count": msg_count,
                "messages": msgs,
            })

        return jsonify({"conversations": result[:20]})
    except Exception:
        import traceback; traceback.print_exc()
        return jsonify({"conversations": [], "error": "Failed to load history"}), 500
    finally:
        try:
            conn.close()
        except:
            pass


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/opener", methods=["GET"])
@limiter.limit("30 per minute")
def opener():
    """Per-user proactive session opener. Called by the frontend when a new
    (empty) conversation starts. Returns the standard query() payload plus
    is_opener=true. Not cached — each call runs fresh against the user's
    current positions / regime / alerts via the user-scoped tool chain."""
    page_context = (request.args.get("page_context") or "valvo-ai-v5").strip()
    from services.valvo_ai_v5.engine import ValvoAIV5Engine

    engine = ValvoAIV5Engine()
    result = engine.generate_opener(page_context=page_context)
    status_code = 400 if (isinstance(result, dict) and result.get("error")) else 200
    return jsonify(result), status_code


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/memory", methods=["GET"])
@limiter.limit("60 per minute")
def memory_get():
    """Inspect what Valvo AI remembers about the caller. Used by the
    Settings / Privacy page (and for debugging)."""
    from services.valvo_ai_v5.memory import load_context

    uid = getattr(g, "user_id", None)
    if not uid:
        return jsonify({"error": "No authenticated user"}), 401
    mem = load_context(uid)
    return jsonify({
        "context":            (mem or {}).get("context") or {},
        "turn_count":         (mem or {}).get("turn_count") or 0,
        "last_extracted_at":  str((mem or {}).get("last_extracted_at")) if mem and mem.get("last_extracted_at") else None,
    })


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/memory/refresh", methods=["POST"])
@limiter.limit("6 per minute")
def memory_refresh():
    """Force an immediate re-extraction regardless of the normal throttle.
    Useful after the user tells the AI something new and wants it locked in."""
    from services.valvo_ai_v5.memory import force_refresh
    from services.valvo_ai_v5.engine import ValvoAIV5Engine

    uid = getattr(g, "user_id", None)
    if not uid:
        return jsonify({"error": "No authenticated user"}), 401
    engine = ValvoAIV5Engine()
    updated = force_refresh(uid, engine.gateway)
    return jsonify({"ok": True, "context": updated})


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/memory", methods=["DELETE"])
@limiter.limit("6 per minute")
def memory_reset():
    """Wipe everything the AI has learned about the user."""
    from services.valvo_ai_v5.memory import reset_context

    uid = getattr(g, "user_id", None)
    if not uid:
        return jsonify({"error": "No authenticated user"}), 401
    ok = reset_context(uid)
    return jsonify({"ok": bool(ok), "cleared": True})


# ───────────────────────────────────────────────────────────────────────────
#  LESSONS — staged → graduated / rejected protocol (services.valvo_ai_v5.lessons)
# ───────────────────────────────────────────────────────────────────────────

@valvo_ai_v5_bp.route("/api/valvo-ai-v5/lessons", methods=["GET"])
@limiter.limit("60 per minute")
def lessons_list():
    """List the user's lessons. Filter by ?status=staged|graduated|rejected.
    Default returns 'graduated' (the active ones) so the settings page can
    show "what the AI has learned about my workflow".
    """
    from database.database import get_db, close_db

    uid = getattr(g, "user_id", None)
    if not uid:
        return jsonify({"error": "No authenticated user"}), 401

    status = (request.args.get("status") or "graduated").strip().lower()
    if status not in ("staged", "graduated", "rejected"):
        return jsonify({"error": "invalid status"}), 400

    conn = get_db()
    if not conn:
        return jsonify({"lessons": []})
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, body, tags, status, source, use_count,
                   last_used_at, created_at, updated_at
            FROM ai_lessons
            WHERE user_id = %s AND status = %s
            ORDER BY updated_at DESC
            LIMIT 200
            """,
            (str(uid), status),
        )
        return jsonify({"lessons": [dict(r) for r in cur.fetchall()]})
    except Exception as exc:
        print(f"[v5-routes] lessons_list failed: {exc}")
        return jsonify({"lessons": [], "error": "Failed to load lessons"}), 500
    finally:
        close_db(conn)


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/lessons/staged", methods=["GET"])
@limiter.limit("60 per minute")
def lessons_staged():
    """Staged-only listing with prior decision history attached. This is the
    review queue — what the dream cycle has proposed since last review.
    Re-staged-after-rejection rows surface their previous pushback so the
    reviewer doesn't argue with themselves."""
    from services.valvo_ai_v5.lessons import list_staged

    uid = getattr(g, "user_id", None)
    if not uid:
        return jsonify({"error": "No authenticated user"}), 401
    return jsonify({"lessons": list_staged(uid)})


def _decide_endpoint(action: str, lesson_id: int):
    """Shared body for graduate / reject / reopen — they all take a rationale
    and call the same decision primitive."""
    from services.valvo_ai_v5 import lessons as lessons_mod

    uid = getattr(g, "user_id", None)
    if not uid:
        return jsonify({"error": "No authenticated user"}), 401

    data = request.get_json() or {}
    rationale = (data.get("rationale") or "").strip()
    if not rationale:
        # Hard contract — the whole point of the protocol is decisions with reasons.
        return jsonify({"error": "rationale is required"}), 400

    fn = {
        "graduate": lessons_mod.graduate,
        "reject":   lessons_mod.reject,
        "reopen":   lessons_mod.reopen,
    }[action]
    ok = fn(lesson_id, uid, rationale, actor_id=uid)
    if not ok:
        return jsonify({"error": f"{action} failed (lesson not found or invalid state)"}), 400
    return jsonify({"ok": True, "lesson_id": lesson_id, "action": action})


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/lessons/<int:lesson_id>/graduate", methods=["POST"])
@limiter.limit("30 per minute")
def lessons_graduate(lesson_id: int):
    return _decide_endpoint("graduate", lesson_id)


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/lessons/<int:lesson_id>/reject", methods=["POST"])
@limiter.limit("30 per minute")
def lessons_reject(lesson_id: int):
    return _decide_endpoint("reject", lesson_id)


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/lessons/<int:lesson_id>/reopen", methods=["POST"])
@limiter.limit("30 per minute")
def lessons_reopen(lesson_id: int):
    return _decide_endpoint("reopen", lesson_id)


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/lessons/dream", methods=["POST"])
@limiter.limit("3 per minute")
def lessons_dream():
    """Manually trigger the dream cycle for the calling user. In production
    this should also be wired to a nightly cron (Cloud Scheduler → this
    endpoint with an admin token). Rate-limited tightly because each call
    fans out to several Flash-Lite generations."""
    from services.valvo_ai_v5.dream import run_cycle
    from services.valvo_ai_v5.engine import ValvoAIV5Engine

    uid = getattr(g, "user_id", None)
    if not uid:
        return jsonify({"error": "No authenticated user"}), 401

    data = request.get_json(silent=True) or {}
    lookback = int(data.get("lookback_days") or 7)
    lookback = max(1, min(lookback, 30))  # clamp 1–30d

    engine = ValvoAIV5Engine()
    report = run_cycle(uid, engine.gateway, lookback_days=lookback)
    return jsonify(report)


# ───────────────────────────────────────────────────────────────────────────
#  HEALTH + HISTORY
# ───────────────────────────────────────────────────────────────────────────

@valvo_ai_v5_bp.route("/api/valvo-ai-v5/health", methods=["GET"])
@limiter.limit("60 per minute")
def health():
    from services.valvo_ai_v5.engine import ValvoAIV5Engine

    engine = ValvoAIV5Engine()
    return jsonify(engine.health())


@valvo_ai_v5_bp.route("/api/valvo-ai-v5/history", methods=["DELETE"])
@limiter.limit("15 per minute")
def history_clear():
    page_context = request.args.get("page_context")
    from services.valvo_ai_v5.engine import ValvoAIV5Engine

    engine = ValvoAIV5Engine()
    return jsonify(engine.clear_history(page_context=page_context))
