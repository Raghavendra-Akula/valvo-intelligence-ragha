"""Multi-watchlist system tables (watchlists + watchlist_items)"""
from database.database import get_db, close_db


def init_multi_watchlist_tables():
    """Create the multi-watchlist tables (watchlists + watchlist_items)."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watchlists (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                pin_slot INT,
                color TEXT DEFAULT '#0A84FF',
                sort_order INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, pin_slot)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watchlist_items (
                id SERIAL PRIMARY KEY,
                watchlist_id INT NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                company_name TEXT,
                security_id TEXT,
                notes TEXT,
                section_name TEXT,
                sort_order INT DEFAULT 0,
                added_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(watchlist_id, symbol)
            )
        ''')
        cursor.execute("ALTER TABLE watchlist_items ADD COLUMN IF NOT EXISTS flagged BOOLEAN DEFAULT FALSE")
        conn.commit()
        print("✅ Multi-watchlist tables ready")
    except Exception as e:
        print(f"⚠️ Multi-watchlist tables init: {e}")
        conn.rollback()
    finally:
        close_db(conn)