# Runbook — Partition `candles_daily` by year

**Migration files:**
- `2026_04_27_partition_candles_daily_stage1.sql` — create + backfill (safe, any time)
- `2026_04_27_partition_candles_daily_stage2_cutover.sql` — atomic swap (scheduled window)
- `2026_04_27_partition_candles_daily_smoketest.sql` — one-paste post-cutover verification

**Estimated total time:** 30 min for Stage 1 + 5 min in-window for Stage 2.
**Active downtime:** ~30 seconds (the cutover transaction).

---

## Why we're doing this

`candles_daily` is one big 6.8M-row / 1.9 GB table. Today that's fine, but:

- **Storage cap risk** — Supabase Pro is 8 GB; growth is ~200 MB/year. We hit the cap around 2030. Without partitioning, reclaiming space requires `DELETE` + `VACUUM FULL` (slow, blocking).
- **Heavy aggregation cost** — `refresh_stock_daily_summary()` ARRAY_AGGs 400 days of data. With partitioning, only 1-2 yearly partitions are scanned instead of the entire table.
- **Archival flexibility** — to drop a year of cold data after this, it's `DROP TABLE candles_daily_y2020` (instant) instead of `DELETE FROM candles_daily WHERE date < '2021'` (hours).
- **Backups granularity** — restore one year independently if needed.

The chart endpoint speedup is real but small (~10-30%) because the existing covering index already makes those queries fast. The big wins are operational.

---

## Pre-flight (do this 1-2 days before the window)

### 1. Verify there are no foreign keys pointing AT `candles_daily`
```sql
SELECT conname, conrelid::regclass AS table_with_fk, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE confrelid = 'public.candles_daily'::regclass;
```
**Expected:** zero rows. If you see anything, stop and ping me — those FKs need to be detached and re-attached during cutover.

### 2. Check current size and take a snapshot count
```sql
SELECT pg_size_pretty(pg_total_relation_size('public.candles_daily')) AS total_size,
       pg_size_pretty(pg_relation_size('public.candles_daily'))       AS table_size,
       pg_size_pretty(pg_indexes_size('public.candles_daily'))        AS indexes_size,
       (SELECT COUNT(*) FROM public.candles_daily)                    AS row_count,
       (SELECT MIN(date) FROM public.candles_daily)                   AS oldest_date,
       (SELECT MAX(date) FROM public.candles_daily)                   AS newest_date;
```
**Save this output somewhere** — you'll diff it against the post-migration state to confirm zero data loss.

### 3. Confirm Supabase has a recent backup
Dashboard → Database → Backups. Pro tier auto-backs up daily. Verify there's one within the last 24 hours before you run Stage 2.

### 4. Pick the window
Best window is **Saturday 14:00 - 22:00 IST** because:
- Listener self-stopped Friday 15:35 IST and won't run again until Monday 09:10 IST
- `finalize-candles` and `index-volume-sync` are Mon-Fri only
- `refresh_summary_morning` runs Sat 09:00 IST — wait until after that
- `refresh_summary_safety` runs Sun 06:00 IST — finish before that
- `backfill_new_stocks` and `ipo_check` run daily 06:30 / 07:00 IST — finish before Sunday morning

If you miss the Saturday window, next safe window is **Sunday 13:00 - Monday 03:00 IST**. Avoid Monday morning entirely (listener fires at 09:10, finalize at 16:05).

---

## Stage 1: build the new structure (any time, ~10 min)

### 1.1 Run the migration
```bash
# In the Supabase SQL editor, paste the entire contents of:
Backend/database/migrations/2026_04_27_partition_candles_daily_stage1.sql
# Or via psql:
psql $DATABASE_URL -f Backend/database/migrations/2026_04_27_partition_candles_daily_stage1.sql
```

### 1.2 Wait for the bulk INSERT to finish
On Pro tier this takes 5-10 minutes for 6.8M rows. The SQL editor will show the result when done (`INSERT 0 6805753` or similar — the second number is the row count actually inserted).

### 1.3 Verify
Run the verification queries listed at the bottom of the Stage 1 SQL file:

