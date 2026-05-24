-- ════════════════════════════════════════════════════════════════════
--  stock_theme_explanations — DeepSeek-generated "Theme Thesis" cache
--
--  Each row is a 3–4 sentence narrative explaining WHY a particular
--  stock belongs in a particular V2 theme. Generated lazily on first
--  request via DeepSeek v7 gateway, then served forever from cache.
--
--  Cache key:
--    (security_id, theme_slug, prompt_version)
--
--    `prompt_version` is bumped in code when the prompt template
--    changes — bumping it naturally invalidates all old rows
--    without a destructive DROP. Old rows stay around as audit trail
--    (cheap; ~1 KB each, ~2K stocks total = ~2 MB).
--
--  Why this is its own table (not a column on stock_themes_v2):
--    • Generation is async + lazy — many stock_themes_v2 rows will
--      never have an explanation.
--    • Token usage + model tracking is per-explanation, not per-edge.
--    • Re-classifying a stock (writing a new stock_themes_v2 row) must
--      not destroy a perfectly-good explanation if the theme didn't
--      actually change.
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS stock_theme_explanations (
    id              BIGSERIAL PRIMARY KEY,
    security_id     TEXT NOT NULL,
    theme_slug      TEXT NOT NULL,
    explanation     TEXT NOT NULL,
    prompt_version  INT  NOT NULL DEFAULT 1,
    model_used      TEXT NOT NULL DEFAULT 'deepseek-chat',
    input_tokens    INT,
    output_tokens   INT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (security_id, theme_slug, prompt_version)
);

-- Hot path: read latest explanation for (stock, theme).
-- Sort by prompt_version DESC so the latest version wins on tie.
CREATE INDEX IF NOT EXISTS idx_ste_lookup
    ON stock_theme_explanations (security_id, theme_slug, prompt_version DESC);

-- Optional reverse-lookup index (admin: which stocks have an explanation
-- for this theme yet?). Cheap and useful for backfill scripts.
CREATE INDEX IF NOT EXISTS idx_ste_theme
    ON stock_theme_explanations (theme_slug);
