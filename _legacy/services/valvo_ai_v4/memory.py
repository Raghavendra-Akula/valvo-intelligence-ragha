"""
Valvo AI v4 -- Conversation memory.

- Short-term: last N raw messages (same as v3)
- Long-term: conversation summaries for cross-session context
"""
from __future__ import annotations

import json

from database.database import get_db, close_db


_HISTORY_PREFIX = "valvo-ai-v4"


def _history_scope(page_context: str | None = None) -> str:
    suffix = (page_context or "global").strip() or "global"
    return f"{_HISTORY_PREFIX}:{suffix}"


def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  Short-term history (raw messages)
# ═══════════════════════════════════════════════════════════════════════════

def load_history(page_context: str | None = None, limit: int = 10) -> list[dict]:
    """Load recent chat messages for context."""
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        scope = _history_scope(page_context)

        if uid:
            cur.execute(
                "SELECT role, content FROM chat_messages "
                "WHERE page_context = %s AND (user_id = %s OR user_id IS NULL) "
                "ORDER BY created_at DESC LIMIT %s",
                (scope, uid, limit),
            )
        else:
            cur.execute(
                "SELECT role, content FROM chat_messages "
                "WHERE page_context = %s ORDER BY created_at DESC LIMIT %s",
                (scope, limit),
            )
        rows = cur.fetchall()
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        print(f"[v4-memory] load_history failed: {e}")
        return []
    finally:
        close_db(conn)


def save_message(
    role: str,
    content: str,
    page_context: str | None = None,
    stock_context: str | None = None,
):
    """Persist a message to chat history."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        cur.execute(
            "INSERT INTO chat_messages (role, content, page_context, stock_context, user_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (role, content, _history_scope(page_context), stock_context, uid),
        )
        conn.commit()
    except Exception as e:
        print(f"[v4-memory] save_message failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        close_db(conn)


def clear_history(page_context: str | None = None) -> dict:
    """Clear conversation history."""
    conn = get_db()
    if not conn:
        return {"cleared": False, "error": "database unavailable"}
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        scope = _history_scope(page_context)

        if uid:
            cur.execute(
                "DELETE FROM chat_messages WHERE page_context = %s "
                "AND (user_id = %s OR user_id IS NULL)",
                (scope, uid),
            )
        else:
            cur.execute(
                "DELETE FROM chat_messages WHERE page_context = %s",
                (scope,),
            )
        deleted = cur.rowcount
        conn.commit()
        return {"cleared": True, "deleted": deleted}
    except Exception as exc:
        print(f"[v4-memory] clear_history failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return {"cleared": False, "error": str(exc)}
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  Long-term memory (conversation summaries)
# ═══════════════════════════════════════════════════════════════════════════

def load_memory_context(page_context: str | None = None, max_summaries: int = 5) -> str:
    """
    Load recent conversation summaries for long-term context.
    Returns a formatted string to inject into the system prompt.

    Summaries are stored as chat_messages with role='system_summary'.
    """
    conn = get_db()
    if not conn:
        return ""
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        # Load summaries from ALL page contexts for this user (cross-page awareness)
        if uid:
            cur.execute(
                "SELECT content, page_context, created_at FROM chat_messages "
                "WHERE role = 'system_summary' AND user_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (uid, max_summaries),
            )
        else:
            return ""

        rows = cur.fetchall()
        if not rows:
            return ""

        rows.reverse()
        lines = []
        for r in rows:
            ctx = r["page_context"].replace(f"{_HISTORY_PREFIX}:", "").replace("valvo-ai-v3:", "")
            ts = r["created_at"]
            date_str = ts.strftime("%b %d") if hasattr(ts, "strftime") else str(ts)[:10]
            lines.append(f"- [{date_str}] {r['content']}")

        return "\n".join(lines)
    except Exception as e:
        print(f"[v4-memory] load_memory_context failed: {e}")
        return ""
    finally:
        close_db(conn)


def save_conversation_summary(summary: str, page_context: str | None = None):
    """Save a conversation summary for long-term memory."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        cur.execute(
            "INSERT INTO chat_messages (role, content, page_context, user_id) "
            "VALUES (%s, %s, %s, %s)",
            ("system_summary", summary, _history_scope(page_context), uid),
        )
        conn.commit()
    except Exception as e:
        print(f"[v4-memory] save_summary failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        close_db(conn)


def normalize_history_override(history_override: list[dict] | None) -> list[dict]:
    """Clean up frontend-provided history."""
    normalized = []
    for item in history_override or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        normalized.append({"role": role, "content": text})
    return normalized


# ═══════════════════════════════════════════════════════════════════════════
#  Summary generation (called after meaningful conversations)
# ═══════════════════════════════════════════════════════════════════════════

_SUMMARY_PROMPT = """\
Summarize this trading assistant conversation in 1-2 short lines.
Focus on: what the user asked, key data points found, and any decisions made.
Keep stock symbols, numbers, and FY references. No preamble — just the summary.

User: {user_msg}
Assistant: {assistant_msg}
"""


def generate_and_save_summary(
    user_msg: str,
    assistant_msg: str,
    tool_calls: list[dict],
    page_context: str | None = None,
):
    """
    Generate a conversation summary using Gemini Flash Lite and save it.
    Only called for meaningful exchanges (has tool calls = real data query).
    Runs in a background thread so it doesn't slow down the response.
    """
    if not tool_calls:
        return

    import threading

    def _generate():
        try:
            from .gateway import GeminiFlashGateway, FLASH_LITE_MODEL

            gateway = GeminiFlashGateway()
            if not gateway.available():
                return

            # Truncate to keep it cheap
            user_short = user_msg[:200]
            assistant_short = assistant_msg[:500]
            prompt = _SUMMARY_PROMPT.format(
                user_msg=user_short,
                assistant_msg=assistant_short,
            )

            result = gateway.create_message(
                model_id=FLASH_LITE_MODEL,
                max_tokens=100,
                system="You summarize trading conversations concisely.",
                messages=[{"role": "user", "content": prompt}],
                tools=[],
            )

            summary = (result.text or "").strip()
            if summary and len(summary) > 10:
                save_conversation_summary(summary, page_context)
                print(f"[v4-memory] Summary saved: {summary[:80]}")

        except Exception as e:
            print(f"[v4-memory] Summary generation failed: {e}")

    threading.Thread(target=_generate, daemon=True).start()