```sql
-- Counts should match
SELECT 'old' AS src, COUNT(*) FROM public.candles_daily
UNION ALL
SELECT 'new' AS src, COUNT(*) FROM public.candles_daily_v2;
```
**Expected:** both rows show roughly the same count. The new one might be 0-100 rows behind if the listener wrote during the load — that's fine, Stage 2 catches up.

```sql
-- Per-year distribution
SELECT EXTRACT(YEAR FROM date) AS y, COUNT(*) AS rows
FROM public.candles_daily_v2 GROUP BY 1 ORDER BY 1;
```
**Expected:** non-zero counts for years where you have data (~2015 onwards), zero for 2010-2014 partitions.

### 1.4 Stop here if anything looks off
If the count is way off, or the per-year distribution looks wrong, **abort** by:
```sql
DROP TABLE public.candles_daily_v2 CASCADE;
```
The live `candles_daily` is unchanged. You can investigate and re-run Stage 1 later.

---

## Stage 2: atomic cutover (scheduled window, ~5 min total)

### 2.1 Pre-cutover sanity check (T-5 min)
```sql
-- No live writes happening?
SELECT MAX(updated_at) AT TIME ZONE 'Asia/Kolkata' AS last_write
FROM public.candles_daily;
-- Expect: a value from the most recent listener / finalize / cron run.
-- If last_write is within the last 60 seconds, something IS writing — wait.
```

```sql
-- Drift between old and new?
SELECT
  (SELECT COUNT(*) FROM public.candles_daily)     AS old_total,
  (SELECT COUNT(*) FROM public.candles_daily_v2)  AS new_total;
-- Expect: old >= new. The diff is what Stage 2 catches up.
```

### 2.2 Run Stage 2
Paste the entire contents of `2026_04_27_partition_candles_daily_stage2_cutover.sql` into the Supabase SQL editor. The whole thing is wrapped in `BEGIN; … COMMIT;` so it either completes atomically or rolls back cleanly.

**Watch for the COMMIT to succeed.** If you see an error before COMMIT, the transaction rolled back and `candles_daily` is unchanged. Read the error, fix the cause, re-attempt.

### 2.3 Immediate post-cutover verification (T+30 sec)

**Easy mode:** paste `2026_04_27_partition_candles_daily_smoketest.sql`
into the SQL editor. It runs all 7 checks below in one shot and prints
PASS / FAIL per check plus a summary verdict. Do not skip this — the
smoke test is the single go/no-go gate before walking away.

If you prefer to run the checks manually:

```sql
-- (1) Counts
SELECT
  (SELECT COUNT(*) FROM public.candles_daily)     AS new_total,
  (SELECT COUNT(*) FROM public.candles_daily_old) AS old_total;
-- Expect: equal (or new > old by the catch-up delta).

-- (2) Triggers attached
SELECT trigger_name, action_timing, event_manipulation
FROM information_schema.triggers
WHERE event_object_table = 'candles_daily'
ORDER BY trigger_name;
-- Expect three rows:
--   trg_candles_daily_updated_at  | BEFORE | UPDATE
--   trg_sync_prev_close_insert    | AFTER  | INSERT
--   trg_sync_prev_close_update    | AFTER  | UPDATE

-- (3) Plan uses partition pruning
EXPLAIN
SELECT * FROM public.candles_daily
WHERE security_id = '2885' AND date >= CURRENT_DATE - 30;
-- Expect: "Append" node with only candles_daily_y2026 (and maybe y2025
--         if cross-year) child plans listed. The other 16 partitions should
--         be excluded by partition pruning.

-- (4) The triggers fire end-to-end
-- Pick a real security_id (one you can verify), do an UPDATE that bumps
-- updated_at, and check stock_daily_summary.live_close was touched:
UPDATE public.candles_daily
SET updated_at = NOW()
WHERE security_id = '2885' AND date = CURRENT_DATE - 1;

SELECT live_close, computed_at
FROM public.stock_daily_summary
WHERE security_id = '2885';
-- Expect: live_close is unchanged (date is yesterday, not today, so the
-- trigger's WHERE n.date >= CURRENT_DATE filter excludes it). This proves
-- the trigger fired AND the date filter still works.
```

