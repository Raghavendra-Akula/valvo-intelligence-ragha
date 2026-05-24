-- ══════════════════════════════════════════════════════════════
-- THEMES (WAVES) — cross-cutting tailwind taxonomy
-- ══════════════════════════════════════════════════════════════
-- Layer that sits alongside the valvo_sector column. A stock has
-- one sector but may carry multiple themes (many-to-many).
--
--   waves           — 6 top-level tailwinds (AI, Energy Transition, …)
--   themes          — ~22 specific themes under those waves
--   stock_themes    — M2M assignment per security
--
-- Idempotent — safe to re-run.
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS waves (
    slug            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    accent_color    TEXT,
    sort_order      INT DEFAULT 99,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_waves_active ON waves(is_active);


CREATE TABLE IF NOT EXISTS themes (
    slug            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    wave_slug       TEXT NOT NULL REFERENCES waves(slug) ON DELETE CASCADE,
    description     TEXT DEFAULT '',
    keywords        JSONB DEFAULT '[]'::jsonb,
    name_overrides  JSONB DEFAULT '[]'::jsonb,
    accent_color    TEXT,
    sort_order      INT DEFAULT 99,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_themes_wave   ON themes(wave_slug);
CREATE INDEX IF NOT EXISTS idx_themes_active ON themes(is_active);


-- Assignment: one row per (security, theme) pairing.
CREATE TABLE IF NOT EXISTS stock_themes (
    security_id     TEXT NOT NULL,
    theme_slug      TEXT NOT NULL REFERENCES themes(slug) ON DELETE CASCADE,
    exposure_score  NUMERIC(3,2) CHECK (exposure_score BETWEEN 0 AND 1),
    source          TEXT NOT NULL CHECK (source IN (
                        'segment_keyword',
                        'name_override',
                        'manual',
                        'web_verified',
                        'peer',
                        'fallback'
                    )),
    is_primary      BOOLEAN DEFAULT FALSE,
    confidence      NUMERIC(3,2),
    matched_term    TEXT,
    evidence_url    TEXT,
    evidence_note   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (security_id, theme_slug)
);

CREATE INDEX IF NOT EXISTS idx_stock_themes_theme    ON stock_themes(theme_slug);
CREATE INDEX IF NOT EXISTS idx_stock_themes_security ON stock_themes(security_id);
CREATE INDEX IF NOT EXISTS idx_stock_themes_primary
    ON stock_themes(security_id) WHERE is_primary = TRUE;


-- ══════════════════════════════════════════════════════════════
-- Audit log — every classifier attempt recorded for review.
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS theme_classification_log (
    id              BIGSERIAL PRIMARY KEY,
    security_id     TEXT NOT NULL,
    theme_slug      TEXT,
    source          TEXT NOT NULL,
    exposure_score  NUMERIC(3,2),
    confidence      NUMERIC(3,2),
    matched_term    TEXT,
    raw_input       TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tcl_sid  ON theme_classification_log(security_id);
CREATE INDEX IF NOT EXISTS idx_tcl_date ON theme_classification_log(created_at);
