-- ════════════════════════════════════════════════════════════════════
--  trade_rationale_prompts — behavioral coaching loop.
--
--  When the user closes a trade and certain conditions fire (loss
--  streak, big-R loss), the backend writes a 'pending' row here.
--  Frontend polls /api/rationale/pending and pops the Valvo AI floating
--  chat with a targeted question. User answers in natural language;
--  Gemini Flash extracts tags from a fixed taxonomy, stored back here.
--
--  The graph page (admin-only) renders these tags as a bubble chart so
--  the user can SEE which emotions / process violations cost them
--  money over time. The whole point: convert "I felt FOMO today" into
--  a queryable, visible pattern they can avoid in the long run.
--
--  Admin-gated for now (rohit@thevalvo.com only). Tag taxonomy will
--  evolve once we see real responses; the `extracted_tags` column is
--  jsonb so adding new tags doesn't require a migration.
--
--  Idempotent — safe to re-run.
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trade_rationale_prompts (
    id              BIGSERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    trigger_kind    TEXT NOT NULL,                  -- 'loss_streak' | 'big_loss' | 'rapid_trades'
    trigger_details JSONB NOT NULL DEFAULT '{}'::jsonb,  -- count, position_ids[], symbols[], avg_r, etc.
    position_ids    BIGINT[] NOT NULL DEFAULT '{}', -- denormalized for fast graph joins
    question_text   TEXT NOT NULL,                  -- the question the AI asked
    status          TEXT NOT NULL DEFAULT 'pending',-- 'pending' | 'answered' | 'dismissed'
    answer_text     TEXT,                           -- raw natural-language response
    extracted_tags  JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ["fomo", "revenge_trade", ...]
    pnl_impact      NUMERIC,                        -- aggregate P&L (negative = loss) of the affected trades
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    answered_at     TIMESTAMPTZ,
    dismissed_at    TIMESTAMPTZ,
    CONSTRAINT trade_rationale_status_check CHECK (status IN ('pending', 'answered', 'dismissed'))
);

-- Hot path: frontend polls "give me my pending prompts" on every page mount
CREATE INDEX IF NOT EXISTS idx_trade_rationale_user_status
    ON trade_rationale_prompts (user_id, status, created_at DESC);

-- Graph page query: "all answered prompts in this window, grouped by tag"
CREATE INDEX IF NOT EXISTS idx_trade_rationale_user_answered
    ON trade_rationale_prompts (user_id, answered_at DESC)
    WHERE status = 'answered';

-- Anti-spam: avoid stacking prompts for the same user too quickly. The
-- service code reads this to decide "did we already ask in the last 24h?"
CREATE INDEX IF NOT EXISTS idx_trade_rationale_recent
    ON trade_rationale_prompts (user_id, created_at DESC);
