from __future__ import annotations

from database.database import get_db


HISTORY_PREFIX = "valvo-ai-v2"


def build_history_scope(page_context: str | None = None) -> str:
    suffix = (page_context or "global").strip() or "global"
    return f"{HISTORY_PREFIX}:{suffix}"


def get_history(limit: int = 12, page_context: str | None = None):
    conn = get_db()
    if not conn:
        return []

    scope = build_history_scope(page_context)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT role, content
            FROM chat_messages
            WHERE page_context = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (scope, limit),
        )
        rows = cur.fetchall()
        rows.reverse()
        return [{"role": row["role"], "content": row["content"]} for row in rows]
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def save_message(role: str, content: str, page_context: str | None = None, stock_context: str | None = None):
    conn = get_db()
    if not conn:
        return

    scope = build_history_scope(page_context)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_messages (role, content, page_context, stock_context)
            VALUES (%s, %s, %s, %s)
            """,
            (role, content, scope, stock_context),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def clear_history(page_context: str | None = None):
    conn = get_db()
    if not conn:
        return {"cleared": False, "error": "database unavailable"}

    scope = build_history_scope(page_context)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM chat_messages WHERE page_context = %s", (scope,))
        deleted = cur.rowcount
        conn.commit()
        return {"cleared": True, "deleted": deleted}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"cleared": False, "error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass
