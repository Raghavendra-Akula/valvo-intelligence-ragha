-- ════════════════════════════════════════════════════════════════════
-- stock_universe.is_ipo: comprehensive bootstrap (v2)
-- ════════════════════════════════════════════════════════════════════
-- Why this exists
--   v1 (2026_05_11_stock_universe_ipo_flag.sql) restricted the bootstrap
--   from stock_daily_summary to first_trade_date >= CURRENT_DATE - 730.
--   The result: 1,815 legitimately listed pre-2024 stocks (TATATECH 2023,
--   IREDA 2023, AZAD Engineering 2023, DOMS Industries 2023, HAPPYFORGE,
--   INOXINDIA, INDIASHLTR, FEDFINA …) were left at is_ipo=false and never
--   surfaced in IPO Lab. User feedback: "all the NSE IPOs are not visible,
--   it's completely damaged data."
--
-- Insight
--   A stock with 50+ trading days in stock_daily_summary IS by definition
--   a real listing — Dhan's phantom back-fill batches (broker CSV
--   historical adds with <50 trading days of candles) never reach
--   stock_daily_summary in the first place. So we can safely flag EVERY
--   non-ETF active row in sds as is_ipo=true regardless of how old
--   first_trade_date is. The phantoms remain at is_ipo=false (their
--   default) and stay filtered out of /api/ipo/young.
--
--   Verified before applying: the 7 phantom symbols from the 2026-04-21
--   batch (FRONTSP, SINGERIND, MARSONS, BI, COCKERILL, NIMBSPROJ,
--   POWERICA) all have NO row in stock_daily_summary (LEFT JOIN returns
--   first_trade_date=NULL), so the WHERE clause below excludes them.
--
-- What this migration does
--   1. Re-runs the ipo_issues match (already idempotent in v1) — picks
--      up any NEW NSE IPOs since the v1 migration.
--   2. Replaces v1's 730-day sds bootstrap with a full-history sweep.
--      Any non-ETF active stock with a populated first_trade_date in
--      stock_daily_summary gets is_ipo=true and listing_date set.
--   3. Leaves the partial index in place from v1.
--
-- Idempotent — safe to re-run.
-- ════════════════════════════════════════════════════════════════════

-- 1) Re-run ipo_issues match (catches new EQ-series listings since v1)
UPDATE public.stock_universe u
   SET is_ipo = true,
       listing_date = COALESCE(u.listing_date, (i.issue_end_date + INTERVAL '1 day')::date)
  FROM (
      SELECT DISTINCT ON (symbol) symbol, issue_end_date
      FROM public.ipo_issues
      WHERE issue_end_date >= CURRENT_DATE - 120
        AND UPPER(COALESCE(series, '')) IN ('EQ', '')
      ORDER BY symbol, issue_end_date DESC
  ) i
 WHERE u.symbol = i.symbol
   AND u.is_active = true
   AND COALESCE(u.is_etf, false) = false
   AND u.is_ipo = false;

-- 2) Full-history bootstrap from stock_daily_summary
--    Any non-ETF active stock that already has summary data IS a real
--    listing — phantoms (<50 trading days) never enter sds.
--    Note: deliberately NO date cap. Pre-2010 listings get is_ipo=true
--    too — accurate at the column level. The period filter in
--    /api/ipo/young controls the recency window for the UI.
UPDATE public.stock_universe u
   SET is_ipo = true,
       listing_date = COALESCE(u.listing_date, s.first_trade_date)
  FROM public.stock_daily_summary s
 WHERE s.security_id = u.security_id
   AND s.first_trade_date IS NOT NULL
   AND COALESCE(s.is_etf, false) = false
   AND COALESCE(u.is_etf, false) = false
   AND u.is_ipo = false;

-- 3) Index already exists from v1 — re-create only if missing
CREATE INDEX IF NOT EXISTS idx_stock_universe_is_ipo
    ON public.stock_universe (is_ipo)
    WHERE is_ipo = true;
