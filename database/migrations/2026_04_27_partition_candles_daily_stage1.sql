-- ═══════════════════════════════════════════════════════════════════════════
-- Partition candles_daily by year — STAGE 1: build new structure + backfill
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Goal: convert public.candles_daily from a single 6.8M-row / 1.9 GB table
-- into a parent table with one child partition per year. The application
-- sees no API change — `SELECT * FROM candles_daily` continues to work and
-- the planner does partition pruning automatically based on the WHERE clause.
--
-- This file is the SAFE first half of a two-stage migration:
--
--   STAGE 1 (this file)                   ← runnable any time, zero risk
--     1. Create new partitioned table candles_daily_v2 with yearly children
--     2. Bulk-INSERT every row from candles_daily into candles_daily_v2
--     3. Build indexes on each child partition
--     4. Verify row counts match
--   STAGE 2 (separate file, scheduled window)
--     5. Catch-up incremental writes since stage 1
--     6. Drop triggers on old table, atomic rename, re-attach triggers
--     7. Verify writes go to new table, drop old after confidence period
--
-- This file does NOT modify candles_daily or any trigger. The listener
-- continues writing to the unpartitioned table normally. Stage 1 is fully
-- reversible — if anything looks wrong, just `DROP TABLE candles_daily_v2`.
--
-- Expected runtime: ~5-10 minutes for the bulk INSERT (6.8M rows on Pro tier).
-- Expected disk usage: temporarily double — old + new tables coexist until
-- stage 2 confidence window completes.
-- ═══════════════════════════════════════════════════════════════════════════

-- ───────────────────────────────────────────────────────────────────────────
-- 1. Pre-flight checks (run these manually first; uncomment to assert here)
-- ───────────────────────────────────────────────────────────────────────────
-- SELECT pg_size_pretty(pg_total_relation_size('public.candles_daily'));
-- SELECT MIN(date), MAX(date), COUNT(*) FROM public.candles_daily;
-- SELECT COUNT(*) FROM pg_constraint
--   WHERE confrelid = 'public.candles_daily'::regclass;   -- expect 0 (no FKs)


-- ───────────────────────────────────────────────────────────────────────────
-- 2. Parent partitioned table
-- ───────────────────────────────────────────────────────────────────────────
-- Mirrors the production schema exactly. Partition key (date) is in the
-- primary key as required by Postgres for partitioned tables.

CREATE TABLE IF NOT EXISTS public.candles_daily_v2 (
    security_id TEXT             NOT NULL,
    date        DATE             NOT NULL,
    open        DOUBLE PRECISION,
    high        DOUBLE PRECISION,
    low         DOUBLE PRECISION,
    close       DOUBLE PRECISION,
    volume      BIGINT,
    updated_at  TIMESTAMPTZ      DEFAULT NOW(),
    PRIMARY KEY (security_id, date)
) PARTITION BY RANGE (date);


-- ───────────────────────────────────────────────────────────────────────────
-- 3. Child partitions — one per year, 2001 through 2027
-- ───────────────────────────────────────────────────────────────────────────
-- The oldest candles_daily row in production is 2001-09-10 (long-listed
-- blue chips like RELIANCE / SBIN). 2027 is created now so the Jan 1, 2027
-- listener doesn't fail with "no partition of relation found for row".
--
-- A reminder to add 2028 should be set for December 2027.

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2001
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2001-01-01') TO ('2002-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2002
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2002-01-01') TO ('2003-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2003
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2003-01-01') TO ('2004-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2004
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2004-01-01') TO ('2005-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2005
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2005-01-01') TO ('2006-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2006
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2006-01-01') TO ('2007-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2007
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2007-01-01') TO ('2008-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2008
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2008-01-01') TO ('2009-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2009
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2009-01-01') TO ('2010-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2010
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2010-01-01') TO ('2011-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2011
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2011-01-01') TO ('2012-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2012
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2012-01-01') TO ('2013-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2013
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2013-01-01') TO ('2014-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2014
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2014-01-01') TO ('2015-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2015
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2015-01-01') TO ('2016-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2016
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2016-01-01') TO ('2017-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2017
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2017-01-01') TO ('2018-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2018
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2018-01-01') TO ('2019-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2019
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2019-01-01') TO ('2020-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2020
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2020-01-01') TO ('2021-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2021
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2021-01-01') TO ('2022-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2022
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2022-01-01') TO ('2023-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2023
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2023-01-01') TO ('2024-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2024
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2025
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2026
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');

