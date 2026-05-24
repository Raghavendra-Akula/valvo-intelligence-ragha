-- ═══════════════════════════════════════════════════════════════════════════
-- Partition candles_daily by year — STAGE 2: cutover (atomic swap)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- This is the dangerous half. Stage 1 created a partitioned candles_daily_v2
-- alongside the live candles_daily and bulk-loaded all rows. This file does
-- the actual SWAP: drops triggers from the live table, atomically renames
-- the two tables so the partitioned one becomes "candles_daily", then re-
-- attaches the triggers to the new target.
--
-- ┌────────────────────────────────────────────────────────────────────────┐
-- │  RUN THIS ONLY DURING A SCHEDULED MAINTENANCE WINDOW.                  │
-- │                                                                        │
-- │  Best window: Saturday evening IST.                                    │
-- │  - Listener self-stopped Friday 15:35 IST                              │
-- │  - finalize-candles ran Friday 16:05 (Mon-Fri only)                    │
-- │  - refresh_summary_morning runs Sat 09:00 (Mon-Sat) — wait til after   │
-- │  - refresh_summary_safety runs Sun 06:00 (daily) — finish before this  │
-- │  Safe to run: Sat 14:00 IST through Sat night                          │
-- └────────────────────────────────────────────────────────────────────────┘
--
-- Expected runtime: 15-30 seconds total. Most of it is the catch-up INSERT
-- (Step 1) which only writes the small delta of rows that landed since
-- Stage 1's bulk load.
--
-- Transactional: every step inside the BEGIN/COMMIT is atomic. If anything
-- fails, the whole thing rolls back — candles_daily is unchanged, triggers
-- are still attached, listener can still write. You can re-attempt later.
-- ═══════════════════════════════════════════════════════════════════════════


BEGIN;

-- ───────────────────────────────────────────────────────────────────────────
-- Step 0: lock the source table to freeze writes for the duration of the
-- swap. This is what makes the catch-up + rename atomic — no listener tick
-- can land between Step 1 and Step 4.
-- ───────────────────────────────────────────────────────────────────────────
LOCK TABLE public.candles_daily IN ACCESS EXCLUSIVE MODE;


-- ───────────────────────────────────────────────────────────────────────────
-- Step 1: catch-up INSERT — copy any rows that landed since Stage 1
-- ───────────────────────────────────────────────────────────────────────────
-- During Stage 1 the listener was off (Saturday) but the morning summary
-- refresh ran. Anything else writing to candles_daily? Should be nothing.
-- This INSERT is defensive — it grabs whatever drifted, with ON CONFLICT
-- DO UPDATE so we get the latest version of any updated row.

INSERT INTO public.candles_daily_v2
    (security_id, date, open, high, low, close, volume, updated_at)
SELECT cd.security_id, cd.date, cd.open, cd.high, cd.low, cd.close,
       cd.volume, cd.updated_at
FROM public.candles_daily cd
LEFT JOIN public.candles_daily_v2 v2
    ON cd.security_id = v2.security_id AND cd.date = v2.date
WHERE v2.security_id IS NULL
   OR cd.updated_at > v2.updated_at
ON CONFLICT (security_id, date) DO UPDATE SET
    open       = EXCLUDED.open,
    high       = EXCLUDED.high,
    low        = EXCLUDED.low,
    close      = EXCLUDED.close,
    volume     = EXCLUDED.volume,
    updated_at = EXCLUDED.updated_at;


-- ───────────────────────────────────────────────────────────────────────────
-- Step 2: drop triggers on the OLD table
-- ───────────────────────────────────────────────────────────────────────────
-- These will be re-created on the new (post-rename) candles_daily in Step 5.
-- Triggers are NOT preserved across ALTER TABLE RENAME for the OLD object;
-- they would still fire on the renamed-to-old table, which would receive no
-- writes anyway, but it's cleaner to drop and re-create.

DROP TRIGGER IF EXISTS trg_sync_prev_close_insert  ON public.candles_daily;
DROP TRIGGER IF EXISTS trg_sync_prev_close_update  ON public.candles_daily;
DROP TRIGGER IF EXISTS trg_candles_daily_updated_at ON public.candles_daily;


-- ───────────────────────────────────────────────────────────────────────────
-- Step 3: atomic rename
-- ───────────────────────────────────────────────────────────────────────────
-- After this point, every existing query, view, function, and pipeline
-- script that references "candles_daily" silently reads from / writes to
-- the new partitioned structure.

ALTER TABLE public.candles_daily    RENAME TO candles_daily_old;
ALTER TABLE public.candles_daily_v2 RENAME TO candles_daily;


-- ───────────────────────────────────────────────────────────────────────────
-- Step 4: rename the child partitions for clarity
-- ───────────────────────────────────────────────────────────────────────────
-- They were created as candles_daily_v2_y2024 etc; rename to drop the v2.
-- This is cosmetic only — partitioned-table queries don't reference child
-- names directly.

