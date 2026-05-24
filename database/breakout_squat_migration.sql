-- ============================================================================
-- breakout_squat_migration.sql — adds intraday squat classification to breakout_events
-- ----------------------------------------------------------------------------
-- Idempotent. Adds two columns and an index, then backfills existing rows by
-- joining candles_daily on (security_id, breakout_date) and applying the
-- 3-bucket classifier from services/breakout_detection.py::_classify_squat.
--
-- Buckets (giveback = (high - close) / high):
--   no_squat     — giveback ≤ 0.02
--   weak_squat   — 0.03 ≤ giveback ≤ 0.05  AND  (close-pivot)/pivot ≥ 0.03
--   strong_squat — catch-all for any other meaningful giveback
-- ============================================================================

ALTER TABLE breakout_events
  ADD COLUMN IF NOT EXISTS squat_grade TEXT
    CHECK (squat_grade IS NULL OR squat_grade IN ('no_squat', 'weak_squat', 'strong_squat')),
  ADD COLUMN IF NOT EXISTS squat_giveback_pct NUMERIC(7,4);

CREATE INDEX IF NOT EXISTS idx_events_squat
  ON breakout_events (breakout_date DESC, squat_grade);

-- Backfill existing rows (only those with squat_grade IS NULL)
UPDATE breakout_events e
   SET squat_grade = CASE
         WHEN c.high IS NULL OR c.close IS NULL OR c.high <= 0 OR c.close <= 0 OR e.pivot_price <= 0 THEN NULL
         WHEN ((c.high - c.close) / c.high) <= 0.02 THEN 'no_squat'
         WHEN ((c.high - c.close) / c.high) BETWEEN 0.03 AND 0.05
              AND ((c.close - e.pivot_price) / e.pivot_price) >= 0.03 THEN 'weak_squat'
         ELSE 'strong_squat'
       END,
       squat_giveback_pct = CASE
         WHEN c.high IS NULL OR c.close IS NULL OR c.high <= 0 THEN NULL
         ELSE ROUND(((c.high - c.close) / c.high)::numeric, 4)
       END
  FROM candles_daily c
 WHERE c.security_id = e.security_id
   AND c.date        = e.breakout_date
   AND e.squat_grade IS NULL;
