-- ============================================================================
-- Breakout Success Intelligence — Outcome v2 migration (additive, idempotent)
-- ----------------------------------------------------------------------------
-- Adds path-dependent (López de Prado triple-barrier) outcome columns alongside
-- the existing path-independent max_gain / max_dd / final / outcome columns so
-- the new and old labelers can run in parallel for one full backfill, then
-- the old columns can be dropped in a follow-up migration once A/B compared.
--
-- Also adds:
--   - ATR + synthetic stops persisted at event-creation time (point-in-time)
--   - Stage-2 universe daily snapshot table (kills survivorship bias)
--
-- Safe to re-run.
-- ============================================================================

------------------------------------------------------------------------------
-- 1. Per-event vol + synthetic-stop columns (compute once, at insertion)
------------------------------------------------------------------------------
ALTER TABLE breakout_events
  ADD COLUMN IF NOT EXISTS atr14_at_breakout  NUMERIC(14,6),  -- ATR(14) using TR up to T-1
  ADD COLUMN IF NOT EXISTS stop_atr            NUMERIC(14,4),  -- entry - 1.5 * ATR (Clenow/Carver)
  ADD COLUMN IF NOT EXISTS stop_struct         NUMERIC(14,4),  -- max(range_low, pivot * 0.93) (O'Neil)
  ADD COLUMN IF NOT EXISTS risk_atr            NUMERIC(14,6),  -- entry - stop_atr  (= 1R atr-defined)
  ADD COLUMN IF NOT EXISTS risk_struct         NUMERIC(14,6),  -- entry - stop_struct
  ADD COLUMN IF NOT EXISTS atr_pct_at_breakout NUMERIC(7,4),   -- atr14 / pivot, for vol-normalization
  ADD COLUMN IF NOT EXISTS days_to_peak_60d    SMALLINT,       -- bar index of local high in [T+1, T+60]
  ADD COLUMN IF NOT EXISTS days_to_neg7pct     SMALLINT,       -- bar index of first close <= entry*0.93
  ADD COLUMN IF NOT EXISTS slippage_bps_used   SMALLINT;       -- entry slippage charged for backtest

------------------------------------------------------------------------------
-- 2. Triple-barrier outcome columns per window
-- N ∈ {5, 10, 20, 40} — 5/10/20 keep parity with existing windows; 40
-- ≈ 8 trading-week (Minervini/O'Neil hold-rule horizon).
------------------------------------------------------------------------------
ALTER TABLE breakout_events
  -- 5-day window
  ADD COLUMN IF NOT EXISTS tb_label_5d     TEXT,            -- 'pt_hit' | 'sl_hit' | 'time_exit'
  ADD COLUMN IF NOT EXISTS tb_exit_idx_5d  SMALLINT,        -- bar index when barrier touched
  ADD COLUMN IF NOT EXISTS tb_R_atr_5d     NUMERIC(7,4),    -- exit R-multiple, ATR-anchored stop
  ADD COLUMN IF NOT EXISTS tb_R_struct_5d  NUMERIC(7,4),    -- exit R-multiple, structural stop
  ADD COLUMN IF NOT EXISTS mae_5d          NUMERIC(7,4),    -- worst dd seen during trade (signed)
  ADD COLUMN IF NOT EXISTS mfe_5d          NUMERIC(7,4),    -- best run seen during trade
  ADD COLUMN IF NOT EXISTS mae_atr_5d      NUMERIC(7,4),    -- mae / atr_pct (in ATR units)
  ADD COLUMN IF NOT EXISTS mfe_atr_5d      NUMERIC(7,4),    -- mfe / atr_pct (in ATR units)
  ADD COLUMN IF NOT EXISTS bucket_v2_5d    TEXT,            -- new path-honest bucket category

  -- 10-day window
  ADD COLUMN IF NOT EXISTS tb_label_10d    TEXT,
  ADD COLUMN IF NOT EXISTS tb_exit_idx_10d SMALLINT,
  ADD COLUMN IF NOT EXISTS tb_R_atr_10d    NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS tb_R_struct_10d NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mae_10d         NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mfe_10d         NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mae_atr_10d     NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mfe_atr_10d     NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS bucket_v2_10d   TEXT,

  -- 20-day window
  ADD COLUMN IF NOT EXISTS tb_label_20d    TEXT,
  ADD COLUMN IF NOT EXISTS tb_exit_idx_20d SMALLINT,
  ADD COLUMN IF NOT EXISTS tb_R_atr_20d    NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS tb_R_struct_20d NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mae_20d         NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mfe_20d         NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mae_atr_20d     NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mfe_atr_20d     NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS bucket_v2_20d   TEXT,

  -- 40-day (8 trading-week) window — Minervini/O'Neil canonical hold horizon
  ADD COLUMN IF NOT EXISTS tb_label_40d    TEXT,
  ADD COLUMN IF NOT EXISTS tb_exit_idx_40d SMALLINT,
  ADD COLUMN IF NOT EXISTS tb_R_atr_40d    NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS tb_R_struct_40d NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS max_gain_40d    NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS max_dd_40d      NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS final_40d       NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mae_40d         NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mfe_40d         NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mae_atr_40d     NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS mfe_atr_40d     NUMERIC(7,4),
  ADD COLUMN IF NOT EXISTS bucket_v2_40d   TEXT;

CREATE INDEX IF NOT EXISTS idx_events_tb_label_10d
  ON breakout_events (breakout_date DESC, tb_label_10d);
CREATE INDEX IF NOT EXISTS idx_events_unresolved_v2
  ON breakout_events (breakout_date) WHERE tb_label_40d IS NULL;

------------------------------------------------------------------------------
-- 3. Stage-2 universe snapshot (kills survivorship bias)
-- ----------------------------------------------------------------------------
-- Without this table, _stage2_universe applies today's `is_active = true`
-- filter retroactively, silently dropping delisted/reclassified failures
-- (e.g. Yes Bank, DHFL). Empirically inflates historical win rates 1.5-2.0pp
-- (Beaver, McNichols, Price CRSP studies).
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stage2_universe_snapshots (
    snapshot_date DATE NOT NULL,
    security_id   TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, security_id)
);
CREATE INDEX IF NOT EXISTS idx_stage2_snap_date ON stage2_universe_snapshots (snapshot_date);

------------------------------------------------------------------------------
-- 4. Outcome v2 config knobs (added to JSONB; no schema change)
-- ----------------------------------------------------------------------------
-- These are merged into breakout_config.payload by the application code on
-- read; the application falls back to these defaults if the keys are missing,
-- so no UPDATE is required to make this migration safe to apply mid-flight.
--
-- New defaults (referenced in services/breakout_outcomes_v2.py):
--   "tb_pt_R": 4.0,                  -- profit-target as multiple of initial risk
--   "tb_sl_R": 1.0,                  -- stop-loss = 1R (i.e. tagged at synthetic stop)
--   "tb_pt_R_short_window": 2.0,     -- short windows use tighter targets (5d / 10d)
--   "stop_atr_mult": 1.5,            -- stop_atr = entry - 1.5 * ATR14
--   "stop_struct_pct": 0.07,         -- stop_struct = pivot * (1 - 0.07)  (O'Neil 7%)
--   "win_threshold_atr": 3.0,        -- gain >= 3 ATRs to count as "real"
--   "hard_fail_atr": -2.0,           -- dd beyond -2 ATR is a hard fail
--   "slippage_bps_by_liq": {"very_liquid": 5, "liquid": 15, "moderate": 40, "illiquid": 100},
--   "ftd_index_regime_mult": {
--      "BREADTH_THRUST": 1.20, "CONFIRMED_RALLY": 1.00, "RALLY_ATTEMPT": 0.90,
--      "UNDER_PRESSURE": 0.65, "CORRECTION": 0.40, "UNKNOWN": 0.75
--   },
--   "ftd_extension_curve_peak":  0.02,   -- Goldilocks peak at +2% above pivot
--   "ftd_extension_curve_decay": 0.05    -- starts decaying past +5%
------------------------------------------------------------------------------

-- (No DDL needed — JSON merge happens in load_config().)

-- End of migration.
