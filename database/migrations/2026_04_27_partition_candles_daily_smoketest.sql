-- ═══════════════════════════════════════════════════════════════════════════
-- Partition candles_daily — POST-CUTOVER SMOKE TEST
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Run this in the Supabase SQL editor IMMEDIATELY after the Stage 2 cutover
-- COMMIT succeeds. It bundles every verification check from the runbook into
-- one DO block that prints PASS/FAIL for each, then a final summary.
--
-- This test is SIDE-EFFECT-FREE — it only does SELECTs and reads system
-- catalogs. Safe to re-run any number of times.
--
-- If ANY check fails: do not start the confidence window. Read the failed
-- check details, then either fix the specific issue or run the ROLLBACK
-- block at the bottom of stage2_cutover.sql to revert.
--
-- Expected output on success:
--
--     NOTICE: ─────────────────────────────────────────
--     NOTICE: PARTITION MIGRATION SMOKE TEST
--     NOTICE: ─────────────────────────────────────────
--     NOTICE: ✅ Row count match (new=6815217, old=6815217 — diff=0)
--     NOTICE: ✅ candles_daily is partitioned
--     NOTICE: ✅ 27 child partitions (expected 27)
--     NOTICE: ✅ 3 triggers attached to parent
--     NOTICE: ✅ 4 indexes on parent (pkey + 3)
--     NOTICE: ✅ All 18 child partitions inherit indexes
--     NOTICE: ✅ Trigger functions exist (sync_prev_close_from_candles, update_updated_at_column)
--     NOTICE: ─────────────────────────────────────────
--     NOTICE: ALL 7 CHECKS PASSED — migration verified.
--     NOTICE: Safe to start the 48-hour confidence window.
--     NOTICE: ─────────────────────────────────────────
--
-- On failure, you'll see ❌ lines and a final FAILED summary.
-- ═══════════════════════════════════════════════════════════════════════════


DO $$
DECLARE
    -- check counters
    v_pass             INT     := 0;
    v_fail             INT     := 0;
    v_warn             INT     := 0;

    -- check-specific working vars
    v_new_count        BIGINT;
    v_old_count        BIGINT;
    v_diff             BIGINT;
    v_relkind          CHAR;
    v_partition_count  INT;
    v_trigger_count    INT;
    v_parent_idx_count INT;
    v_child_min_idx    INT;
    v_func_count       INT;
    v_expected_funcs   TEXT[]  := ARRAY['sync_prev_close_from_candles',
                                        'update_updated_at_column'];