ALTER TABLE public.candles_daily_v2_y2001 RENAME TO candles_daily_y2001;
ALTER TABLE public.candles_daily_v2_y2002 RENAME TO candles_daily_y2002;
ALTER TABLE public.candles_daily_v2_y2003 RENAME TO candles_daily_y2003;
ALTER TABLE public.candles_daily_v2_y2004 RENAME TO candles_daily_y2004;
ALTER TABLE public.candles_daily_v2_y2005 RENAME TO candles_daily_y2005;
ALTER TABLE public.candles_daily_v2_y2006 RENAME TO candles_daily_y2006;
ALTER TABLE public.candles_daily_v2_y2007 RENAME TO candles_daily_y2007;
ALTER TABLE public.candles_daily_v2_y2008 RENAME TO candles_daily_y2008;
ALTER TABLE public.candles_daily_v2_y2009 RENAME TO candles_daily_y2009;
ALTER TABLE public.candles_daily_v2_y2010 RENAME TO candles_daily_y2010;
ALTER TABLE public.candles_daily_v2_y2011 RENAME TO candles_daily_y2011;
ALTER TABLE public.candles_daily_v2_y2012 RENAME TO candles_daily_y2012;
ALTER TABLE public.candles_daily_v2_y2013 RENAME TO candles_daily_y2013;
ALTER TABLE public.candles_daily_v2_y2014 RENAME TO candles_daily_y2014;
ALTER TABLE public.candles_daily_v2_y2015 RENAME TO candles_daily_y2015;
ALTER TABLE public.candles_daily_v2_y2016 RENAME TO candles_daily_y2016;
ALTER TABLE public.candles_daily_v2_y2017 RENAME TO candles_daily_y2017;
ALTER TABLE public.candles_daily_v2_y2018 RENAME TO candles_daily_y2018;
ALTER TABLE public.candles_daily_v2_y2019 RENAME TO candles_daily_y2019;
ALTER TABLE public.candles_daily_v2_y2020 RENAME TO candles_daily_y2020;
ALTER TABLE public.candles_daily_v2_y2021 RENAME TO candles_daily_y2021;
ALTER TABLE public.candles_daily_v2_y2022 RENAME TO candles_daily_y2022;
ALTER TABLE public.candles_daily_v2_y2023 RENAME TO candles_daily_y2023;
ALTER TABLE public.candles_daily_v2_y2024 RENAME TO candles_daily_y2024;
ALTER TABLE public.candles_daily_v2_y2025 RENAME TO candles_daily_y2025;
ALTER TABLE public.candles_daily_v2_y2026 RENAME TO candles_daily_y2026;
ALTER TABLE public.candles_daily_v2_y2027 RENAME TO candles_daily_y2027;


-- ───────────────────────────────────────────────────────────────────────────
-- Step 5: re-attach triggers to the new candles_daily
-- ───────────────────────────────────────────────────────────────────────────
-- Same definitions as Backend/database/candles_triggers.sql — the trigger
-- functions (sync_prev_close_from_candles, update_updated_at_column) are
-- unchanged and live in the public schema; we just bind them to the new
-- table.
--
-- Note: REFERENCING NEW TABLE AS new_rows is supported on partitioned
-- tables in Postgres 15+, which Supabase runs.

CREATE TRIGGER trg_sync_prev_close_insert
    AFTER INSERT ON public.candles_daily
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION public.sync_prev_close_from_candles();

CREATE TRIGGER trg_sync_prev_close_update
    AFTER UPDATE ON public.candles_daily
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION public.sync_prev_close_from_candles();

CREATE TRIGGER trg_candles_daily_updated_at
    BEFORE UPDATE ON public.candles_daily
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();


COMMIT;


-- ───────────────────────────────────────────────────────────────────────────
-- Post-cutover verification — run these AFTER the COMMIT above
-- ───────────────────────────────────────────────────────────────────────────
-- 1) Counts should match (catch-up + bulk = old):
--      SELECT
--        (SELECT COUNT(*) FROM public.candles_daily)     AS new_total,
--        (SELECT COUNT(*) FROM public.candles_daily_old) AS old_total;
--
-- 2) Plan looks like an Append over partitions, not a single seq scan:
--      EXPLAIN ANALYZE
--      SELECT * FROM public.candles_daily
--      WHERE security_id = '2885' AND date >= CURRENT_DATE - 30;
--    Look for "Append" + "Subplans Removed" indicating partition pruning.
--
-- 3) Triggers are attached:
--      SELECT trigger_name, event_object_table
--      FROM information_schema.triggers
--      WHERE event_object_table = 'candles_daily';
--    Expect three rows: insert, update, updated_at.
--
-- 4) Smoke test: insert a synthetic candle for today and check the trigger
--    propagated to stock_daily_summary.live_close. Use a non-prod
--    security_id you can rollback.


-- ═══════════════════════════════════════════════════════════════════════════
-- ROLLBACK (within the confidence window — say, before the first listener
-- run on Monday 9:10 IST)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- BEGIN;
--   LOCK TABLE public.candles_daily IN ACCESS EXCLUSIVE MODE;
--
--   DROP TRIGGER IF EXISTS trg_sync_prev_close_insert  ON public.candles_daily;
--   DROP TRIGGER IF EXISTS trg_sync_prev_close_update  ON public.candles_daily;
--   DROP TRIGGER IF EXISTS trg_candles_daily_updated_at ON public.candles_daily;
--
--   ALTER TABLE public.candles_daily      RENAME TO candles_daily_partitioned_failed;
--   ALTER TABLE public.candles_daily_old  RENAME TO candles_daily;
--
--   CREATE TRIGGER trg_sync_prev_close_insert
--     AFTER INSERT ON public.candles_daily
--     REFERENCING NEW TABLE AS new_rows
--     FOR EACH STATEMENT
--     EXECUTE FUNCTION public.sync_prev_close_from_candles();
--   CREATE TRIGGER trg_sync_prev_close_update
--     AFTER UPDATE ON public.candles_daily
--     REFERENCING NEW TABLE AS new_rows
--     FOR EACH STATEMENT
--     EXECUTE FUNCTION public.sync_prev_close_from_candles();
--   CREATE TRIGGER trg_candles_daily_updated_at
--     BEFORE UPDATE ON public.candles_daily
--     FOR EACH ROW
--     EXECUTE FUNCTION public.update_updated_at_column();
-- COMMIT;
--
-- After 24-48h of confidence on the partitioned table:
--   DROP TABLE public.candles_daily_old;   -- frees the ~1.9 GB old table
-- ═══════════════════════════════════════════════════════════════════════════
