"""
Valvo AI v4 -- API routes.

/api/valvo-ai-v4/stream   (POST)  — SSE streaming query
/api/valvo-ai-v4/query    (POST)  — Non-streaming fallback
/api/valvo-ai-v4/health   (GET)   — Health check
/api/valvo-ai-v4/history  (DELETE) — Clear history
/api/valvo-ai-v4/history-list (GET) — Conversation history
"""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request, stream_with_context

from services.valvo_ai_v4.engine import ValvoAIV4Engine


valvo_ai_v4_bp = Blueprint("valvo_ai_v4", __name__)

_engine = None


def _get_engine() -> ValvoAIV4Engine:
    global _engine
    if _engine is None:
        _engine = ValvoAIV4Engine()
    return _engine


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/valvo-ai-v4/query — Non-streaming
# ═══════════════════════════════════════════════════════════════════════════

@valvo_ai_v4_bp.route("/api/valvo-ai-v4/query", methods=["POST"])
def query():
    engine = _get_engine()
    data = request.get_json(silent=True) or {}

    # Handle confirmation/cancellation
    confirm_id = data.get("confirm_pending_action_id")
    cancel_id = data.get("cancel_pending_action_id")
    if confirm_id:
        from services.valvo_ai_v2.actions import confirm_pending_action
        from services.valvo_ai_v2.utils import to_jsonable
        return jsonify(to_jsonable(confirm_pending_action(confirm_id)))
    if cancel_id:
        from services.valvo_ai_v2.actions import cancel_pending_action
        from services.valvo_ai_v2.utils import to_jsonable
        return jsonify(to_jsonable(cancel_pending_action(cancel_id)))

    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    result = engine.query(
        message=message,
        page_context=data.get("page_context", ""),
        stock_context=data.get("stock_context", ""),
        voice=data.get("voice", False),
        load_history_flag=data.get("load_history", True),
        persist_history=True,
        history_override=data.get("history"),
    )
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/valvo-ai-v4/stream — SSE streaming
# ═══════════════════════════════════════════════════════════════════════════

@valvo_ai_v4_bp.route("/api/valvo-ai-v4/stream", methods=["POST"])
def stream():
    engine = _get_engine()
    data = request.get_json(silent=True) or {}

    # Handle confirmation/cancellation inline
    confirm_id = data.get("confirm_pending_action_id")
    cancel_id = data.get("cancel_pending_action_id")
    if confirm_id:
        from services.valvo_ai_v2.actions import confirm_pending_action
        from services.valvo_ai_v2.utils import to_jsonable
        return jsonify(to_jsonable(confirm_pending_action(confirm_id)))
    if cancel_id:
        from services.valvo_ai_v2.actions import cancel_pending_action
        from services.valvo_ai_v2.utils import to_jsonable
        return jsonify(to_jsonable(cancel_pending_action(cancel_id)))

    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    def generate():
        yield from engine.query_stream(
            message=message,
            page_context=data.get("page_context", ""),
            stock_context=data.get("stock_context", ""),
            voice=data.get("voice", False),
            load_history_flag=data.get("load_history", True),
            persist_history=True,
            history_override=data.get("history"),
        )

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════════════════
#  GET /api/valvo-ai-v4/health
# ═══════════════════════════════════════════════════════════════════════════

@valvo_ai_v4_bp.route("/api/valvo-ai-v4/health", methods=["GET"])
def health():
    engine = _get_engine()
    return jsonify(engine.health())


# ═══════════════════════════════════════════════════════════════════════════
#  POST /api/valvo-ai-v4/reindex-stocks — trigger embedding for new stocks
#  Called by the daily universe_sync.py pipeline after new stocks are added.
#  Protected by X-Cron-Secret header (matches pattern used by other cron jobs).
# ═══════════════════════════════════════════════════════════════════════════

@valvo_ai_v4_bp.route("/api/valvo-ai-v4/reindex-stocks", methods=["POST"])
def reindex_stocks():
    """
    Trigger embedding for new stocks. Called by universe_sync.py pipeline.
    Auth: X-Cron-Secret header must match CRON_SECRET env var.
    """
    import os
    from services.valvo_ai_v4.stock_embeddings import ensure_schema, populate_missing_embeddings

    cron_secret = os.getenv("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret", "")
    if not cron_secret or provided != cron_secret:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        ensure_schema()
        count = populate_missing_embeddings()
        return jsonify({
            "status": "ok",
            "embedded": count,
            "message": f"Embedded {count} new stocks" if count else "No new stocks to embed",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
#  DELETE /api/valvo-ai-v4/history
# ═══════════════════════════════════════════════════════════════════════════

@valvo_ai_v4_bp.route("/api/valvo-ai-v4/history", methods=["DELETE"])
def delete_history():
    engine = _get_engine()
    page_context = request.args.get("page_context", "")
    return jsonify(engine.clear_history(page_context))


# ═══════════════════════════════════════════════════════════════════════════
#  GET /api/valvo-ai-v4/history-list — Conversation history sidebar
# ═══════════════════════════════════════════════════════════════════════════

@valvo_ai_v4_bp.route("/api/valvo-ai-v4/history-list", methods=["GET"])
def history_list():
    """List conversation threads grouped by 30-min session gaps."""
    from database.database import get_db, close_db
    from flask import g
    from datetime import timedelta

    uid = getattr(g, "user_id", None)
    page_context = request.args.get("page_context", "floating-v3")
    scope = f"valvo-ai-v4:{page_context}"

    conn = get_db()
    if not conn:
        return jsonify({"conversations": []})

    try:
        cur = conn.cursor()
        if uid:
            cur.execute(
                "SELECT role, content, created_at FROM chat_messages "
                "WHERE page_context = %s AND (user_id = %s OR user_id IS NULL) "
                "AND role != 'system_summary' "
                "ORDER BY created_at DESC LIMIT 200",
                (scope, uid),
            )
        else:
            cur.execute(
                "SELECT role, content, created_at FROM chat_messages "
                "WHERE page_context = %s AND role != 'system_summary' "
                "ORDER BY created_at DESC LIMIT 200",
                (scope,),
            )
        rows = cur.fetchall()
        if not rows:
            return jsonify({"conversations": []})

        # Group by 30-min gaps
        conversations = []
        current_group: list = []
        prev_ts = None

        for row in rows:
            ts = row["created_at"]
            if prev_ts and (prev_ts - ts).total_seconds() > 1800:
                if current_group:
                    conversations.append(_summarize_group(current_group))
                current_group = []
            current_group.append(row)
            prev_ts = ts

        if current_group:
            conversations.append(_summarize_group(current_group))

        return jsonify({"conversations": conversations[:20]})
    except Exception as e:
        print(f"[v4-routes] history_list failed: {e}")
        return jsonify({"conversations": []})
    finally:
        close_db(conn)


def _summarize_group(messages: list) -> dict:
    """Summarize a conversation group for the sidebar."""
    # Messages are in DESC order, find the first user message
    user_msgs = [m for m in messages if m["role"] == "user"]
    preview = user_msgs[-1]["content"][:80] if user_msgs else "Conversation"
    latest = messages[0]["created_at"]
    return {
        "preview": preview,
        "message_count": len(messages),
        "date": latest.isoformat() if hasattr(latest, "isoformat") else str(latest),
    }
