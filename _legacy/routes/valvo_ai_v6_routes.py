"""
Valvo AI v6 -- Flask blueprint.
"""
from __future__ import annotations

from flask import Blueprint, Response, g, jsonify, request, stream_with_context
from extensions import limiter

valvo_ai_v6_bp = Blueprint("valvo_ai_v6", __name__)


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


@valvo_ai_v6_bp.route("/api/valvo-ai-v6/query", methods=["POST"])
@limiter.limit("20 per minute")
def query():
    data = request.get_json() or {}
    confirm_pending_action_id = (data.get("confirm_pending_action_id") or "").strip()
    cancel_pending_action_id = (data.get("cancel_pending_action_id") or "").strip()
    if confirm_pending_action_id:
        from services.valvo_ai_v2.actions import confirm_pending_action

        result = confirm_pending_action(confirm_pending_action_id, request_text=(data.get("message") or "").strip())
        status_code = 400 if result.get("error") else 200
        return jsonify(_format_action_response(result, resolved_pending_action_id=confirm_pending_action_id)), status_code

    if cancel_pending_action_id:
        from services.valvo_ai_v2.actions import cancel_pending_action

        result = cancel_pending_action(cancel_pending_action_id)
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

    from services.valvo_ai_v6.engine import ValvoAIV6Engine

    engine = ValvoAIV6Engine()
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


@valvo_ai_v6_bp.route("/api/valvo-ai-v6/stream", methods=["POST"])
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

    from services.valvo_ai_v6.engine import ValvoAIV6Engine

    engine = ValvoAIV6Engine()

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


@valvo_ai_v6_bp.route("/api/valvo-ai-v6/history-list", methods=["GET"])
@limiter.limit("60 per minute")
def history_list():
    """Return conversations grouped by session gaps (>30 min = new conversation)."""
    from database.database import get_db
    from services.valvo_ai_v6.engine import _history_scope

    page_context = (request.args.get("page_context") or "valvo-ai-v6").strip() or "valvo-ai-v6"
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

        # Group into conversations (gap > 30 min = new conversation)
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

        # Build summary list (newest first)
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


@valvo_ai_v6_bp.route("/api/valvo-ai-v6/health", methods=["GET"])
@limiter.limit("60 per minute")
def health():
    from services.valvo_ai_v6.engine import ValvoAIV6Engine

    engine = ValvoAIV6Engine()
    return jsonify(engine.health())


@valvo_ai_v6_bp.route("/api/valvo-ai-v6/history", methods=["DELETE"])
@limiter.limit("15 per minute")
def history_clear():
    page_context = request.args.get("page_context")
    from services.valvo_ai_v6.engine import ValvoAIV6Engine

    engine = ValvoAIV6Engine()
    return jsonify(engine.clear_history(page_context=page_context))
