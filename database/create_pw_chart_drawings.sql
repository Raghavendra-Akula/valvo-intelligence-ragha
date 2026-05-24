-- Past-Winners chart drawings — annotations scoped to (user, stock, scope).
-- scope = "pw:YYYY-MM-DD:YYYY-MM-DD" for a PW from/to window, keeping PW notes
-- separate from the global per-stock drawings in chart_drawings.
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS pw_chart_drawings (
    user_id      TEXT        NOT NULL,
    security_id  TEXT        NOT NULL,
    scope        TEXT        NOT NULL,
    drawings     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, security_id, scope)
);

CREATE INDEX IF NOT EXISTS pw_chart_drawings_user_idx
    ON pw_chart_drawings (user_id, updated_at DESC);