BEGIN
    RAISE NOTICE '─────────────────────────────────────────';
    RAISE NOTICE 'PARTITION MIGRATION SMOKE TEST';
    RAISE NOTICE '─────────────────────────────────────────';

    -- ── Check 1: row counts match within tolerance ────────────────────────
    -- new should be >= old (catch-up may add a few rows). Anything >100 row
    -- shortfall is data loss.
    SELECT COUNT(*) INTO v_new_count FROM public.candles_daily;
    BEGIN
        SELECT COUNT(*) INTO v_old_count FROM public.candles_daily_old;
    EXCEPTION WHEN undefined_table THEN
        v_old_count := -1;
    END;

    IF v_old_count = -1 THEN
        RAISE NOTICE '⚠️  Row count: candles_daily_old not found — already cleaned up?';
        v_warn := v_warn + 1;
    ELSE
        v_diff := v_new_count - v_old_count;
        IF v_diff >= 0 AND v_diff <= 100 THEN
            RAISE NOTICE '✅ Row count match (new=%, old=%, diff=%)',
                v_new_count, v_old_count, v_diff;
            v_pass := v_pass + 1;
        ELSIF v_diff > 100 THEN
            RAISE NOTICE '⚠️  Row count: new=% has % MORE rows than old=% (catch-up was big — verify)',
                v_new_count, v_diff, v_old_count;
            v_warn := v_warn + 1;
        ELSE
            RAISE NOTICE '❌ Row count: new=% has % FEWER rows than old=% (DATA LOSS)',
                v_new_count, ABS(v_diff), v_old_count;
            v_fail := v_fail + 1;
        END IF;
    END IF;

    -- ── Check 2: candles_daily is now a partitioned table ─────────────────
    SELECT relkind INTO v_relkind
    FROM pg_class
    WHERE relname = 'candles_daily'
      AND relnamespace = 'public'::regnamespace;

    IF v_relkind = 'p' THEN
        RAISE NOTICE '✅ candles_daily is partitioned (relkind=p)';
        v_pass := v_pass + 1;
    ELSIF v_relkind = 'r' THEN
        RAISE NOTICE '❌ candles_daily is still a regular table (relkind=r) — Stage 2 did not run or failed';
        v_fail := v_fail + 1;
    ELSE
        RAISE NOTICE '❌ candles_daily has unexpected relkind=% (or does not exist)', COALESCE(v_relkind::TEXT, 'NULL');
        v_fail := v_fail + 1;
    END IF;

    -- ── Check 3: 18 child partitions ──────────────────────────────────────
    SELECT COUNT(*) INTO v_partition_count
    FROM pg_inherits
    WHERE inhparent = 'public.candles_daily'::regclass;

    IF v_partition_count = 27 THEN
        RAISE NOTICE '✅ % child partitions (expected 27, years 2001-2027)', v_partition_count;
        v_pass := v_pass + 1;
    ELSE
        RAISE NOTICE '❌ % child partitions (expected 27) — partitions missing or extras present',
            v_partition_count;
        v_fail := v_fail + 1;
    END IF;

    -- ── Check 4: 3 triggers attached to parent ────────────────────────────
    -- Expected: trg_sync_prev_close_insert, trg_sync_prev_close_update,
    --           trg_candles_daily_updated_at
    SELECT COUNT(*) INTO v_trigger_count
    FROM pg_trigger
    WHERE tgrelid = 'public.candles_daily'::regclass
      AND NOT tgisinternal;

    IF v_trigger_count >= 3 THEN
        RAISE NOTICE '✅ % triggers attached to parent (expected 3)', v_trigger_count;
        v_pass := v_pass + 1;
    ELSE
        RAISE NOTICE '❌ % triggers attached to parent (expected 3) — Stage 2 trigger re-attach failed',
            v_trigger_count;
        v_fail := v_fail + 1;
    END IF;

    -- ── Check 5: 4 indexes on parent (pkey + 3 named) ─────────────────────
    SELECT COUNT(*) INTO v_parent_idx_count
    FROM pg_indexes
    WHERE schemaname = 'public'
      AND tablename = 'candles_daily';

    IF v_parent_idx_count >= 4 THEN
        RAISE NOTICE '✅ % indexes on parent (expected >= 4: pkey + covering + date + security_id)',
            v_parent_idx_count;
        v_pass := v_pass + 1;
    ELSE
        RAISE NOTICE '❌ % indexes on parent (expected >= 4)', v_parent_idx_count;
        v_fail := v_fail + 1;
    END IF;

    -- ── Check 6: every child partition inherited the indexes ──────────────
    -- Each child should have ~4 indexes propagated from the parent.
    SELECT MIN(idx_count) INTO v_child_min_idx
    FROM (
        SELECT tablename, COUNT(*) AS idx_count
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename ~ '^candles_daily_y20[0-9]{2}$'
        GROUP BY tablename
    ) t;

    IF v_child_min_idx IS NULL THEN
        RAISE NOTICE '❌ No child-partition indexes found — partitions may not exist or naming pattern is wrong';
        v_fail := v_fail + 1;
    ELSIF v_child_min_idx >= 4 THEN
        RAISE NOTICE '✅ All child partitions have at least % indexes', v_child_min_idx;
        v_pass := v_pass + 1;
    ELSE
        RAISE NOTICE '❌ Some child partition has only % indexes (expected >= 4) — index propagation incomplete',
            v_child_min_idx;
        v_fail := v_fail + 1;
    END IF;

    -- ── Check 7: trigger functions exist in public schema ─────────────────
    SELECT COUNT(*) INTO v_func_count
    FROM pg_proc
    WHERE pronamespace = 'public'::regnamespace
      AND proname = ANY(v_expected_funcs);

    IF v_func_count = array_length(v_expected_funcs, 1) THEN
        RAISE NOTICE '✅ Trigger functions exist (%)',
            array_to_string(v_expected_funcs, ', ');
        v_pass := v_pass + 1;
    ELSE
        RAISE NOTICE '❌ Only %/% expected trigger functions found in public schema',
            v_func_count, array_length(v_expected_funcs, 1);
        v_fail := v_fail + 1;
    END IF;

    -- ── Final summary ──────────────────────────────────────────────────────
    RAISE NOTICE '─────────────────────────────────────────';
    IF v_fail = 0 AND v_warn = 0 THEN
        RAISE NOTICE 'ALL % CHECKS PASSED — migration verified.', v_pass;
        RAISE NOTICE 'Safe to start the confidence window.';
        RAISE NOTICE 'Run DROP TABLE candles_daily_old when ready (48h+ recommended).';
    ELSIF v_fail = 0 AND v_warn > 0 THEN
        RAISE NOTICE '% CHECKS PASSED, % WARNING(s) — investigate the warnings.', v_pass, v_warn;
    ELSE
        RAISE WARNING '% CHECK(S) FAILED, % PASSED, % WARNING(s) — DO NOT proceed.',
            v_fail, v_pass, v_warn;
        RAISE WARNING 'Read the failure messages above. If you cannot fix in place,';
        RAISE WARNING 'run the ROLLBACK block at the bottom of';
        RAISE WARNING '2026_04_27_partition_candles_daily_stage2_cutover.sql';
    END IF;
    RAISE NOTICE '─────────────────────────────────────────';
