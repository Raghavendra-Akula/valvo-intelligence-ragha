-- ──────────────────────────────────────────────────────────────────────────
-- pg_cron job definitions
-- ──────────────────────────────────────────────────────────────────────────
-- Exported snapshot of cron.job in production (Supabase Pro). Re-run this
-- file to recreate every job — each block unschedules by name first, then
-- re-schedules. cron.unschedule(name) silently returns false if the job
-- doesn't exist, so this is safe on a fresh database.
--
-- Schedules use UTC. NSE market is 09:15-15:30 IST = 03:45-10:00 UTC.
--
-- The summary refresh function is in summary_functions.sql.
-- The trigger chain it complements is in candles_triggers.sql.
-- The audit log it writes to is in summary_refresh_log_table.sql.
-- ──────────────────────────────────────────────────────────────────────────

-- Required extensions (Supabase: pg_cron + pg_net for HTTP callouts)
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;


-- ──────────────────────────────────────────────────────────────────────────
-- 1. Holiday candle cleanup
-- ──────────────────────────────────────────────────────────────────────────
-- Removes weekend rows that occasionally land in candles_daily/indices when
-- the listener stays connected past Friday close. Runs 9:00 AM IST Mon-Fri.
SELECT cron.unschedule('cleanup-holiday-candles');
SELECT cron.schedule(
    'cleanup-holiday-candles',
    '30 3 * * 1-5',
    $$
    DELETE FROM candles_daily WHERE date IN (
      SELECT date FROM candles_daily
      WHERE EXTRACT(DOW FROM date) IN (0, 6)
      AND date >= CURRENT_DATE - 30
    );
    DELETE FROM candles_indices WHERE date IN (
      SELECT date FROM candles_indices
      WHERE EXTRACT(DOW FROM date) IN (0, 6)
      AND date >= CURRENT_DATE - 30
    );
    $$
);


-- ──────────────────────────────────────────────────────────────────────────
-- 2. Stock daily summary refresh — three-way redundancy
-- ──────────────────────────────────────────────────────────────────────────
-- Three runs per day so a single failure doesn't leave stale data showing
-- to community users:
--   safety  — 6:00 AM IST daily (catches overnight backfills)
--   morning — 9:00 AM IST Mon-Sat (just before market open)
--   evening — 4:00 PM IST Mon-Fri (right after listener finalize)
--
-- TODO: morning schedule includes Saturday (1-6) where there's no new data.
-- Function runs anyway and produces identical output; harmless but wasteful.
-- Tighten to 1-5 once we've confirmed nothing else relies on the Saturday
-- run.

SELECT cron.unschedule('refresh_summary_morning');
SELECT cron.schedule(
    'refresh_summary_morning',
    '30 3 * * 1-6',
    $$SELECT refresh_stock_daily_summary('cron_morning')$$
);

SELECT cron.unschedule('refresh_summary_evening');
SELECT cron.schedule(
    'refresh_summary_evening',
    '30 10 * * 1-5',
    $$SELECT refresh_stock_daily_summary('cron_evening')$$
);

SELECT cron.unschedule('refresh_summary_safety');
SELECT cron.schedule(
    'refresh_summary_safety',
    '30 0 * * *',
    $$SELECT refresh_stock_daily_summary('cron_safety')$$
);


-- ──────────────────────────────────────────────────────────────────────────
-- 3. Summary refresh log retention
-- ──────────────────────────────────────────────────────────────────────────
-- Trim summary_refresh_log to the last 90 days every Sunday at 5 AM UTC.
SELECT cron.unschedule('cleanup_summary_log');
SELECT cron.schedule(
    'cleanup_summary_log',
    '0 5 * * 0',
    $$DELETE FROM summary_refresh_log WHERE started_at < NOW() - INTERVAL '90 days'$$
);


