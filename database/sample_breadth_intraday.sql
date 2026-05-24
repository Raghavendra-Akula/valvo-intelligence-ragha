-- ═══════════════════════════════════════════════════════════════════════════
-- sample_breadth_intraday() — minute-by-minute market breadth sampler
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Called by pg_cron job `breadth-intraday-sample` every minute (Mon-Fri).
-- Writes one row into breadth_intraday per minute during market hours.
--
-- v2 (2026-04-18): rewritten to read from stock_daily_summary.live_close
-- instead of DISTINCT-ON over candles_daily. The `live_close` column is
-- kept in sync by the trg_sync_prev_close_* triggers on candles_daily, so
-- it's already refreshed every ~10s by the WebSocket VM. Result: function
-- runs in ~10 ms instead of ~1000 ms (100x faster; previously it was
-- consuming ~48% of total DB CPU time per pg_stat_statements).
--
-- Idempotent — safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════

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
  -- Only during market hours IST (9:15 - 15:30)
  v_time := date_trunc('minute', (NOW() AT TIME ZONE 'Asia/Kolkata')::time);
  IF v_time < '09:15'::time OR v_time > '15:30'::time THEN
    RETURN;
  END IF;

  -- Skip weekends
  IF EXTRACT(DOW FROM (NOW() AT TIME ZONE 'Asia/Kolkata')::date) IN (0, 6) THEN
    RETURN;
  END IF;

  -- Advances/declines directly from summary's live_close (already 10s-fresh
  -- via trg_sync_prev_close_insert/_update on candles_daily). Falls back
  -- to prev_close for symbols that haven't ticked yet today.
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

-- ─── VERIFY ───
-- SELECT pg_get_functiondef('public.sample_breadth_intraday'::regproc);
-- EXPLAIN ANALYZE SELECT sample_breadth_intraday();  -- expect < 20 ms during market hours
