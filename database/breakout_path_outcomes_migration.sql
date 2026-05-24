-- Path-aware breakout outcome classification.
-- Companion to legacy outcome_5d/10d/20d (path-independent buckets).
--
-- Each event gets a per-window path_outcome_Nd in:
--   failed_d1_reversal | failed_pre_5pct_drop | failed_no_5pct_in_window
--   moderate_reversed
--   success_7_to_10    | success_strong_gt_10
--
-- See Backend/services/breakout_path_outcomes.py for the bucket logic.
-- Idempotent: safe to re-run.

ALTER TABLE breakout_events
  ADD COLUMN IF NOT EXISTS path_outcome_5d        TEXT,
  ADD COLUMN IF NOT EXISTS path_outcome_10d       TEXT,
  ADD COLUMN IF NOT EXISTS peak_pct_5d            NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS peak_pct_10d           NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS final_vs_pivot_5d      NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS final_vs_pivot_10d     NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS hit_5pct_day_5d        SMALLINT,
  ADD COLUMN IF NOT EXISTS hit_5pct_day_10d       SMALLINT,
  ADD COLUMN IF NOT EXISTS below_pivot_day_5d     SMALLINT,
  ADD COLUMN IF NOT EXISTS below_pivot_day_10d    SMALLINT;

CREATE INDEX IF NOT EXISTS idx_events_path_outcome_10d
  ON breakout_events (breakout_date DESC, path_outcome_10d);

CREATE INDEX IF NOT EXISTS idx_events_path_outcome_5d
  ON breakout_events (breakout_date DESC, path_outcome_5d);
