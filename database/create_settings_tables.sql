-- Ensure user_settings table exists with all required columns
CREATE TABLE IF NOT EXISTS user_settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    display_name TEXT,
    base_capital NUMERIC,
    palette TEXT DEFAULT 'tradingview',
    show_52w BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Ensure the default row exists
INSERT INTO user_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- Ensure market_regime_history table exists
CREATE TABLE IF NOT EXISTS market_regime_history (
    id SERIAL PRIMARY KEY,
    regime TEXT NOT NULL DEFAULT 'bull',
    note TEXT DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Ensure leading_sectors table exists
CREATE TABLE IF NOT EXISTS leading_sectors (
    id SERIAL PRIMARY KEY,
    sectors JSONB DEFAULT '[]',
    regime TEXT DEFAULT 'bull',
    note TEXT DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