### 2.4 If verification fails — ROLLBACK
The bottom of `2026_04_27_partition_candles_daily_stage2_cutover.sql` has the rollback transaction. Run it in the SQL editor. It swaps the names back, re-attaches triggers to the original table, and you're back where you started — minus a few minutes of confusion.

---

## Confidence window (24-48 hours after Stage 2)

### Day 1 monitoring
- Hit `https://valvo-backend-898426542840.asia-south1.run.app/api/health/data-freshness` every few hours; `ok: true` and `candles.coverage_pct` rising as the next trading day's listener fills in.
- Watch the Supabase logs for any errors mentioning `candles_daily` or trigger functions.
- Run a screener scan and spot-check: chart for any active stock should render normally.

### Monday morning (first trading day after migration) — the real test
- 09:10 IST: `valvo-listener` fires. SSH into the VM and `journalctl -u valvo-listener.service -f` for 2-3 minutes — confirm tick logs are flowing.
- 09:15 IST: open the Backend `/api/health/data-freshness`. `last_listener_write` should be < 30s old.
- 09:20 IST: open the breadth dashboard. Live block should populate.
- Throughout the day: spot-check chart latency. Should feel the same or slightly faster than before.
- 16:05 IST: `finalize-candles` runs. Check `journalctl -u finalize-candles.service` — the upsert into the now-partitioned `candles_daily` should succeed.
- 16:10 IST: refresh_summary_evening cron fires; check `summary_refresh_log` for status='success'.

### Cleanup (after 48h of green)
```sql
-- Frees the ~1.9 GB the old un-partitioned table is still holding.
-- Only run after you're sure the partitioned version is solid.
DROP TABLE public.candles_daily_old;
```

---

## What this changes for application code

**Nothing immediate.** All existing queries (`SELECT FROM candles_daily`, `INSERT INTO candles_daily`, etc.) work transparently. The Postgres planner does partition pruning automatically.

**One operational change you have to remember:** every December, add the next year's partition before Jan 1. Otherwise, the Jan 1 listener INSERT will fail with `no partition of relation "candles_daily" found for row`.

```sql
-- Run before Dec 31, 2027 to create the 2028 partition
CREATE TABLE IF NOT EXISTS public.candles_daily_y2028
    PARTITION OF public.candles_daily
    FOR VALUES FROM ('2028-01-01') TO ('2029-01-01');
```

Two ways to automate this:
1. **Calendar reminder** — the simplest. Set a yearly Dec 1 reminder.
2. **pg_cron + DO block** — schedule a cron that creates next year's partition once a year. I can write that as a follow-up if you want it automated.

---

## What's NOT included in this migration

These are deliberate punts. Address separately if/when needed:

- **Hot/warm/cold tiering** — the next architectural step would be detaching `candles_daily_y2020` (and earlier), exporting to Parquet on GCS, and using a Foreign Data Wrapper for cold reads. Defer until you actually need >5 years of history readily available.
- **Sub-yearly partitioning** — if any single year's partition crosses ~5 GB, switch to monthly or quarterly partitions for that year. Not a concern for ~5 years.
- **`pg_partman` extension** — automates new-partition creation and detachment. Worth installing only when (1) you have multiple partitioned tables, or (2) you adopt monthly partitioning.

---

## Rollback summary (in case it all goes sideways mid-Stage 2)

Stage 2 is in a single transaction. If COMMIT fails, you're back where you started — just `DROP TABLE candles_daily_v2 CASCADE` and try Stage 1 again later.

If COMMIT succeeded but post-verification reveals a problem (e.g., trigger is silent, listener can't write):
- Run the ROLLBACK block at the bottom of the Stage 2 SQL file
- It re-swaps names atomically, re-attaches triggers
- You're back on the un-partitioned table
- The (now-failed) partitioned attempt sits as `candles_daily_partitioned_failed` for forensics
- Listener resumes normally on its next timer fire (Mon 09:10 IST)

If something goes wrong **after** the 48h confidence window and `candles_daily_old` has been dropped — restore from Supabase backup. That's the worst-case scenario, hopefully never reached.
