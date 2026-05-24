-- ============================================================================
-- Breakout Success Intelligence — schema (idempotent)
-- ----------------------------------------------------------------------------
-- Tables:
--   breakout_config            single-row JSONB knob (admin-tunable)
--   breakout_pivots            detected consolidation bases (one per security x base_end_date)
--   breakout_events            actual breakout firings + path-aware 5d/10d outcomes + squat
-- security_id is TEXT to match candles_daily and stock_universe.
-- ============================================================================

------------------------------------------------------------------------------
-- 1. breakout_config — runtime-tunable detection knobs
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS breakout_config (
    id          SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    payload     JSONB    NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by  TEXT
);

INSERT INTO breakout_config (id, payload) VALUES (1, '{
  "tightness_thresholds": {"tight": 0.05, "normal": 0.08, "loose": 0.12},
  "min_base_length": 3,
  "max_base_length": 25,
  "short_base_max_width": 0.12,
  "pivot_buffer_pct": 0.005,
  "stage2_required": true,
  "stage2_dist_from_52w_high": 0.25,
  "stage2_above_52w_low": 0.30,
  "pivot_max_age_days": 30,
  "invalidation_pct": 0.02,
  "gap_extended_pct": 0.03,
  "mcap_tiers": {
    "largecap":  {"min": 80000, "max": null},
    "midcap":    {"min": 12000, "max": 80000},
    "smallcap":  {"min": 5000,  "max": 12000},
    "microcap":  {"min": 0,     "max": 5000}
  },
  "min_liq_for_eligibility": 10.0,
  "default_dashboard_min_liq": 0.5,
  "squat_no_squat_giveback_max": 0.02,
  "squat_weak_giveback_min": 0.03,
  "squat_weak_giveback_max": 0.05,
  "squat_weak_close_min_above_pivot": 0.03,
  "path_outcome_d1_spike_pct": 0.03,
  "path_outcome_pre_5pct_window_days": 3,
  "path_outcome_target_5pct": 0.05,
  "path_outcome_target_7pct": 0.07,
  "path_outcome_target_10pct": 0.10,
  "path_outcome_success_5to7_final_min": 0.03,
  "path_outcome_success_7to10_final_min": 0.05,
  "path_outcome_success_strong_final_min": 0.07
}'::jsonb)
ON CONFLICT (id) DO NOTHING;

------------------------------------------------------------------------------
-- 2. breakout_pivots — every detected consolidation base
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS breakout_pivots (
    id                  BIGSERIAL PRIMARY KEY,
    security_id         TEXT             NOT NULL,
    base_start_date     DATE             NOT NULL,
    base_end_date       DATE             NOT NULL,
    range_high          DOUBLE PRECISION NOT NULL,
    range_low           DOUBLE PRECISION NOT NULL,
    range_width_pct     NUMERIC(7,4)     NOT NULL,
    base_length_days    SMALLINT         NOT NULL,
    avg_volume_base     BIGINT,
    tightness_grade     TEXT             CHECK (tightness_grade IN ('tight', 'normal', 'loose')),
    pivot_type          TEXT             NOT NULL DEFAULT 'flat'
                        CHECK (pivot_type IN ('flat', 'cup', 'handle', 'pennant', 'channel')),
    base_quality        SMALLINT,
    status              TEXT             NOT NULL DEFAULT 'active'
                        CHECK (status IN ('forming', 'active', 'triggered', 'expired', 'invalidated')),
    triggered_at        DATE,
    expired_at          DATE,
    invalidated_at      DATE,
    invalidated_reason  TEXT,
    detected_at         TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    UNIQUE (security_id, base_end_date)
);

CREATE INDEX IF NOT EXISTS idx_pivots_status_endate ON breakout_pivots (status, base_end_date DESC);
CREATE INDEX IF NOT EXISTS idx_pivots_sec_base_end  ON breakout_pivots (security_id, base_end_date DESC);
CREATE INDEX IF NOT EXISTS idx_pivots_active_lookup ON breakout_pivots (security_id) WHERE status = 'active';

