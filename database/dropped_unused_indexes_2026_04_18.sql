-- ═══════════════════════════════════════════════════════════════════════════
-- Dropped unused indexes (audit: 2026-04-18)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Each of these had idx_scan = 0 in pg_stat_user_indexes and was > 1 MB.
-- Total storage reclaimed: ~408 MB.
-- Write-amplification on the parent tables also goes down since every
-- INSERT/UPDATE no longer has to maintain these unused B-trees.
--
-- If a query ever needs one of these back, re-create from the definitions
-- below. All definitions are idempotent (IF NOT EXISTS).
-- ═══════════════════════════════════════════════════════════════════════════

-- ───────────────────────────────────────────────────────────────────────────
-- TIER 1 — Big wins (redundant covering-index pairs)
-- ───────────────────────────────────────────────────────────────────────────

-- candles_daily: duplicate of idx_candles_daily_sid_date_covering (same
-- leading columns, the covering version also INCLUDEs OHLCV so planner
-- always picks it for this shape of query).
-- DROP: 369 MB
CREATE INDEX IF NOT EXISTS idx_candles_daily_lookup
  ON public.candles_daily USING btree (security_id, date DESC);

-- candles_indices: duplicate of idx_candles_indices_sym_date_covering.
-- DROP: 12 MB
CREATE INDEX IF NOT EXISTS idx_candles_indices_symbol
  ON public.candles_indices USING btree (symbol, date DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- TIER 2 — filings: code always filters by security_id+filing_date via
--          idx_filings_security_date. symbol / bse_code / period are
--          output columns, never lookup keys.
-- ───────────────────────────────────────────────────────────────────────────

-- DROP: 4.9 MB
CREATE INDEX IF NOT EXISTS idx_filings_symbol   ON public.filings USING btree (symbol);
-- DROP: 4.5 MB
CREATE INDEX IF NOT EXISTS idx_filings_bse_code ON public.filings USING btree (bse_code);
-- DROP: 3.8 MB
CREATE INDEX IF NOT EXISTS idx_filings_period   ON public.filings USING btree (period);


-- ───────────────────────────────────────────────────────────────────────────
-- TIER 3 — fundamentals tables: v1 and v2 shareholding / financials /
--          segments. The code joins by (symbol, period_end_date), which
--          is already served by the UNIQUE constraint's index
--          (uq_*_symbol_period). isin_period and security_period variants
--          were added speculatively and never got traffic.
-- ───────────────────────────────────────────────────────────────────────────

-- DROP: 2.4 MB
CREATE INDEX IF NOT EXISTS idx_fin_quarterly_v2_isin_period
  ON public.financials_quarterly_v2 USING btree (isin, period_end_date);

-- DROP: 2.3 MB
CREATE INDEX IF NOT EXISTS idx_shareholding_quarterly_symbol
  ON public.shareholding_quarterly USING btree (symbol, period_end_date);

-- DROP: 2.0 MB
CREATE INDEX IF NOT EXISTS idx_financials_quarterly_consolidated
  ON public.financials_quarterly USING btree (symbol, period_end_date, is_consolidated);

-- DROP: 1.9 MB
CREATE INDEX IF NOT EXISTS idx_fin_quarterly_v2_security_period
  ON public.financials_quarterly_v2 USING btree (security_id, period_end_date DESC);

-- DROP: 1.9 MB
CREATE INDEX IF NOT EXISTS idx_shareholding_v2_isin_period
  ON public.shareholding_quarterly_v2 USING btree (isin, period_end_date);

-- DROP: 1.7 MB
CREATE INDEX IF NOT EXISTS idx_segments_quarterly_v2_isin_period
  ON public.segments_quarterly_v2 USING btree (isin, period_end_date);

-- DROP: 1.6 MB
CREATE INDEX IF NOT EXISTS idx_shareholding_v2_security_period
  ON public.shareholding_quarterly_v2 USING btree (security_id, period_end_date DESC);


-- ═══════════════════════════════════════════════════════════════════════════
-- ACTUAL DROP STATEMENTS (what the migration ran)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- DROP INDEX IF EXISTS public.idx_candles_daily_lookup;
-- DROP INDEX IF EXISTS public.idx_candles_indices_symbol;
-- DROP INDEX IF EXISTS public.idx_filings_symbol;
-- DROP INDEX IF EXISTS public.idx_filings_bse_code;
-- DROP INDEX IF EXISTS public.idx_filings_period;
-- DROP INDEX IF EXISTS public.idx_fin_quarterly_v2_isin_period;
-- DROP INDEX IF EXISTS public.idx_shareholding_quarterly_symbol;
-- DROP INDEX IF EXISTS public.idx_financials_quarterly_consolidated;
-- DROP INDEX IF EXISTS public.idx_fin_quarterly_v2_security_period;
-- DROP INDEX IF EXISTS public.idx_shareholding_v2_isin_period;
-- DROP INDEX IF EXISTS public.idx_segments_quarterly_v2_isin_period;
-- DROP INDEX IF EXISTS public.idx_shareholding_v2_security_period;