END $$;


-- ═══════════════════════════════════════════════════════════════════════════
-- Optional follow-up checks — not part of the automated smoke test
-- because they're either subjective ("looks fast") or require live writes
-- to verify properly.
-- ═══════════════════════════════════════════════════════════════════════════

-- (A) Per-year row distribution sanity-check.
--     Run manually and eyeball — earlier years should hold ~600-700k rows,
--     2026 (current) much fewer (only ~4 months in), 2010-2014 likely 0.
--
--     SELECT EXTRACT(YEAR FROM date)::INT AS y, COUNT(*) AS rows
--     FROM public.candles_daily
--     GROUP BY 1 ORDER BY 1;

-- (B) Verify partition pruning kicks in for typical chart queries.
--     The plan should show only 1 child partition scanned, not 18.
--
--     EXPLAIN
--     SELECT * FROM public.candles_daily
--     WHERE security_id = '2885' AND date >= CURRENT_DATE - 30;
--
--     Look for "Append" with only candles_daily_y2026 as a child plan.

-- (C) End-to-end trigger smoke (small side effect: bumps updated_at on
--     yesterday's row by a few seconds; trigger's date filter prevents
--     stock_daily_summary.live_close from being touched).
--
--     UPDATE public.candles_daily
--     SET updated_at = NOW()
--     WHERE security_id = '2885' AND date = CURRENT_DATE - 1;
--
--     -- Confirm the update_at-touch trigger fired (updated_at is now NOW())
--     SELECT updated_at AT TIME ZONE 'Asia/Kolkata' AS bumped
--     FROM public.candles_daily
--     WHERE security_id = '2885' AND date = CURRENT_DATE - 1;

-- (D) Real end-to-end test: wait for Monday 09:10 IST listener fire and
--     watch /api/health/data-freshness — listener_tick_age_seconds should
--     be < 30s within 2 minutes of 09:15 IST.
