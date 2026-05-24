-- Intraday P&L snapshots — one row per cron tick per user during market hours.
-- Populated by POST /api/internal/portfolio-snapshot every ~2 min (Cloud
-- Scheduler). Read by GET /api/positions/intraday-pnl so the Position Manager
-- can render the day's P&L curve when the user clicks the Day's P&L stat card.
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                BIGSERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL,
    ts                TIMESTAMPTZ NOT NULL,
    date_ist          DATE NOT NULL,
    portfolio_value   NUMERIC(20, 2) NOT NULL,
    day_pnl           NUMERIC(20, 2) NOT NULL,
    day_pnl_pct       NUMERIC(10, 4) NOT NULL,
    UNIQUE (user_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_user_date
    ON portfolio_snapshots (user_id, date_ist, ts);
