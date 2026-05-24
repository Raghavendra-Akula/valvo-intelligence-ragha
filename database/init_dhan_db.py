"""
Dhan integration tables.

dhan_tokens         per-admin OAuth-style access token from Dhan partner consent flow
dhan_consent_state  short-lived consentId ↔ user_id map used during the redirect dance
"""
from database.database import get_db, close_db


def init_dhan_tables():
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS dhan_tokens (
                user_id          UUID PRIMARY KEY,
                dhan_client_id   TEXT NOT NULL,
                access_token     TEXT NOT NULL,
                token_type       TEXT DEFAULT 'partner',
                expires_at       TIMESTAMPTZ NOT NULL,
                last_refreshed   TIMESTAMPTZ DEFAULT NOW(),
                created_at       TIMESTAMPTZ DEFAULT NOW(),
                updated_at       TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dhan_tokens_client ON dhan_tokens(dhan_client_id)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS dhan_consent_state (
                consent_id   TEXT PRIMARY KEY,
                user_id      UUID NOT NULL,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                consumed_at  TIMESTAMPTZ
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dhan_consent_user ON dhan_consent_state(user_id)")

        conn.commit()
        print("Dhan tables verified")
    except Exception as e:
        conn.rollback()
        print(f"Dhan tables init: {e}")
    finally:
        close_db(conn)
