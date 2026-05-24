-- ──────────────────────────────────────────────────────────────────────────
-- summary_refresh_log
-- ──────────────────────────────────────────────────────────────────────────
-- Audit log written by refresh_stock_daily_summary(). Every call inserts a
-- row on entry and UPDATEs it on success or in the EXCEPTION handler, so a
-- failed run leaves status='failed' + error_message populated.
--
-- Read by the /api/health/data-freshness endpoint to detect failed cron runs
-- and by ops to investigate degraded scans.
-- Cleaned weekly by the cleanup_summary_log cron job (see cron_jobs.sql).
-- ──────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.summary_refresh_log (
    id               BIGSERIAL PRIMARY KEY,
    trigger_source   TEXT NOT NULL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at      TIMESTAMPTZ,
    stocks_refreshed INTEGER,
    status           TEXT CHECK (status IN ('running', 'success', 'failed')),
    error_message    TEXT
);

CREATE INDEX IF NOT EXISTS idx_summary_refresh_log_started
    ON public.summary_refresh_log (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_summary_refresh_log_status_started
    ON public.summary_refresh_log (status, started_at DESC)
    WHERE status = 'failed';