------------------------------------------------------------------------------
-- 3. breakout_events — actual breakout firings + path-aware 5d/10d outcomes + squat
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS breakout_events (
    id              BIGSERIAL PRIMARY KEY,
    pivot_id        BIGINT           NOT NULL REFERENCES breakout_pivots(id) ON DELETE CASCADE,
    security_id     TEXT             NOT NULL,
    breakout_date   DATE             NOT NULL,
    breakout_close  DOUBLE PRECISION NOT NULL,
    pivot_price     DOUBLE PRECISION NOT NULL,
    entry_price     DOUBLE PRECISION NOT NULL,
    volume          BIGINT           NOT NULL,
    gap_up_pct      NUMERIC(7,4),
    gap_extended    BOOLEAN          NOT NULL DEFAULT false,

    -- Snapshots at breakout time — critical for liquidity/market-cap stratification.
    -- Stored on the event row so dashboard slicing is a WHERE clause, not a JOIN
    -- to today's stock_daily_summary (which would mis-categorize a stock that
    -- changed tier between breakout date and today).
    liq_cr_at_breakout   NUMERIC(12,2),   -- 20-day avg daily turnover in ₹ Cr
    mcap_cr_at_breakout  NUMERIC(14,2),   -- shares_outstanding × breakout_close / 1e7
    sector               TEXT,            -- COALESCE(valvo_sector, sector) snapshot
    industry             TEXT,

    -- Day+1 / Day+2 % gains relative to pivot (filled by update_outcomes)
    gain_d1_pct     NUMERIC(7,4),
    gain_d2_pct     NUMERIC(7,4),

    -- Path-aware outcome buckets (5d / 10d / 20d) — see services.breakout_path_outcomes
    path_outcome_5d     TEXT,
    path_outcome_10d    TEXT,
    path_outcome_20d    TEXT,
    peak_pct_5d         NUMERIC(7,4),
    peak_pct_10d        NUMERIC(7,4),
    peak_pct_20d        NUMERIC(7,4),
    final_vs_pivot_5d   NUMERIC(7,4),
    final_vs_pivot_10d  NUMERIC(7,4),
    final_vs_pivot_20d  NUMERIC(7,4),
    hit_5pct_day_5d     SMALLINT,
    hit_5pct_day_10d    SMALLINT,
    hit_5pct_day_20d    SMALLINT,
    below_pivot_day_5d  SMALLINT,
    below_pivot_day_10d SMALLINT,
    below_pivot_day_20d SMALLINT,

    -- Breakout-day intraday squat classification (see _classify_squat)
    squat_grade        TEXT CHECK (squat_grade IS NULL OR squat_grade IN ('no_squat', 'weak_squat', 'strong_squat')),
    squat_giveback_pct NUMERIC(7,4),

    breadth_snapshot JSONB,
    created_at      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    UNIQUE (pivot_id, breakout_date)
);

CREATE INDEX IF NOT EXISTS idx_events_date_outcome10 ON breakout_events (breakout_date DESC, path_outcome_10d);
CREATE INDEX IF NOT EXISTS idx_events_sec_date       ON breakout_events (security_id, breakout_date DESC);
CREATE INDEX IF NOT EXISTS idx_events_unresolved     ON breakout_events (breakout_date) WHERE path_outcome_10d IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_unresolved_20d ON breakout_events (breakout_date) WHERE path_outcome_20d IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_liq_mcap       ON breakout_events (breakout_date DESC, liq_cr_at_breakout, mcap_cr_at_breakout);
CREATE INDEX IF NOT EXISTS idx_events_sector         ON breakout_events (breakout_date DESC, sector);
CREATE INDEX IF NOT EXISTS idx_events_squat          ON breakout_events (breakout_date DESC, squat_grade);
