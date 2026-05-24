-- ──────────────────────────────────────────────────────────────────────────
-- Candles → Stock Daily Summary trigger chain
-- ──────────────────────────────────────────────────────────────────────────
-- Two-hop cascade that keeps live prices flowing out to consumers without
-- the application or VM doing any work beyond writing candles_daily:
--
--     candles_daily INSERT/UPDATE
--         ↓ trg_sync_prev_close_insert / _update  (statement-level)
--         ↓ sync_prev_close_from_candles()
--     stock_daily_summary.live_close|high|low|volume updated
--         ↓ trg_sync_live_price (AFTER UPDATE OF live_close)
--         ↓ sync_live_price_to_dependents()
--         ├─→ positions_live.current_price + current_r_multiple
--         └─→ fundamentals_overview.current_price
--
-- The trigger functions themselves are defined in summary_functions.sql.
-- This file is idempotent (DROP TRIGGER IF EXISTS … CREATE TRIGGER).
--
-- NOTE on partitioning:
-- candles_daily is partitioned by year (see migrations/2026_04_27_partition_*).
-- Triggers attached to the parent table propagate to all current and future
-- partitions automatically. Statement-level triggers with REFERENCING NEW
-- TABLE work on partitioned parents in Postgres 15+. If you ever rename
-- the table or rebuild it, the cutover SQL (Stage 2 of the partition
-- migration) re-attaches these triggers — keep that in sync if you change
-- anything below.
-- ──────────────────────────────────────────────────────────────────────────

-- ── candles_daily ─────────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_sync_prev_close_insert ON public.candles_daily;
CREATE TRIGGER trg_sync_prev_close_insert
    AFTER INSERT ON public.candles_daily
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION public.sync_prev_close_from_candles();

DROP TRIGGER IF EXISTS trg_sync_prev_close_update ON public.candles_daily;
CREATE TRIGGER trg_sync_prev_close_update
    AFTER UPDATE ON public.candles_daily
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION public.sync_prev_close_from_candles();

-- updated_at maintenance — depends on update_updated_at_column() (TODO: export)
DROP TRIGGER IF EXISTS trg_candles_daily_updated_at ON public.candles_daily;
CREATE TRIGGER trg_candles_daily_updated_at
    BEFORE UPDATE ON public.candles_daily
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();


-- ── candles_indices ───────────────────────────────────────────────────────
DROP TRIGGER IF EXISTS trg_candles_indices_updated_at ON public.candles_indices;
CREATE TRIGGER trg_candles_indices_updated_at
    BEFORE UPDATE ON public.candles_indices
    FOR EACH ROW
    EXECUTE FUNCTION public.update_updated_at_column();


-- ── stock_daily_summary ───────────────────────────────────────────────────
-- Fires only when live_close changes, so cron-time bulk UPSERTs (which set
-- many other columns but rarely change live_close) don't cause unnecessary
-- fan-out work to positions_live and fundamentals_overview.
DROP TRIGGER IF EXISTS trg_sync_live_price ON public.stock_daily_summary;
CREATE TRIGGER trg_sync_live_price
    AFTER UPDATE OF live_close ON public.stock_daily_summary
    FOR EACH ROW
    EXECUTE FUNCTION public.sync_live_price_to_dependents();
