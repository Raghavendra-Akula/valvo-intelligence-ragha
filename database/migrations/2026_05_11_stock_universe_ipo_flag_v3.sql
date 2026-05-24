-- ════════════════════════════════════════════════════════════════════
-- stock_universe.is_ipo: fresh-IPO bootstrap from candles_daily (v3)
-- ════════════════════════════════════════════════════════════════════
-- Why this exists
--   v2 flagged every stock already in stock_daily_summary. Problem: sds
--   has a HAVING COUNT(*) >= 50 trading-day threshold, so any stock
--   listed in the last ~70 calendar days is NOT in sds yet. The /api/ipo/young
--   Query B path (Fresh IPOs <50 days) only returns stocks already
--   flagged is_ipo=true, so freshly listed stocks were never surfaced.
--
--   User feedback: "so many ipos under 90 days are not visible why?"
--   Investigation: 25-28 active non-ETF stocks first traded in last 90d
--   but is_ipo=false / listing_date=NULL (SEDEMAC, ARIS, CLEANMAX,
--   AARNAV, PNGSREVA, INNOVISION, BCPL, GSPCROP, RSL, AARNAV…).
--
-- Approach: candles_daily-driven bootstrap with phantom batch filter
--   1. Compute MIN(date) per security_id from candles_daily (true
--      first-trade date in our data, including stocks not in sds).
--   2. Identify Dhan phantom back-fill batches: any debut date where
--      >10 distinct stocks "first traded" the same day. These are
--      historical CSV adds, not real listings. Excluded.
--   3. Require cnt >= 5 candles (filters one-off back-fills).
--   4. Window: last 365 calendar days.
--
-- Trade-off (acknowledged)
--   Some BSE-established companies whose historical candles Dhan
--   recently started providing will be flagged as IPOs (MAFATIND,
--   NIMBSPROJ, POWERICA, SINGERIND, MARSONS, CMPDI, AMIRCHAND,
--   TCIFINANCE, SVPGLOB, INA-Insolation, OMNI-Omnitech, ...). They
--   pass all data-side heuristics. Distinguishing them from real new
--   SME IPOs requires the NSE IPO calendar (planned follow-up:
--   historical ingestion into ipo_issues, replacing the data-shape
--   heuristic with a definitive listing-date source).
--
--   This is acceptable in the short term because:
--     a) User's complaint is "too few IPOs visible," not "false ones."
--     b) is_ipo can be flipped back per-symbol once the calendar
--        scraper lands.
--     c) Real recent SME IPOs (SEDEMAC, ARIS, etc.) become visible.
--
-- Idempotent — safe to re-run.
-- ════════════════════════════════════════════════════════════════════

-- ───── 1) Bootstrap is_ipo=true from candles_daily ─────
WITH first_dates AS (
    SELECT security_id,
           MIN(date) AS first_candle,
           COUNT(*)  AS cnt
      FROM public.candles_daily
     GROUP BY security_id
),
phantom_batch_dates AS (
    SELECT first_candle
      FROM first_dates
     GROUP BY first_candle
    HAVING COUNT(*) > 10
)
UPDATE public.stock_universe u
   SET is_ipo = true,
       listing_date = COALESCE(u.listing_date, f.first_candle)
  FROM first_dates f
 WHERE u.security_id = f.security_id
   AND u.is_active = true
   AND COALESCE(u.is_etf, false) = false
   AND COALESCE(u.is_ipo, false) = false
   AND f.first_candle >= CURRENT_DATE - 365
   AND f.first_candle NOT IN (SELECT first_candle FROM phantom_batch_dates)
   AND f.cnt >= 5
   AND u.symbol NOT IN (
       -- ── BSE-migration exclusion list ──
       -- These are publicly-verifiable established BSE-listed companies
       -- whose appearance in candles_daily reflects Dhan starting to fetch
       -- their NSE-side data, NOT a fresh IPO event. Manually curated.
       -- TODO: replace this list with an ipo_issues lookup once the NSE
       -- IPO calendar is ingested historically.
       'MAFATIND',     -- Mafatlal Industries (founded 1905, BSE 500264)
       'SINGERIND',    -- Singer India (BSE 505729)
       'TCIFINANCE',   -- TCI Finance (BSE 501391)
       'SVPGLOB',      -- SVP Global Ventures (BSE 530721)
       'NIMBSPROJ',    -- Nimbus Projects (BSE 511714, listed 1995)
       'MARSONS'       -- Marsons (BSE 500306)
   );

-- ───── 2) Defensive re-unflag for already-applied runs ─────
--   If an earlier run of v3 (before the exclusion list was added) flagged
--   any of these as IPOs, unflag them here. Idempotent.
UPDATE public.stock_universe
   SET is_ipo = false,
       listing_date = NULL
 WHERE symbol IN ('MAFATIND', 'SINGERIND', 'TCIFINANCE', 'SVPGLOB',
                  'NIMBSPROJ', 'MARSONS')
   AND is_ipo = true;
