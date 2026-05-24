-- ════════════════════════════════════════════════════════════════════
--  coach_qa_sessions — manual AI-led check-in conversations.
--
--  The Daily Coach (daily_coach_reports) is panoramic + quantitative.
--  This table stores the QUALITATIVE layer: a short structured Q&A the
--  user can trigger anytime, where the AI asks 4–6 questions tailored
--  to that day's leak findings + market context, and the user's free-
--  form answers are captured + auto-summarised + auto-tagged.
--
--  Transcript shape:
--    [
--      { "role": "ai",   "content": "...", "ts": "2026-05-04T10:00:00Z" },
--      { "role": "user", "content": "...", "ts": "2026-05-04T10:00:14Z" },
--      ...
--    ]
--
--  Status flow:
--    in_progress → completed   (auto-completes after question budget)
--    in_progress → abandoned   (user closes mid-flow)
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS coach_qa_sessions (
    id                BIGSERIAL PRIMARY KEY,
    user_id           UUID NOT NULL,
    session_date      DATE NOT NULL,                   -- the trading day this Q&A is about
    coach_report_id   BIGINT REFERENCES daily_coach_reports(id) ON DELETE SET NULL,
    status            TEXT NOT NULL DEFAULT 'in_progress'
        CHECK (status IN ('in_progress', 'completed', 'abandoned')),
    transcript        JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary           TEXT,
    tags              TEXT[] NOT NULL DEFAULT '{}'::text[],
    questions_asked   SMALLINT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ
);

-- Hot path: latest sessions for a user
CREATE INDEX IF NOT EXISTS idx_coach_qa_user_created
    ON coach_qa_sessions (user_id, created_at DESC);

-- Per-day lookup (most recent for a given session_date)
CREATE INDEX IF NOT EXISTS idx_coach_qa_user_date
    ON coach_qa_sessions (user_id, session_date DESC);

-- updated_at trigger
CREATE OR REPLACE FUNCTION coach_qa_sessions_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_coach_qa_sessions_updated_at ON coach_qa_sessions;
CREATE TRIGGER trg_coach_qa_sessions_updated_at
    BEFORE UPDATE ON coach_qa_sessions
    FOR EACH ROW EXECUTE FUNCTION coach_qa_sessions_touch_updated_at();

-- RLS: own-row access only
ALTER TABLE coach_qa_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS coach_qa_sessions_select_own ON coach_qa_sessions;
CREATE POLICY coach_qa_sessions_select_own ON coach_qa_sessions
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS coach_qa_sessions_modify_own ON coach_qa_sessions;
CREATE POLICY coach_qa_sessions_modify_own ON coach_qa_sessions
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