-- ──────────────────────────────────────────────────────────────────────────
-- 4. Live live_* reset
-- ──────────────────────────────────────────────────────────────────────────
-- Clears yesterday's live_close/high/low/volume from stock_daily_summary
-- at 9:05 AM IST so the listener starts the new session against a clean
-- slate (otherwise advance/decline calculations carry over yesterday's tape).
-- Function reset_live_columns() is TODO-export.
SELECT cron.unschedule('reset_live_columns_morning');
SELECT cron.schedule(
    'reset_live_columns_morning',
    '35 3 * * 1-5',
    $$SELECT reset_live_columns()$$
);


-- ──────────────────────────────────────────────────────────────────────────
-- 5. Price alerts
-- ──────────────────────────────────────────────────────────────────────────
-- Every 5 minutes during market hours (08:30-15:55 IST = 03:00-10:55 UTC).
-- Function check_and_trigger_alerts() is TODO-export.
SELECT cron.unschedule('check-price-alerts');
SELECT cron.schedule(
    'check-price-alerts',
    '*/5 3-10 * * 1-5',
    $$SELECT check_and_trigger_alerts()$$
);


-- ──────────────────────────────────────────────────────────────────────────
-- 6. Position auto-trail
-- ──────────────────────────────────────────────────────────────────────────
-- Trails stop-losses on active positions four times a day. Function
-- auto_trail_positions() is TODO-export.
SELECT cron.unschedule('trail-1115');
SELECT cron.schedule('trail-1115', '45 5 * * 1-5', $$SELECT auto_trail_positions()$$);

SELECT cron.unschedule('trail-1315');
SELECT cron.schedule('trail-1315', '45 7 * * 1-5', $$SELECT auto_trail_positions()$$);

SELECT cron.unschedule('trail-1515');
SELECT cron.schedule('trail-1515', '45 9 * * 1-5', $$SELECT auto_trail_positions()$$);

SELECT cron.unschedule('trail-1545');
SELECT cron.schedule('trail-1545', '15 10 * * 1-5', $$SELECT auto_trail_positions()$$);


-- ──────────────────────────────────────────────────────────────────────────
-- 7. Breadth intraday sampler
-- ──────────────────────────────────────────────────────────────────────────
-- Every minute Mon-Fri. Function self-guards on time window 09:15-15:30 IST,
-- but the cron currently runs 24/7. TODO: tighten to 03-10 UTC to cut
-- ~1,000 idle invocations/day.
SELECT cron.unschedule('breadth-intraday-sample');
SELECT cron.schedule(
    'breadth-intraday-sample',
    '* * * * 1-5',
    $$SELECT sample_breadth_intraday()$$
);

-- Trim breadth_intraday to the last 5 days every night at midnight UTC.
SELECT cron.unschedule('breadth-intraday-cleanup');
SELECT cron.schedule(
    'breadth-intraday-cleanup',
    '30 18 * * *',
    $$DELETE FROM breadth_intraday WHERE date < CURRENT_DATE - 5$$
);


-- ──────────────────────────────────────────────────────────────────────────
-- 8. Project hub — task due-date reminders
-- ──────────────────────────────────────────────────────────────────────────
-- Daily HTTP callout to the Cloud Run backend at 9:00 AM IST. pg_net is
-- required.
SELECT cron.unschedule('task-due-date-reminders');
SELECT cron.schedule(
    'task-due-date-reminders',
    '30 3 * * *',
    $$
    SELECT net.http_post(
        url := 'https://valvo-backend-898426542840.asia-south1.run.app/api/project-hub/run-reminders',
        headers := jsonb_build_object('Content-Type', 'application/json'),
        body := '{}'::jsonb
    );
    $$
);


-- ──────────────────────────────────────────────────────────────────────────
-- 9. Dhan broker fills → positions/journal auto-sync
-- ──────────────────────────────────────────────────────────────────────────
-- Every 5 min during NSE market hours (09:15-15:30 IST = 03:45-10:00 UTC),
-- plus a 15:35 IST tail run to catch the closing-auction fills. Backend
-- iterates every connected dhan_tokens user and pulls /trades; idempotency
-- is enforced via dhan_synced_fills.trade_id.
--
-- If DHAN_SYNC_CRON_SECRET is set on the backend, supply a matching value via:
--   ALTER DATABASE postgres SET app.dhan_sync_cron_secret = '<secret>';
-- Defaults to empty string (header sent but ignored) when GUC is unset.
DO $$
BEGIN
  PERFORM cron.unschedule('dhan-sync-fills');
EXCEPTION WHEN OTHERS THEN
  NULL;
END $$;
SELECT cron.schedule(
    'dhan-sync-fills',
    '*/5 3-10 * * 1-5',
    $$
    SELECT net.http_post(
        url := 'https://valvo-backend-898426542840.asia-south1.run.app/api/dhan/sync-fills',
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'X-Cron-Secret', COALESCE(current_setting('app.dhan_sync_cron_secret', true), '')
        ),
        body := '{}'::jsonb
    );
    $$
);


-- ──────────────────────────────────────────────────────────────────────────
-- 10. Dhan broker-side SL: morning rollover
-- ──────────────────────────────────────────────────────────────────────────
-- 09:14 IST = 03:44 UTC, Mon-Fri. Yesterday's DAY-validity SL orders have
-- expired overnight, so we re-place fresh STOP_LOSS_MARKET sells for every
-- active broker position so the broker enforces the stop_loss during the
-- new session. Subsequent SL drift (auto-trail bumps, manual edits, qty
-- changes) is reconciled by the dhan-sync-fills cron every 5 min.
DO $$
BEGIN
  PERFORM cron.unschedule('dhan-place-morning-sl-orders');
EXCEPTION WHEN OTHERS THEN
  NULL;
END $$;
SELECT cron.schedule(
    'dhan-place-morning-sl-orders',
    '44 3 * * 1-5',
    $$
    SELECT net.http_post(
        url := 'https://valvo-backend-898426542840.asia-south1.run.app/api/dhan/place-morning-sl-orders',
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'X-Cron-Secret', COALESCE(current_setting('app.dhan_sync_cron_secret', true), '')
        ),
        body := '{}'::jsonb
    );
    $$
);
