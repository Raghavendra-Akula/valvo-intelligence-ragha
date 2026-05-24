-- ════════════════════════════════════════════════════════════════════
--  Crisp Explore cards — sector thesis + catalysts + short theme
--
--  The Explore strip's old Company Summary card paired a one-line
--  business blurb with the long Theme Thesis paragraph. We're replacing
--  that single card with three precise cards, all DeepSeek-backed:
--
--    1. "Why this Theme"  — 1 crisp sentence (existing table, new col)
--    2. "Why this Sector" — 1 crisp sentence (new table)
--    3. "Catalysts"       — 2-4 bullets synthesized from internal
--                            signals (concall summary + last quarter
--                            beats/misses + segment growth). New table.
--
--  All three use DeepSeek (gateway in services/valvo_ai_v7) — no
--  external web search, by explicit user direction. Catalysts are keyed
--  by the concall period that fed them so a new concall invalidates
--  cleanly without a destructive DELETE.
-- ════════════════════════════════════════════════════════════════════


-- ── 1) Short theme explanation as a sibling column ─────────────────
--    Existing rows keep their long paragraph; the short field starts
--    NULL and gets populated lazily on first crisp-card request.
ALTER TABLE stock_theme_explanations
    ADD COLUMN IF NOT EXISTS short_explanation TEXT;


-- ── 2) Sector thesis cache — mirrors stock_theme_explanations ──────
CREATE TABLE IF NOT EXISTS stock_sector_explanations (
    id              BIGSERIAL PRIMARY KEY,
    security_id     TEXT NOT NULL,
    sector          TEXT NOT NULL,           -- the sector label this row explains
    explanation     TEXT NOT NULL,           -- 1-2 sentence "why this sector"
    prompt_version  INT  NOT NULL DEFAULT 1,
    model_used      TEXT NOT NULL DEFAULT 'deepseek-chat',
    input_tokens    INT,
    output_tokens   INT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (security_id, sector, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_sse_lookup
    ON stock_sector_explanations (security_id, sector, prompt_version DESC);


-- ── 3) Catalysts cache — JSONB list, keyed by concall_period ───────
--    `concall_period` is the period_end_date of the concall row that
--    grounded the synthesis (e.g. '2025-12-31'). When a new concall
--    lands, the next read for this stock misses the cache and a fresh
--    catalyst list is synthesized. Old rows stay as audit trail.
CREATE TABLE IF NOT EXISTS stock_catalysts (
    id              BIGSERIAL PRIMARY KEY,
    security_id     TEXT NOT NULL,
    concall_period  DATE,                    -- nullable: stocks with no concall still get a fundamentals-only synthesis
    catalysts_json  JSONB NOT NULL,          -- [{title, detail, kind}]
    prompt_version  INT  NOT NULL DEFAULT 1,
    model_used      TEXT NOT NULL DEFAULT 'deepseek-chat',
    input_tokens    INT,
    output_tokens   INT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (security_id, concall_period, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_sc_lookup
    ON stock_catalysts (security_id, concall_period, prompt_version DESC);