CREATE TABLE IF NOT EXISTS public.candles_daily_v2_y2027
    PARTITION OF public.candles_daily_v2
    FOR VALUES FROM ('2027-01-01') TO ('2028-01-01');


-- ───────────────────────────────────────────────────────────────────────────
-- 4. Bulk-load existing data
-- ───────────────────────────────────────────────────────────────────────────
-- Plain INSERT with ON CONFLICT DO NOTHING so re-running this block is safe.
-- On Supabase Pro this should take 5-10 minutes for ~6.8M rows. The whole
-- statement is one transaction; if it fails midway you'll see an error and
-- nothing is committed — re-run from scratch.

INSERT INTO public.candles_daily_v2
    (security_id, date, open, high, low, close, volume, updated_at)
SELECT security_id, date, open, high, low, close, volume, updated_at
FROM public.candles_daily
ON CONFLICT (security_id, date) DO NOTHING;


-- ───────────────────────────────────────────────────────────────────────────
-- 5. Indexes — created on the parent, propagated to all children
-- ───────────────────────────────────────────────────────────────────────────
-- Mirrors the post-2026-04-18 audited index set on the original table:
-- the covering index is the primary access path; the date-only and
-- security_id-only indexes are kept for niche queries (lookups by date
-- across all stocks, distinct security_id scans).
--
-- Postgres creates these on every existing partition automatically and on
-- any partition created in the future.

CREATE INDEX IF NOT EXISTS idx_candles_daily_v2_sid_date_covering
    ON public.candles_daily_v2 USING btree (security_id, date DESC)
    INCLUDE (open, high, low, close, volume);

CREATE INDEX IF NOT EXISTS idx_candles_daily_v2_date
    ON public.candles_daily_v2 USING btree (date);

CREATE INDEX IF NOT EXISTS idx_candles_daily_v2_security_id
    ON public.candles_daily_v2 USING btree (security_id);


-- ───────────────────────────────────────────────────────────────────────────
-- 6. Verification queries (run manually, not asserted here)
-- ───────────────────────────────────────────────────────────────────────────
-- Counts must match (within the few hundred rows the listener wrote during
-- the INSERT). Stage 2 will re-sync any drift.
--
--   SELECT 'old' AS src, COUNT(*) FROM public.candles_daily
--   UNION ALL
--   SELECT 'new' AS src, COUNT(*) FROM public.candles_daily_v2;
--
--   -- Per-year distribution sanity-check:
--   SELECT EXTRACT(YEAR FROM date) AS y, COUNT(*)
--   FROM public.candles_daily_v2 GROUP BY 1 ORDER BY 1;
--
--   -- Index sizes per partition:
--   SELECT schemaname, tablename,
--          pg_size_pretty(pg_indexes_size(format('%I.%I', schemaname, tablename)::regclass))
--   FROM pg_stat_user_tables
--   WHERE tablename LIKE 'candles_daily_v2%'
--   ORDER BY tablename;
--
-- Expected: a few empty partitions (2010-2014 likely have 0 rows), the rest
-- holding ~600-700k rows each, with the covering index ~2-3x the table size.

-- ───────────────────────────────────────────────────────────────────────────
-- ROLLBACK (if anything looks wrong)
-- ───────────────────────────────────────────────────────────────────────────
-- DROP TABLE public.candles_daily_v2 CASCADE;
-- ───────────────────────────────────────────────────────────────────────────
