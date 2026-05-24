-- ════════════════════════════════════════════════════════════════════
--  research_jobs — work queue for the Claude-Code (no-API) worker.
--
--  Browser POSTs to /api/deep-research/queue-claude-code → row inserted
--  with status='queued'. A long-running Claude Code session pulls jobs
--  via /api/deep-research/jobs/dequeue (atomic UPDATE with FOR UPDATE
--  SKIP LOCKED), runs WebSearch + synthesis + persists into
--  deep_research_reports, and POSTs /api/deep-research/jobs/<id>/done
--  with the new report_id. Browser polls /jobs/<id>/status and auto-
--  loads the report when status flips to 'done'.
--
--  Idempotent — safe to run multiple times.
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS research_jobs (
    id            BIGSERIAL PRIMARY KEY,

    symbol        TEXT NOT NULL,
    mode          TEXT NOT NULL CHECK (mode IN ('retrospective', 'forward')),
    from_date     DATE,
    to_date       DATE,

    -- queued     → fresh, waiting for worker to claim
    -- running    → claimed by a worker, processing
    -- done       → completed, report_id populated
    -- failed     → worker reported error
    -- expired    → stale sweep (>10 min in 'running') marked failed
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'running', 'done', 'failed', 'expired')),

    report_id     BIGINT REFERENCES deep_research_reports(id) ON DELETE SET NULL,
    error         TEXT,

    requested_by  TEXT NOT NULL,                  -- user_id of admin who clicked the button
    claimed_by    TEXT,                           -- worker id (host:pid) that claimed it
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ
);

-- Hot path: dequeue picks the oldest queued job
CREATE INDEX IF NOT EXISTS idx_research_jobs_queued
    ON research_jobs (requested_at)
    WHERE status = 'queued';

-- Hot path: stale sweep finds running jobs older than X
CREATE INDEX IF NOT EXISTS idx_research_jobs_running
    ON research_jobs (started_at)
    WHERE status = 'running';

-- Browser polling: status by id (covered by PK; no extra index needed)


-- ════════════════════════════════════════════════════════════════════
--  research_workers — heartbeat from each polling worker so the UI
--  can show "Worker online" / "No worker running".
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS research_workers (
    worker_id      TEXT PRIMARY KEY,            -- e.g. 'host-mac:54321'
    user_id        TEXT NOT NULL,
    last_polled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note           TEXT
);

CREATE INDEX IF NOT EXISTS idx_research_workers_recent
    ON research_workers (last_polled_at DESC);
