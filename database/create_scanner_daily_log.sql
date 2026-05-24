-- Scanner Daily Log — tracks how many stocks hit the screener each day
CREATE TABLE IF NOT EXISTS scanner_daily_log (
    id SERIAL PRIMARY KEY,
    scan_date DATE NOT NULL UNIQUE,
    stock_count INTEGER NOT NULL DEFAULT 0,
    gainers INTEGER DEFAULT 0,
    losers INTEGER DEFAULT 0,
    avg_change NUMERIC(6,2) DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scanner_daily_log_date ON scanner_daily_log(scan_date DESC);
