-- ════════════════════════════════════════════════════════════════════
--  daily_coach_reports — proactive end-of-day trade coaching.
--
--  Counterpart to trade_rationale_prompts (which is REACTIVE — fires a
--  question after a bad close). This table is PROACTIVE: a scheduled
--  job runs after market close each weekday and writes one row
--  summarising the user's recent trades against that day's market
--  context, surfacing five "leaks" plus concrete next-day fixes.
--
--  Findings JSONB shape (versioned via `schema_version`):
--    {
--      "leaks": [
--        { "key": "sizing_inversion",
--          "severity": "high"|"medium"|"low",
--          "headline": "...",
--          "detail": "...",
--          "evidence": [...],     -- trade rows used to support
--          "fix": "..." },
--        ...
--      ],
--      "market": {
--        "regime": "Trending"|"Volatile"|"Bearish"|...,
--        "nifty_change_pct": -1.2, "nifty500_change_pct": -0.9,
--        "breadth": { "advance_count":..., "decline_count":..., "pct_above_ema20":..., "thrust":... },
--        "interpretation": "Weak tape — chasing momentum is high-risk today."
--      },
--      "trades_window": {
--        "days": 7, "trades_closed": 4, "trades_opened": 3,
--        "win_rate_pct": 25, "net_pnl": -440000
--      },
--      "tag_carryover": ["fomo","position_too_large"]   -- top tags from rationale_service
--    }
--
--  leak_score (0–100): aggregate badness — 0 = clean, 100 = every leak firing high. Stored alongside JSONB so we can chart trend cheaply.
--
--  adherence_streak JSONB: { "no_sl_breach_days": 3, "no_oversize_days": 1, "no_cluster_days": 0 }
--
--  Idempotent: PRIMARY KEY on (user_id, report_date, fy).
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS daily_coach_reports (
    id                BIGSERIAL PRIMARY KEY,
    user_id           UUID NOT NULL,
    report_date       DATE NOT NULL,                    -- the trading day this report covers
    fy                TEXT NOT NULL,                    -- e.g. '2026-27'
    schema_version    SMALLINT NOT NULL DEFAULT 1,
    findings          JSONB NOT NULL DEFAULT '{}'::jsonb,
    leak_score        SMALLINT NOT NULL DEFAULT 0       -- 0..100
        CHECK (leak_score BETWEEN 0 AND 100),
    adherence_streak  JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes             TEXT,                             -- free-text user notes / acknowledgments
    acknowledged_at   TIMESTAMPTZ,                      -- "I read this report"
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT daily_coach_reports_unique UNIQUE (user_id, report_date, fy)
);

-- Hot path: "give me the latest report" + "trend over last 30 days"
CREATE INDEX IF NOT EXISTS idx_daily_coach_user_date
    ON daily_coach_reports (user_id, report_date DESC);

-- Allow ad-hoc queries by FY
CREATE INDEX IF NOT EXISTS idx_daily_coach_user_fy_date
    ON daily_coach_reports (user_id, fy, report_date DESC);

-- updated_at trigger (mirrors pattern from other tables)
CREATE OR REPLACE FUNCTION daily_coach_reports_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_daily_coach_reports_updated_at ON daily_coach_reports;
CREATE TRIGGER trg_daily_coach_reports_updated_at
    BEFORE UPDATE ON daily_coach_reports
    FOR EACH ROW EXECUTE FUNCTION daily_coach_reports_touch_updated_at();

-- RLS: user can only read/write their own reports
ALTER TABLE daily_coach_reports ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS daily_coach_reports_select_own ON daily_coach_reports;
CREATE POLICY daily_coach_reports_select_own ON daily_coach_reports
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS daily_coach_reports_modify_own ON daily_coach_reports;
CREATE POLICY daily_coach_reports_modify_own ON daily_coach_reports
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
