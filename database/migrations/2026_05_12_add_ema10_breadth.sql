-- 2026_05_12_add_ema10_breadth.sql
-- Adds EMA10 plumbing for the Index Breadth Dashboard (Nifty Smallcap 100 view).
-- Both columns are nullable additive ALTERs — instant on Postgres, no rewrite.
-- Safe to re-run.

ALTER TABLE stock_daily_summary
    ADD COLUMN IF NOT EXISTS ema10 DOUBLE PRECISION;

ALTER TABLE breadth_daily_history
    ADD COLUMN IF NOT EXISTS pct_above_ema10 NUMERIC(5,1);

COMMENT ON COLUMN stock_daily_summary.ema10
    IS 'Daily EMA(10) of close. Seeded once via Backend/scripts/seed_ema10.py; smoothed in summary_functions.sql STEP 6.';

COMMENT ON COLUMN breadth_daily_history.pct_above_ema10
    IS '% of non-ETF NSE stocks trading above their EMA(10). Backfilled by Backend/scripts/backfill_pct_above_ema10.py; updated by _gap_fill_breadth in breadth_routes.py.';
