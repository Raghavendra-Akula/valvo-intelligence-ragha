-- ═══════════════════════════════════════════════════════════════════════════
-- ai_episodes + ai_lessons + ai_lesson_decisions
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Adds episodic + semantic memory to Valvo AI v5, alongside the existing
-- per-user `user_ai_context` (personal memory). This is the "learning ladder":
--
--   ai_episodes          ← one row per agent turn (raw log)
--          │
--          │  nightly Flash-Lite clusterer (services.valvo_ai_v5.dream)
--          ▼
--   ai_lessons (status='staged')      ← candidate lessons awaiting review
--          │
--          │  human/admin graduates or rejects with rationale
--          ▼
--   ai_lessons (status='graduated')   ← loaded into prompt by relevance
--   ai_lessons (status='rejected')    ← kept; reappearance signals churn
--
-- ai_lesson_decisions is an append-only audit log of every graduate / reject /
-- reopen action (with rationale). Rejected lessons stay in ai_lessons but the
-- decision history lives here so re-staging the same idea surfaces past
-- pushback rather than silently re-asking.
--
-- Design notes:
-- - All three tables are scoped by user_id. Lessons are per-user for now;
--   we can promote to global by setting user_id = NULL and adding a partial
--   unique index later if a clear shared pattern emerges.
-- - `tool_calls` and `signals` are JSONB for schema flexibility — the agent
--   loop logs whatever shape it wants without a migration per change.
-- - `embedding` is left out deliberately (v1). Retrieval starts heuristic
--   (recency + tag match + lexical score). If lesson count crosses ~500
--   per user we add pgvector then.
-- - Idempotent — safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────────
-- 1.  ai_episodes — raw turn log
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.ai_episodes (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID        NOT NULL,
    page_context    TEXT,
    user_message    TEXT        NOT NULL,
    final_answer    TEXT,
    -- Array of {name, input, ok, latency_ms, error?, result_digest?}
    tool_calls      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- Free-form signals for the dream cycle: detected intent, model used,
    -- token counts, follow-up rephrase flag, user reaction, etc.
    signals         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    rounds          SMALLINT,
    total_latency_ms INTEGER,
    model           TEXT,
    -- 'ok' | 'error' | 'partial' — coarse status to filter on quickly.
    status          TEXT        NOT NULL DEFAULT 'ok',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_episodes_user_recent
    ON public.ai_episodes (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_episodes_status
    ON public.ai_episodes (status, created_at DESC)
    WHERE status <> 'ok';
-- GIN index on tool_calls so the dream cycle can cluster by tool name fast.
CREATE INDEX IF NOT EXISTS idx_ai_episodes_tool_calls_gin
    ON public.ai_episodes USING GIN (tool_calls jsonb_path_ops);


-- ───────────────────────────────────────────────────────────────────────────
-- 2.  ai_lessons — staged + graduated + rejected lessons
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.ai_lessons (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID        NOT NULL,
    -- Short imperative title used for the staged-list UI.
    title           TEXT        NOT NULL,
    -- The actual lesson body injected into the system prompt when relevant.
    body            TEXT        NOT NULL,
    -- Tags drive retrieval ('rs', 'sector_rotation', 'positions', tool names…).
    tags            TEXT[]      NOT NULL DEFAULT ARRAY[]::TEXT[],
    -- 'staged' (awaiting decision) | 'graduated' (active) | 'rejected'
    status          TEXT        NOT NULL DEFAULT 'staged'
                    CHECK (status IN ('staged', 'graduated', 'rejected')),
    -- Cluster metadata from the dream cycle: example episode ids, frequency,
    -- generating model, etc. Useful when reviewing a candidate.
    source          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- How many times this lesson has been loaded into a prompt — feeds a
    -- staleness signal: graduated but never used means the retrieval is wrong.
    use_count       INTEGER     NOT NULL DEFAULT 0,
    last_used_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_lessons_user_status
    ON public.ai_lessons (user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_lessons_tags
    ON public.ai_lessons USING GIN (tags);


-- ───────────────────────────────────────────────────────────────────────────
-- 3.  ai_lesson_decisions — append-only audit of graduate/reject/reopen
-- ───────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.ai_lesson_decisions (
    id          BIGSERIAL PRIMARY KEY,
    lesson_id   BIGINT      NOT NULL REFERENCES public.ai_lessons(id) ON DELETE CASCADE,
    user_id     UUID        NOT NULL,
    -- 'graduate' | 'reject' | 'reopen'
    decision    TEXT        NOT NULL
                CHECK (decision IN ('graduate', 'reject', 'reopen')),
    rationale   TEXT        NOT NULL,
    -- Who decided. NULL means "the host agent itself" (auto-graduation, off
    -- by default). Populated with admin user_id when a human acts.
    actor_id    UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_lesson_decisions_lesson
    ON public.ai_lesson_decisions (lesson_id, created_at DESC);


-- ───────────────────────────────────────────────────────────────────────────
-- 4.  updated_at trigger for ai_lessons (so manual UPDATEs don't drift)
-- ───────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.touch_ai_lessons_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ai_lessons_touch ON public.ai_lessons;
CREATE TRIGGER trg_ai_lessons_touch
    BEFORE UPDATE ON public.ai_lessons
    FOR EACH ROW EXECUTE FUNCTION public.touch_ai_lessons_updated_at();
