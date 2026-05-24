-- ──────────────────────────────────────────────────────────────────────────
-- Stock Daily Summary — PL/pgSQL functions
-- ──────────────────────────────────────────────────────────────────────────
-- Source-of-truth definitions for the functions that maintain
-- public.stock_daily_summary. These run on the database (Supabase pg_cron
-- + triggers on candles_daily) — the Backend Flask app does not duplicate
-- this logic.
--
-- Re-running this file is idempotent (CREATE OR REPLACE FUNCTION).
-- Schedules and trigger bindings live in cron_jobs.sql / candles_triggers.sql.
--
-- Functions defined here:
--   refresh_stock_daily_summary(text)   — heavy daily refresh (3x/day cron)
--   refresh_summary_with_timeout(text)  — wrapper that bumps statement_timeout
--   sync_prev_close_from_candles()      — trigger fn on candles_daily writes
--   sync_live_price_to_dependents()     — trigger fn on stock_daily_summary
--   sync_live_prices()                  — manual prev_close sync helper
--   sample_breadth_intraday()           — every-minute breadth sampler
--   get_stocks_without_candles()        — pipeline backfill helper
-- ──────────────────────────────────────────────────────────────────────────

-- ──────────────────────────────────────────────────────────────────────────
-- refresh_stock_daily_summary
-- ──────────────────────────────────────────────────────────────────────────
-- Scheduled by pg_cron 3x/day (safety 6 AM, morning 9 AM, evening 4 PM IST).
-- Writes every derived column except live_*: those are kept fresh by the
-- candles_daily triggers below. Logs each run to summary_refresh_log.
-- ──────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.refresh_stock_daily_summary(p_source text DEFAULT 'manual'::text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    v_count INTEGER;
    v_log_id BIGINT;
BEGIN
    -- Force 5-minute timeout at SESSION level (false = session, not transaction)
    -- This overrides the database-level 60s even in autocommit mode
    PERFORM set_config('statement_timeout', '300000', false);

    INSERT INTO summary_refresh_log (trigger_source, status)
    VALUES (p_source, 'running')
    RETURNING id INTO v_log_id;

    -- STEP 1: Core metrics (MA, 52W, liquidity 19d, MA50 sum)
    WITH arrayed AS (
        SELECT cd.security_id,
            ARRAY_AGG(cd.close ORDER BY cd.date DESC) as cl,
            ARRAY_AGG(cd.high ORDER BY cd.date DESC) as hi,
            ARRAY_AGG(cd.low ORDER BY cd.date DESC) as lo,
            ARRAY_AGG(cd.volume * cd.close ORDER BY cd.date DESC) as turnover,
            MAX(cd.date) as last_date
        FROM candles_daily cd
        WHERE cd.date >= CURRENT_DATE - 400 AND cd.date < CURRENT_DATE
        GROUP BY cd.security_id
        HAVING array_length(ARRAY_AGG(cd.close ORDER BY cd.date DESC), 1) >= 50
    )
    INSERT INTO stock_daily_summary
        (security_id, symbol, company_name, prev_close, high_52w, low_52w,
         ma50, ma200, ma50_sum, close_50th,
         turnover_19d_sum, turnover_19d_count, liq_cr,
         last_hist_date, computed_at, computed_date)
    SELECT a.security_id, su.symbol, su.company_name,
        a.cl[1],
        (SELECT MAX(v) FROM UNNEST(a.hi[1:252]) v),
        (SELECT MIN(v) FROM UNNEST(a.lo[1:252]) v),
        COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:50]) v), 0),
        COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:200]) v), 0),
        COALESCE((SELECT SUM(v) FROM UNNEST(a.cl[1:50]) v), 0),
        a.cl[50],
        COALESCE((SELECT SUM(v) FROM UNNEST(a.turnover[1:19]) v), 0),
        COALESCE((SELECT COUNT(v) FROM UNNEST(a.turnover[1:19]) v WHERE v > 0), 0)::int,
        ROUND(((SELECT AVG(v) FROM UNNEST(a.turnover[1:20]) v) / 10000000.0)::numeric, 2),
        a.last_date, NOW(), CURRENT_DATE
    FROM arrayed a
    JOIN stock_universe su ON a.security_id = su.security_id
    ON CONFLICT (security_id) DO UPDATE SET
        symbol = EXCLUDED.symbol, company_name = EXCLUDED.company_name,
        prev_close = EXCLUDED.prev_close,
        high_52w = EXCLUDED.high_52w, low_52w = EXCLUDED.low_52w,
        ma50 = EXCLUDED.ma50, ma200 = EXCLUDED.ma200,
        ma50_sum = EXCLUDED.ma50_sum, close_50th = EXCLUDED.close_50th,
        turnover_19d_sum = EXCLUDED.turnover_19d_sum,
        turnover_19d_count = EXCLUDED.turnover_19d_count,
        liq_cr = EXCLUDED.liq_cr,
        last_hist_date = EXCLUDED.last_hist_date,
        computed_at = EXCLUDED.computed_at, computed_date = EXCLUDED.computed_date;

    GET DIAGNOSTICS v_count = ROW_COUNT;

    -- STEP 2: Momentum period closes
    UPDATE stock_daily_summary s SET
        close_5d = p5.close, close_20d = p20.close, close_60d = p60.close,
        close_120d = p120.close, close_252d = p252.close
    FROM
        (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 5 AND date >= CURRENT_DATE - 20 ORDER BY security_id, date DESC) p5,
        (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 20 AND date >= CURRENT_DATE - 35 ORDER BY security_id, date DESC) p20,
        (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 60 AND date >= CURRENT_DATE - 75 ORDER BY security_id, date DESC) p60,
        (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 120 AND date >= CURRENT_DATE - 135 ORDER BY security_id, date DESC) p120,
        (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 252 AND date >= CURRENT_DATE - 267 ORDER BY security_id, date DESC) p252
    WHERE s.security_id = p5.security_id AND s.security_id = p20.security_id
      AND s.security_id = p60.security_id AND s.security_id = p120.security_id
      AND s.security_id = p252.security_id;

    UPDATE stock_daily_summary s SET close_5d = p.close FROM (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 5 AND date >= CURRENT_DATE - 20 ORDER BY security_id, date DESC) p WHERE s.security_id = p.security_id AND s.close_5d IS NULL;
    UPDATE stock_daily_summary s SET close_20d = p.close FROM (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 20 AND date >= CURRENT_DATE - 35 ORDER BY security_id, date DESC) p WHERE s.security_id = p.security_id AND s.close_20d IS NULL;
    UPDATE stock_daily_summary s SET close_60d = p.close FROM (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 60 AND date >= CURRENT_DATE - 75 ORDER BY security_id, date DESC) p WHERE s.security_id = p.security_id AND s.close_60d IS NULL;
    UPDATE stock_daily_summary s SET close_120d = p.close FROM (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 120 AND date >= CURRENT_DATE - 135 ORDER BY security_id, date DESC) p WHERE s.security_id = p.security_id AND s.close_120d IS NULL;
    UPDATE stock_daily_summary s SET close_252d = p.close FROM (SELECT DISTINCT ON (security_id) security_id, close FROM candles_daily WHERE date <= CURRENT_DATE - 252 AND date >= CURRENT_DATE - 267 ORDER BY security_id, date DESC) p WHERE s.security_id = p.security_id AND s.close_252d IS NULL;

    -- STEP 3: 30-day volume stats
    UPDATE stock_daily_summary s SET
        vol_29d_sum = v.vol_sum, vol_29d_count = v.vol_cnt,
        turnover_29d_sum = v.turn_sum, turnover_29d_count = v.turn_cnt
    FROM (
        SELECT security_id,
            COALESCE(SUM(volume), 0) as vol_sum, COUNT(*) FILTER (WHERE volume > 0) as vol_cnt,
            COALESCE(SUM(volume * close), 0) as turn_sum, COUNT(*) FILTER (WHERE volume > 0) as turn_cnt
        FROM candles_daily WHERE date >= CURRENT_DATE - 30 AND date < CURRENT_DATE AND volume > 0
        GROUP BY security_id
    ) v WHERE s.security_id = v.security_id;

    -- STEP 4: Sync is_etf flag
    UPDATE stock_daily_summary s SET is_etf = COALESCE(u.is_etf, false)
    FROM stock_universe u WHERE s.security_id = u.security_id;

    -- STEP 5: IPO metrics
    WITH ipo_base AS (
        SELECT security_id, MIN(date) as first_trade_date,
               COUNT(*) as trading_days, MAX(high) as ath
        FROM candles_daily WHERE volume > 0
        GROUP BY security_id
    )
    UPDATE stock_daily_summary s SET
        first_trade_date = b.first_trade_date,
        trading_days = b.trading_days,
        ath = b.ath
    FROM ipo_base b WHERE s.security_id = b.security_id;

    WITH listing_day AS (
        SELECT DISTINCT ON (security_id)
            security_id, open as l_open, close as l_close, high as l_high, volume as l_vol
        FROM candles_daily WHERE volume > 0
        ORDER BY security_id, date ASC
    )
    UPDATE stock_daily_summary s SET
        listing_open = d.l_open, listing_close = d.l_close,
        listing_high = d.l_high, listing_volume = d.l_vol
    FROM listing_day d WHERE s.security_id = d.security_id;

    -- STEP 6: EMA update (one-day exponential smoothing on prev_close)
    UPDATE stock_daily_summary SET
        ema10  = prev_close * (2.0/11) + CASE WHEN ema10  > 0 THEN ema10  ELSE prev_close END * (1 - 2.0/11),
        ema20  = prev_close * (2.0/21) + CASE WHEN ema20  > 0 THEN ema20  ELSE prev_close END * (1 - 2.0/21),
        ema50  = prev_close * (2.0/51) + CASE WHEN ema50  > 0 THEN ema50  ELSE prev_close END * (1 - 2.0/51),
        ema200 = prev_close * (2.0/201) + CASE WHEN ema200 > 0 THEN ema200 ELSE prev_close END * (1 - 2.0/201)
    WHERE prev_close > 0;

    UPDATE summary_refresh_log
    SET finished_at = NOW(), stocks_refreshed = v_count, status = 'success'
    WHERE id = v_log_id;
    RETURN v_count;

