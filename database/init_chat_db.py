"""
init_chat_db.py — Chat messages table for Valvo AI assistant
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from database.database import get_db_port


def get_db():
    """Connect using same method as rest of the app."""
    try:
        from database.database import get_db as _get_db
        return _get_db()
    except (ImportError, ModuleNotFoundError):
        # Fallback: try DATABASE_URL if available
        db_url = os.environ.get("DATABASE_URL")
        if db_url:
            return psycopg2.connect(db_url, cursor_factory=RealDictCursor)
        # Final fallback: individual vars
        return psycopg2.connect(
            host=os.getenv('DB_HOST'),
            database=os.getenv('DB_NAME', 'postgres'),
            user=os.getenv('DB_USER', 'postgres'),
            password=os.getenv('DB_PASSWORD'),
            port=get_db_port(),
            sslmode='require',
            cursor_factory=RealDictCursor
        )


def init_chat_table():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (
                id SERIAL PRIMARY KEY,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                page_context TEXT DEFAULT NULL,
                stock_context TEXT DEFAULT NULL,
                user_id TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        # Index for fast recent history fetch
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_chat_created ON chat_messages (created_at DESC)
        ''')
        # Index for user-scoped queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_chat_user_id ON chat_messages (user_id)
        ''')
        # Migration: add user_id column if table already existed without it
        try:
            cursor.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS user_id TEXT")
        except Exception:
            pass

        # Telemetry columns for the admin error-tracking view.
        # All nullable so historical rows still load fine. Populated by
        # services/valvo_ai_v3/engine.py on every turn (success + failure).
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS engine TEXT")
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS model_used TEXT")
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS error_type TEXT")
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS error_detail TEXT")

        # Partial index on error rows for fast "show me recent errors" queries.
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_chat_error_type
            ON chat_messages (created_at DESC)
            WHERE error_type IS NOT NULL
        ''')

        conn.commit()
        print("✅ chat_messages table ready")
    except Exception as e:
        print(f"⚠️ Chat table init: {e}")
        conn.rollback()
    finally:
        conn.close()


if __name__ == '__main__':
    init_chat_table()
