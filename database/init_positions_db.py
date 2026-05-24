"""
Position Management Database — Full Schema
Supports all 9 stages of the Position Manager architecture:
  1. Entry  2. MA Detection  3. Defensive  4. Extensions
  5. Regime  6. Bucket System  7. AI Collaboration  8. Journal  9. Post-Trade
"""
import psycopg2
from dotenv import load_dotenv
import os
from database.database import get_db_port

load_dotenv()


def init_positions_table():
    """Create positions table (safe — won't drop existing data)"""

    print("\n📊 Initializing Positions Table...")
    print("=" * 60)

    try:
        conn = psycopg2.connect(
            host=os.getenv('DB_HOST'),
            database=os.getenv('DB_NAME', 'postgres'),
            user=os.getenv('DB_USER', 'postgres'),
            password=os.getenv('DB_PASSWORD'),
            port=get_db_port(),
            sslmode='require'
        )
        print(f"✅ Connected to: {os.getenv('DB_HOST')}")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return

    cursor = conn.cursor()

    try:
        # ═══════════════════════════════════════════════════
        # MAIN POSITIONS TABLE
        # ═══════════════════════════════════════════════════
        print("\n📝 Creating positions table...")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id SERIAL PRIMARY KEY,

                -- ══ STAGE 1: ENTRY ══
                stock_name TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL,
                quantity INTEGER NOT NULL,
                position_value REAL,
                one_r_value REAL,
                risk_pct REAL DEFAULT 4.0,

                -- Source: from VALVO scoring or manual
                source TEXT DEFAULT 'manual',
                submission_id INTEGER,

                -- ══ STAGE 2: MA DETECTION ══
                ma_followed TEXT,
                ma_grade TEXT,
                shakeout_count INTEGER,
                ma_transition TEXT,
                qualifies_for_trailing BOOLEAN DEFAULT FALSE,
                ma_analysis_json JSONB,

                -- ══ STAGE 3: DEFENSIVE ══
                -- Tracked per daily update, latest status stored here
                defensive_status TEXT DEFAULT 'safe',
                grace_day_used BOOLEAN DEFAULT FALSE,
                last_5ma_price REAL,
                last_10ma_price REAL,
                last_20ma_price REAL,

                -- ══ STAGE 4: EXTENSIONS ══
                current_price REAL,
                -- Perspective 1: Current leg from latest base
                leg_base_price REAL,
                leg_extension_pct REAL,
                -- Perspective 2: Overall from entry
                entry_extension_pct REAL,
                -- Perspective 3: Overall from VALVO reference
                valvo_ref_price REAL,
                valvo_extension_pct REAL,
                -- Current R-multiple
                current_r_multiple REAL,

                -- ══ STAGE 5: MARKET REGIME ══
                market_regime TEXT DEFAULT 'bull',
                regime_source TEXT DEFAULT 'manual',
                -- Regime-derived sell parameters (auto-set based on regime)
                first_sell_zone REAL,
                max_sell_pct REAL,
                min_trail_pct REAL,
                reversal_detect_from REAL,
                daily_sell_from REAL,

                -- ══ STAGE 6: BUCKET SYSTEM ══
                bucket_sold_pct REAL DEFAULT 0,
                bucket_cap REAL DEFAULT 66.0,
                trail_remaining_pct REAL DEFAULT 100.0,
                first_sell_done BOOLEAN DEFAULT FALSE,
                -- Sell history: [{date, pct, price, extension, trigger, r_multiple}]
                sell_history JSONB DEFAULT '[]',

                -- ══ STAGE 7: AI COLLABORATION ══
                -- Last AI analysis results
                last_ai_recommendation TEXT,
                last_ai_mode TEXT,
                ai_conversation_history JSONB DEFAULT '[]',

                -- ══ STAGE 8: DECISION JOURNAL ══
                -- [{date, action, reason, extension, r_multiple, ai_suggestion, bucket_status}]
                journal_entries JSONB DEFAULT '[]',

                -- ══ STAGE 9: POST-TRADE ══
                exit_price REAL,
                exit_date TIMESTAMP,
                total_pnl REAL,
                total_pnl_pct REAL,
                net_cost_of_selling REAL,
                drawdown_saved REAL,
                payoff_ratio REAL,
                post_trade_analysis JSONB,

                -- ══ CHARTS ══
                entry_chart_url TEXT,
                latest_chart_url TEXT,

                -- ══ MARKET DATA ══
                security_id TEXT,

                -- ══ META ══
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        print("   ✅ positions table created")

        # ═══════════════════════════════════════════════════
        # DAILY UPDATES TABLE — one row per daily chart upload
        # ═══════════════════════════════════════════════════
        print("📝 Creating position_daily_updates table...")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS position_daily_updates (
                id SERIAL PRIMARY KEY,
                position_id INTEGER NOT NULL REFERENCES positions(id) ON DELETE CASCADE,

                -- Daily snapshot
                date DATE DEFAULT CURRENT_DATE,
                close_price REAL,
                five_ma_price REAL,
                ten_ma_price REAL,
                twenty_ma_price REAL,

                -- Defensive check result
                defensive_result TEXT,

                -- Extensions at this point
                leg_extension_pct REAL,
                entry_extension_pct REAL,
                valvo_extension_pct REAL,
                r_multiple REAL,

                -- AI analysis for this day
                ai_model TEXT,
                ai_analysis JSONB,
                chart_url TEXT,

                -- Action taken
                action_taken TEXT,
                sell_pct REAL,
                sell_price REAL,
                journal_note TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        print("   ✅ position_daily_updates table created")

        # Enable RLS
        cursor.execute('ALTER TABLE public.positions ENABLE ROW LEVEL SECURITY')
        cursor.execute('ALTER TABLE public.position_daily_updates ENABLE ROW LEVEL SECURITY')
        print("   ✅ RLS enabled on both tables")

        # Migration: add columns if table already existed
        try:
            cursor.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS security_id TEXT")
            cursor.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_10ma_price REAL")
            cursor.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_20ma_price REAL")
            cursor.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS trailing_mode TEXT DEFAULT 'original'")
            print("   ✅ columns ensured (security_id, last_10ma_price, last_20ma_price)")
        except Exception:
            pass

        # Performance indexes for frequent query patterns
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_user_status_created ON positions(user_id, status, created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_security_id ON positions(security_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pdu_position_date ON position_daily_updates(position_id, date DESC)")
        print("   ✅ indexes ensured")

        conn.commit()
        print("\n" + "=" * 60)
        print("✅ Position Management tables ready!")
        print("=" * 60)

    except Exception as e:
        print(f"\n❌ Error: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


# ═══════════════════════════════════════════════════
# REGIME PARAMETER PRESETS
# ═══════════════════════════════════════════════════
REGIME_PARAMS = {
    "bull": {
        "first_sell_zone": 35.0,
        "max_sell_pct": 66.0,
        "min_trail_pct": 33.0,
        "reversal_detect_from": 35.0,
        "daily_sell_from": 60.0,
        "bucket_cap": 66.0,
    },
    "sideways": {
        "first_sell_zone": 35.0,
        "max_sell_pct": 80.0,
        "min_trail_pct": 20.0,
        "reversal_detect_from": 15.0,
        "daily_sell_from": 50.0,
        "bucket_cap": 80.0,
    },
    "grind_down": {
        "first_sell_zone": 30.0,
        "max_sell_pct": 66.0,
        "min_trail_pct": 10.0,
        "reversal_detect_from": 30.0,
        "daily_sell_from": 40.0,
        "bucket_cap": 66.0,
    },
    "sharp_downtrend": {
        "first_sell_zone": 8.0,
        "max_sell_pct": 75.0,
        "min_trail_pct": 25.0,
        "reversal_detect_from": 8.0,
        "daily_sell_from": 15.0,
        "bucket_cap": 75.0,
    },
}


def get_regime_params(regime):
    """Get sell parameters for a given market regime"""
    return REGIME_PARAMS.get(regime, REGIME_PARAMS["bull"])


if __name__ == '__main__':
    init_positions_table()
