-- ═══════════════════════════════════════════════════════════
-- JOURNAL TRADES TABLE — Core table for the Trading Journal
-- Run in Supabase SQL Editor
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS journal_trades (
    id SERIAL PRIMARY KEY,
    trade_no INTEGER,
    trade_date DATE,
    symbol TEXT NOT NULL,
    name TEXT,                          -- Full company name
    setup JSONB DEFAULT '[]'::jsonb,    -- Array of setup types
    entry_type TEXT DEFAULT 'BREAKOUT', -- ANTICIPATION or BREAKOUT
    self_rating INTEGER DEFAULT 0,      -- 1-5 star rating
    buy_sell TEXT DEFAULT 'Buy',        -- Buy or Sell

    -- Entry
    entry_price REAL,
    avg_entry REAL,
    sl REAL,                            -- Stop loss price
    initial_qty REAL DEFAULT 0,

    -- Partial exits
    p1_price REAL, p1_qty REAL DEFAULT 0, p1_date DATE, p1_sl REAL,
    p2_price REAL, p2_qty REAL DEFAULT 0, p2_date DATE, p2_sl REAL,
    tsl REAL,                           -- Trailing stop loss

    -- Full exits
    e1_price REAL, e1_qty REAL DEFAULT 0, e1_date DATE,
    e2_price REAL, e2_qty REAL DEFAULT 0, e2_date DATE,
    e3_price REAL, e3_qty REAL DEFAULT 0, e3_date DATE,

    -- Qualitative
    plan_followed TEXT DEFAULT '',       -- Yes / No / Partially
    exit_trigger TEXT DEFAULT '',
    growth_areas TEXT DEFAULT '',
    base_duration TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    chart_image TEXT DEFAULT '',          -- URL to chart screenshot

    -- Meta
    sector TEXT DEFAULT '',
    security_id TEXT DEFAULT '',          -- Security ID for live CMP
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_journal_trades_symbol ON journal_trades(symbol);
CREATE INDEX IF NOT EXISTS idx_journal_trades_date ON journal_trades(trade_date);
CREATE INDEX IF NOT EXISTS idx_journal_trades_no ON journal_trades(trade_no);

-- ═══════════════════════════════════════════════════════════
-- IMPORT from nexus_trades → journal_trades
-- Maps existing fields to new schema
-- ═══════════════════════════════════════════════════════════

INSERT INTO journal_trades (
    trade_no, trade_date, symbol, name, setup, entry_type,
    self_rating, buy_sell, entry_price, avg_entry, sl,
    initial_qty, tsl, plan_followed, exit_trigger,
    growth_areas, base_duration, notes, sector,
    updated_at
)
SELECT
    trade_no,
    trade_date,
    stock_name,
    stock_name,                                              -- name = symbol initially
    CASE
        WHEN setup IS NOT NULL AND setup != '' THEN to_jsonb(ARRAY[setup])
        ELSE '[]'::jsonb
    END,
    COALESCE(entry_type, 'BREAKOUT'),
    COALESCE(rating, 0),
    COALESCE(side, 'Buy'),
    entry_price,
    COALESCE(avg_entry, entry_price),
    stop_loss,
    initial_qty,
    trailing_stop,
    COALESCE(plan_followed, ''),
    COALESCE(exit_trigger, ''),
    COALESCE(growth_areas, ''),
    COALESCE(base_duration, ''),
    COALESCE(notes, ''),
    COALESCE(sector, ''),
    COALESCE(updated_at, CURRENT_TIMESTAMP)
FROM nexus_trades
WHERE NOT EXISTS (
    SELECT 1 FROM journal_trades jt WHERE jt.symbol = nexus_trades.stock_name AND jt.trade_no = nexus_trades.trade_no
)
ORDER BY trade_no;

-- After import, we need to populate exit data from the sells JSONB
-- This will be done via a backend migration script since JSONB parsing is complex
