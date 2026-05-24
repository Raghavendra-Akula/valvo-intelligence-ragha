from __future__ import annotations

from database.database import close_db, get_db


def init_valvo_ai_v2_tables():
    conn = get_db()
    if not conn:
        raise RuntimeError("Database unavailable")

    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS valvo_ai_v2_pending_actions (
                id TEXT PRIMARY KEY,
                action_name TEXT NOT NULL,
                target_table TEXT NOT NULL,
                target_ref TEXT,
                request_text TEXT,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                preview JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '30 minutes'
            )
            """
        )
        cur.execute(
            """
            ALTER TABLE valvo_ai_v2_pending_actions
            ADD COLUMN IF NOT EXISTS user_id UUID
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS valvo_ai_v2_audit_log (
                id SERIAL PRIMARY KEY,
                action_name TEXT NOT NULL,
                target_table TEXT NOT NULL,
                target_ref TEXT,
                request_text TEXT,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                before_state JSONB,
                after_state JSONB,
                outcome TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            ALTER TABLE valvo_ai_v2_audit_log
            ADD COLUMN IF NOT EXISTS user_id UUID
            """
        )
        conn.commit()
    finally:
        close_db(conn)
