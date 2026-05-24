-- ════════════════════════════════════════════════════════════════════
-- stock_universe.is_ipo: precise flag for genuine recent listings
-- ════════════════════════════════════════════════════════════════════
-- Problem
--   Dhan's daily instrument CSV periodically adds historical equities
--   that the broker had not previously tracked (e.g. 47 phantom adds on
--   2026-04-21: Frontier Springs/1985, Singer India, Bilcare,
--   John Cockerill, Marsons, Powerica, Nimbus Projects, ...). With only
--   a 280-day Dhan back-fill these look identical to genuine fresh IPOs
--   in stock_daily_summary: first candle is recent, trading_days < 50.
--   IPO Lab's "Fresh IPOs" view leaked all of them as "Apr 2026 IPOs".
--   A coarse 4-stock-same-date heuristic (commit 79ce2025) only catches
--   bulk batches; smaller residual phantoms still slip through.
--
-- Fix
--   The authoritative IPO feed is `ipo_issues` (NSE all-upcoming +
--   currently-bidding endpoints, populated daily by Feed 5 in
--   run_daily_sync.py). It is upsert-only — closed/listed rows persist
--   indefinitely — so a symbol-level match cleanly separates real IPOs
--   from broker-CSV phantoms.
--
--   Stamp `stock_universe.is_ipo = true` ONLY for stocks whose symbol
--   matches a recent `ipo_issues` EQ-series row. IPO Lab Query B then
--   filters by `is_ipo = true`, which eliminates phantoms entirely
--   without depending on bulk-batch heuristics.
--
-- This migration
--   1. Adds `is_ipo BOOLEAN NOT NULL DEFAULT false` and
--      `listing_date DATE` (nullable) to `stock_universe`.
--      Defaults mean every existing row starts as is_ipo=false — the
--      phantoms are filtered out automatically.
--   2. Back-fills is_ipo=true (+ listing_date = issue_end_date + 1d)
--      for stocks whose symbol matches an `ipo_issues` EQ-series row
--      with issue_end_date in the last 120 days. This re-flags KISSHT
--      (real 2026-05-08 IPO) and any other genuine recent listing
--      already present in stock_universe.
--   3. Adds a partial index on `(is_ipo) WHERE is_ipo = true` —
--      Query B's filter touches only ~1-2 dozen rows at any time.
--
-- Idempotent — safe to re-run.
-- ════════════════════════════════════════════════════════════════════

-- 1) Columns
ALTER TABLE public.stock_universe
    ADD COLUMN IF NOT EXISTS is_ipo BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE public.stock_universe
    ADD COLUMN IF NOT EXISTS listing_date DATE;

-- 2) Backfill from ipo_issues (EQ-series mainboard only, last 120 days)
--    SME / RR (REIT) / IV (InvIT) series are tracked separately and not
--    in this NSE_EQ universe — restrict the match so a stray symbol
--    collision across series cannot stamp the wrong row.
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

-- 3) Backfill from stock_daily_summary (historical-IPO bootstrap)
--    `ipo_issues` was only added in May 2026 — for any IPO listed before
--    Feed 5 started running we have no NSE source to match against. So
--    we trust the existing summary: any stock that ALREADY has 50+
--    trading days of history with a first_trade_date in the last 2
--    years is, by definition, a real listing — the phantom batches
--    (Dhan back-fills of historical equities) are all <50 trading days
--    today and therefore not yet in stock_daily_summary.
--
--    This bootstrap runs ONCE; future IPOs are stamped at ingestion by
--    Pipeline/ipo_check.py via the ipo_issues match. Inactive (delisted)
--    rows are flagged too — they were real IPOs at the time of listing.
UPDATE public.stock_universe u
   SET is_ipo = true,
       listing_date = COALESCE(u.listing_date, s.first_trade_date)
  FROM public.stock_daily_summary s
 WHERE s.security_id = u.security_id
   AND s.first_trade_date IS NOT NULL
   AND s.first_trade_date >= CURRENT_DATE - 730
   AND COALESCE(s.is_etf, false) = false
   AND COALESCE(u.is_etf, false) = false
   AND u.is_ipo = false;

-- 4) Partial index for IPO Lab Query A + Query B
CREATE INDEX IF NOT EXISTS idx_stock_universe_is_ipo
    ON public.stock_universe (is_ipo)
    WHERE is_ipo = true;