EXCEPTION WHEN OTHERS THEN
    UPDATE summary_refresh_log
    SET finished_at = NOW(), status = 'failed', error_message = SQLERRM
    WHERE id = v_log_id;
    RAISE;
END;
$function$;


-- ──────────────────────────────────────────────────────────────────────────
-- refresh_summary_with_timeout
-- ──────────────────────────────────────────────────────────────────────────
-- Wrapper that escalates statement_timeout for long sessions where the
-- database-level default would otherwise kill the refresh.
-- ──────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.refresh_summary_with_timeout(p_source text DEFAULT 'manual'::text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    v_result INTEGER;
BEGIN
    -- SET via EXECUTE runs at session level, bypasses database-level override
    EXECUTE 'SET statement_timeout = ''300000''';
    SELECT refresh_stock_daily_summary(p_source) INTO v_result;
    -- Reset timeout back to database default after completion
    EXECUTE 'SET statement_timeout = ''60000''';
    RETURN v_result;
END;
$function$;


-- ──────────────────────────────────────────────────────────────────────────
-- sync_prev_close_from_candles
-- ──────────────────────────────────────────────────────────────────────────
-- Statement-level trigger fn on candles_daily INSERT/UPDATE.
-- Mirrors today's OHLV from the latest candles_daily writes into
-- stock_daily_summary.live_*, so the screener can serve ~10s-fresh prices
-- without re-querying the 6.8M-row table.
-- ──────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.sync_prev_close_from_candles()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
BEGIN
    UPDATE stock_daily_summary s
    SET live_close = n.close,
        live_high = n.high,
        live_low = n.low,
        live_volume = n.volume
    FROM new_rows n
    WHERE n.security_id = s.security_id
      AND n.date >= CURRENT_DATE;
    RETURN NULL;
END;
$function$;


-- ──────────────────────────────────────────────────────────────────────────
-- sync_live_price_to_dependents
-- ──────────────────────────────────────────────────────────────────────────
-- Row-level trigger fn on stock_daily_summary AFTER UPDATE OF live_close.
-- Cascades the live price into the two consumer tables that need it for
-- realtime UX:
--   - positions_live (sidecar with current_price + R-multiple per active trade)
--   - fundamentals_overview.current_price
-- Short-circuits if live_close didn't actually change.
-- ──────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.sync_live_price_to_dependents()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  -- Short-circuit if live_close didn't actually change
  IF NEW.live_close IS NOT DISTINCT FROM OLD.live_close THEN
    RETURN NEW;
  END IF;
  IF NEW.live_close IS NULL THEN
    RETURN NEW;
  END IF;

  -- 1. Update positions_live (hot sidecar — narrow row, cheap WAL).
  --    Only active positions; closed trades keep their pinned exit price.
  --    The JOIN to positions is needed to (a) filter by status and
  --    security_id, and (b) compute current_r_multiple from entry/SL
  --    which stay on positions (immutable once the trade opens).
  UPDATE positions_live pl
  SET current_price = NEW.live_close,
      current_r_multiple = CASE
        WHEN p.entry_price IS NOT NULL
         AND p.stop_loss IS NOT NULL
         AND p.entry_price > p.stop_loss
        THEN (NEW.live_close - p.entry_price) / NULLIF(p.entry_price - p.stop_loss, 0)
        ELSE pl.current_r_multiple
      END,
      updated_at = NOW()
  FROM positions p
  WHERE pl.position_id = p.id
    AND p.security_id = NEW.security_id
    AND p.status = 'active';

  -- 2. Update fundamentals_overview
  UPDATE fundamentals_overview fo
  SET current_price = NEW.live_close,
      updated_at = NOW()
  WHERE fo.security_id = NEW.security_id;

  RETURN NEW;
END;
$function$;


-- ──────────────────────────────────────────────────────────────────────────
-- sync_live_prices
-- ──────────────────────────────────────────────────────────────────────────
-- Manual helper to align stock_daily_summary.prev_close with the freshest
-- candles_daily close. Used for one-off corrections; not on any cron.
-- ──────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.sync_live_prices()
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    v_count INTEGER;
    v_latest_date DATE;
BEGIN
    -- Find the most recent trading day with data
    SELECT MAX(date) INTO v_latest_date
    FROM candles_daily
    WHERE date <= CURRENT_DATE;

    IF v_latest_date IS NULL THEN
        RETURN 0;
    END IF;

    -- Update prev_close only where the price actually changed
    UPDATE stock_daily_summary s
    SET prev_close = c.close
    FROM candles_daily c
    WHERE c.security_id = s.security_id
      AND c.date = v_latest_date
      AND s.prev_close IS DISTINCT FROM c.close;

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$function$;


-- ──────────────────────────────────────────────────────────────────────────
-- sample_breadth_intraday
-- ──────────────────────────────────────────────────────────────────────────
-- Cron at every minute (Mon-Fri). Self-guards on time window 09:15-15:30 IST
-- and writes one row per minute to breadth_intraday using the live_close
-- columns kept fresh by sync_prev_close_from_candles.
-- ──────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.sample_breadth_intraday()
 RETURNS void
 LANGUAGE plpgsql
AS $function$
DECLARE
  v_time  TIME;
  v_adv   INTEGER;
  v_dec   INTEGER;
  v_total INTEGER;
BEGIN
  v_time := date_trunc('minute', (NOW() AT TIME ZONE 'Asia/Kolkata')::time);
  IF v_time < '09:15'::time OR v_time > '15:30'::time THEN
    RETURN;
  END IF;
  IF EXTRACT(DOW FROM (NOW() AT TIME ZONE 'Asia/Kolkata')::date) IN (0, 6) THEN
    RETURN;
  END IF;

  SELECT
    COUNT(*) FILTER (WHERE COALESCE(live_close, prev_close) > prev_close),
    COUNT(*) FILTER (WHERE COALESCE(live_close, prev_close) < prev_close),
    COUNT(*)
  INTO v_adv, v_dec, v_total
  FROM stock_daily_summary
  WHERE is_etf = false
    AND prev_close > 0
    AND live_close IS NOT NULL;

  INSERT INTO breadth_intraday (date, time_ist, advances, declines, total)
  VALUES (
    (NOW() AT TIME ZONE 'Asia/Kolkata')::date,
    v_time,
    COALESCE(v_adv, 0),
    COALESCE(v_dec, 0),
    COALESCE(v_total, 0)
  )
  ON CONFLICT (date, time_ist) DO UPDATE SET
    advances   = EXCLUDED.advances,
    declines   = EXCLUDED.declines,
    total      = EXCLUDED.total,
    sampled_at = NOW();
END;
$function$;


-- ──────────────────────────────────────────────────────────────────────────
-- get_stocks_without_candles
-- ──────────────────────────────────────────────────────────────────────────
-- Used by the data-pipeline VM (backfill_new_stocks.py) to find newly
-- onboarded stocks that still need a historical candle backfill.
-- ──────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.get_stocks_without_candles()
 RETURNS TABLE(security_id text, symbol text, nse_series text)
 LANGUAGE sql
 STABLE
AS $function$
  SELECT su.security_id, su.symbol, su.nse_series
  FROM stock_universe su
  LEFT JOIN (
    SELECT DISTINCT cd.security_id FROM candles_daily cd
  ) cd ON su.security_id = cd.security_id
  WHERE su.is_active = TRUE
    AND su.is_etf = FALSE
    AND cd.security_id IS NULL
  ORDER BY su.symbol;
$function$;


-- ──────────────────────────────────────────────────────────────────────────
-- TODO: still-to-export functions
-- ──────────────────────────────────────────────────────────────────────────
-- The following functions exist in Supabase and are referenced by triggers
-- or cron jobs but have not yet been exported to source control. Pull them
-- on the next round-trip with the SQL editor:
--
--   reset_live_columns()                — cron: clears live_* every 9:05 AM IST
--   auto_trail_positions()              — cron: 4x intraday position trailing
--   check_and_trigger_alerts()          — cron: every 5 min during market
--   update_updated_at_column()          — generic BEFORE UPDATE timestamp fn
--   create_positions_live_row()         — trigger fn on positions INSERT
--   trg_journal_resolve_security_id()   — trigger fn on journal_trades
--   trg_positions_resolve_security_id() — trigger fn on positions
-- ──────────────────────────────────────────────────────────────────────────
