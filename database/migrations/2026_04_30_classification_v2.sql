-- ════════════════════════════════════════════════════════════════════
-- Classification V2 — parallel taxonomy, evidence layer, concall layer
-- ════════════════════════════════════════════════════════════════════
-- Purpose
--   Build a richer, segment-driven, evidence-backed classification system
--   alongside the existing V1 tables. V1 (`custom_sectors`, `themes`,
--   `stock_themes`, `stock_custom_sector`) is left completely untouched —
--   the live UI continues to read from V1 until V2 is verified.
--
-- Design principles
--   1. Additive only. No DROP, no ALTER on V1 tables. Re-running this file
--      is safe (everything is IF NOT EXISTS / idempotent).
--   2. V2 taxonomy lives in its own tables with the `_v2` suffix so the
--      seed can evolve (new sub-sectors like wires-cables, new themes
--      like dc_connectivity_fiber) without colliding with V1.
--   3. Every classification decision is logged in `classification_evidence_v2`
--      with the layer (sector / sub_sector / theme), the source kind
--      (segment_revenue / concall_understanding / industry_text /
--      manual_override / etc.), the confidence, and the underlying
--      evidence_data (JSONB). The right-hand "Why" panel reads this.
--   4. Concall content lives in `concall_transcripts_v2` (raw text) and
--      `concall_understanding_v2` (Gemini-derived structured exposure %).
--      These are populated by an offline pipeline, not synchronously.
--   5. User feedback ("Suggest fix") flows into
--      `classification_review_queue_v2`. Approved entries get applied
--      to the link tables and become learned signals.
--
-- Run order
--   This file is self-contained and can be applied to Supabase in one
--   shot. Re-running it is a no-op (idempotent).
-- ════════════════════════════════════════════════════════════════════


-- ─────────────────────────────────────────────────────────────────────
-- 1. WAVES V2  — top-level tailwinds (mirrors V1 `waves`, separate seed)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS waves_v2 (
    slug            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    accent_color    TEXT,
    sort_order      INT DEFAULT 0,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);


-- ─────────────────────────────────────────────────────────────────────
-- 2. THEMES V2  — granular themes under waves (mirrors V1 `themes`)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS themes_v2 (
    slug              TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    wave_slug         TEXT REFERENCES waves_v2(slug) ON DELETE SET NULL,
    parent_sector     TEXT,                          -- canonical broad bucket
    description       TEXT,
    keywords          JSONB DEFAULT '[]'::jsonb,     -- substring matches
    name_overrides    JSONB DEFAULT '[]'::jsonb,     -- pure-play symbols
    segment_keywords  JSONB DEFAULT '[]'::jsonb,     -- match against segment_name
    accent_color      TEXT,
    sort_order        INT DEFAULT 0,
    is_active         BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_themes_v2_wave ON themes_v2(wave_slug);
CREATE INDEX IF NOT EXISTS idx_themes_v2_active ON themes_v2(is_active);


-- ─────────────────────────────────────────────────────────────────────
-- 3. CUSTOM SECTORS V2  — sub-sector taxonomy (mirrors V1 `custom_sectors`)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS custom_sectors_v2 (
    id                SERIAL PRIMARY KEY,
    slug              TEXT UNIQUE NOT NULL,
    name              TEXT NOT NULL,
    parent_sector     TEXT NOT NULL,                 -- one of the 20 broad
    description       TEXT,
    keywords          JSONB DEFAULT '[]'::jsonb,
    segment_keywords  JSONB DEFAULT '[]'::jsonb,     -- segment_name patterns
    name_overrides    JSONB DEFAULT '[]'::jsonb,     -- pure-play symbols
    is_active         BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_custom_sectors_v2_parent ON custom_sectors_v2(parent_sector);
CREATE INDEX IF NOT EXISTS idx_custom_sectors_v2_active ON custom_sectors_v2(is_active);


-- ─────────────────────────────────────────────────────────────────────
-- 4. STOCK ↔ THEMES V2  — weighted exposure per stock per theme
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_themes_v2 (
    security_id      TEXT NOT NULL,
    theme_slug       TEXT NOT NULL REFERENCES themes_v2(slug) ON DELETE CASCADE,
    exposure_score   NUMERIC(5,4) DEFAULT 0,        -- 0.0000–1.0000
    confidence       NUMERIC(5,4) DEFAULT 0,        -- 0.0000–1.0000
    is_primary       BOOLEAN DEFAULT FALSE,
    source           TEXT,                          -- segment_revenue / concall / name_override / manual / fallback
    matched_term     TEXT,                          -- what triggered the match
    evidence_url     TEXT,
    evidence_note    TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (security_id, theme_slug)
);

CREATE INDEX IF NOT EXISTS idx_stock_themes_v2_theme ON stock_themes_v2(theme_slug);
CREATE INDEX IF NOT EXISTS idx_stock_themes_v2_security ON stock_themes_v2(security_id);
CREATE INDEX IF NOT EXISTS idx_stock_themes_v2_primary ON stock_themes_v2(security_id) WHERE is_primary;


-- ─────────────────────────────────────────────────────────────────────
-- 5. STOCK ↔ CUSTOM SECTORS V2
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_custom_sector_v2 (
    security_id        TEXT NOT NULL,
    custom_sector_id   INT NOT NULL REFERENCES custom_sectors_v2(id) ON DELETE CASCADE,
    confidence         NUMERIC(5,4) DEFAULT 0,
    is_primary         BOOLEAN DEFAULT FALSE,
    source             TEXT,
    matched_keyword    TEXT,
    note               TEXT,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (security_id, custom_sector_id)
);

CREATE INDEX IF NOT EXISTS idx_stock_custom_sector_v2_sid ON stock_custom_sector_v2(security_id);
CREATE INDEX IF NOT EXISTS idx_stock_custom_sector_v2_cs ON stock_custom_sector_v2(custom_sector_id);
CREATE INDEX IF NOT EXISTS idx_stock_custom_sector_v2_primary ON stock_custom_sector_v2(security_id) WHERE is_primary;


-- ─────────────────────────────────────────────────────────────────────
-- 6. STOCK ↔ V2 SECTOR  — denormalised primary sector per stock
--   Useful for fast group-by-sector queries without joining the link
--   tables. Filled by the V2 classifier; mirrors `stock_universe.valvo_sector`
--   but isolated under V2.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_sector_v2 (
    security_id     TEXT PRIMARY KEY,
    sector          TEXT NOT NULL,                  -- one of the 20 broad
    sub_sector_slug TEXT,                           -- primary custom sector slug
    primary_theme   TEXT,                           -- highest-exposure theme slug
    primary_wave    TEXT,                           -- corresponding wave slug
    confidence      NUMERIC(5,4) DEFAULT 0,
    source          TEXT,                           -- segment_revenue / concall / etc.
    classified_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_sector_v2_sector ON stock_sector_v2(sector);
CREATE INDEX IF NOT EXISTS idx_stock_sector_v2_sub ON stock_sector_v2(sub_sector_slug);
CREATE INDEX IF NOT EXISTS idx_stock_sector_v2_theme ON stock_sector_v2(primary_theme);


-- ─────────────────────────────────────────────────────────────────────
-- 7. CLASSIFICATION EVIDENCE V2  — every decision logged for the "Why" panel
-- ─────────────────────────────────────────────────────────────────────
-- One row = one piece of evidence backing a layer assignment for a stock.
-- Multiple rows per (security_id, layer) are fine (we want the trail, not
-- a single answer). The classifier writes these; the API joins them when
-- the user clicks "Why this?".
--
-- evidence_kind values (informal enum):
--   segment_revenue       — segments_quarterly row drove the assignment
--   concall_understanding — Gemini extracted from concall transcript
--   industry_text         — fundamentals_overview.industry text matched
--   name_match            — company_name keyword hit
--   keyword_match         — generic keyword fired against text blob
--   manual_override       — admin or learned user feedback
--   peer_inference        — k-nearest peers in `peers` table
--   web_verified          — backfill from verify_themes_web.py
--   sector_override_csv   — legacy docs/sector_overrides_v2.csv
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS classification_evidence_v2 (
    id              BIGSERIAL PRIMARY KEY,
    security_id     TEXT NOT NULL,
    layer           TEXT NOT NULL,                  -- 'sector' | 'sub_sector' | 'theme'
    value_slug      TEXT,                           -- theme_slug / sub_sector slug
    value_text      TEXT,                           -- broad sector name or human label
    evidence_kind   TEXT NOT NULL,                  -- see comment above
    weight          NUMERIC(5,4) DEFAULT 0,         -- contribution to final score (0..1)
    confidence      NUMERIC(5,4) DEFAULT 0,         -- how reliable this evidence is (0..1)
    matched_term    TEXT,                           -- the substring / segment / phrase
    evidence_data   JSONB DEFAULT '{}'::jsonb,      -- structured payload
    source_ref      TEXT,                           -- pdf_url / transcript_hash / etc
    author          TEXT,                           -- 'classifier' / 'gemini' / user email
    applied_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evidence_v2_sid ON classification_evidence_v2(security_id);
CREATE INDEX IF NOT EXISTS idx_evidence_v2_layer ON classification_evidence_v2(security_id, layer);
CREATE INDEX IF NOT EXISTS idx_evidence_v2_kind ON classification_evidence_v2(evidence_kind);
CREATE INDEX IF NOT EXISTS idx_evidence_v2_value ON classification_evidence_v2(value_slug);


-- ─────────────────────────────────────────────────────────────────────
-- 8. CONCALL TRANSCRIPTS V2  — raw text content of earnings calls
-- ─────────────────────────────────────────────────────────────────────
-- Populated by an offline pipeline that pulls PDFs from
-- `filings(filing_type IN ('CONCALL_TRANSCRIPT', 'INVESTOR_PRESENTATION'))`,
-- extracts text, and stores it here. Stored once per (security_id, period).
-- The transcript_hash lets us skip re-extracting unchanged PDFs.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS concall_transcripts_v2 (
    id                BIGSERIAL PRIMARY KEY,
    security_id       TEXT NOT NULL,
    symbol            TEXT,
    period            TEXT,                         -- 'Q3FY26' / 'FY25' / etc
    period_end_date   DATE,
    filing_id         BIGINT,                       -- FK→filings.id (soft, no constraint)
    source_url        TEXT,                         -- where the PDF came from
    content_text      TEXT,                         -- extracted plain text
    content_chars     INT,                          -- length sanity-check
    transcript_hash   TEXT,                         -- sha256 of source PDF
    fetched_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (security_id, period, transcript_hash)
);

CREATE INDEX IF NOT EXISTS idx_concall_v2_sid ON concall_transcripts_v2(security_id);
CREATE INDEX IF NOT EXISTS idx_concall_v2_period ON concall_transcripts_v2(security_id, period_end_date DESC);


-- ─────────────────────────────────────────────────────────────────────
-- 9. CONCALL UNDERSTANDING V2  — Gemini-derived structured exposure
-- ─────────────────────────────────────────────────────────────────────
-- One row per (security_id, period). The pipeline runs Gemini on the
-- transcript and asks it to estimate the % of business / forward-looking
-- mention tied to AI, data centres, defence, EV, renewables, etc.
-- These percentages are *not* revenue percentages — they're a coarse
-- thematic-exposure estimate from management commentary.
--
-- exposure_json: structured payload, e.g.
--   {
--     "ai_data_center": 0.45,
--     "telecom_5g": 0.30,
--     "defence": 0.05,
--     "themes": ["dc_connectivity_fiber", "telecom_5g"],
--     "evidence_quotes": ["..."],
--     "summary": "Optical fiber business pivoting to hyperscale DCs..."
--   }
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS concall_understanding_v2 (
    id                BIGSERIAL PRIMARY KEY,
    security_id       TEXT NOT NULL,
    symbol            TEXT,
    period            TEXT,
    period_end_date   DATE,
    transcript_id     BIGINT REFERENCES concall_transcripts_v2(id) ON DELETE SET NULL,
    exposure_json     JSONB DEFAULT '{}'::jsonb,
    themes_extracted  JSONB DEFAULT '[]'::jsonb,    -- array of theme_slugs
    summary           TEXT,
    model_used        TEXT,                         -- 'gemini-2.5-flash-lite' / etc.
    model_confidence  NUMERIC(5,4),                 -- self-reported by model
    raw_response      JSONB,                        -- for debugging
    generated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (security_id, period)
);

CREATE INDEX IF NOT EXISTS idx_concall_under_v2_sid ON concall_understanding_v2(security_id);
CREATE INDEX IF NOT EXISTS idx_concall_under_v2_period ON concall_understanding_v2(security_id, period_end_date DESC);


-- ─────────────────────────────────────────────────────────────────────
-- 10. CLASSIFICATION REVIEW QUEUE V2  — user "Suggest fix" submissions
-- ─────────────────────────────────────────────────────────────────────
-- The right-hand panel's [Suggest fix] button POSTs into this table.
-- Admin reviews, approves, and on approval the change is applied to
-- the link tables and re-classification log entries are written.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS classification_review_queue_v2 (
    id                BIGSERIAL PRIMARY KEY,
    security_id       TEXT NOT NULL,
    symbol            TEXT,
    layer             TEXT NOT NULL,                -- 'sector' | 'sub_sector' | 'theme'
    current_value     TEXT,
    suggested_value   TEXT NOT NULL,
    reasoning         TEXT,
    submitted_by      TEXT,
    submitted_at      TIMESTAMPTZ DEFAULT NOW(),
    status            TEXT DEFAULT 'pending',       -- 'pending' | 'approved' | 'rejected' | 'applied'
    reviewed_by       TEXT,
    reviewed_at       TIMESTAMPTZ,
    applied_at        TIMESTAMPTZ,
    review_note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_queue_v2_status ON classification_review_queue_v2(status);
CREATE INDEX IF NOT EXISTS idx_review_queue_v2_sid ON classification_review_queue_v2(security_id);


-- ─────────────────────────────────────────────────────────────────────
-- 11. UNIFIED VIEW  — single-row classification spine per stock
-- ─────────────────────────────────────────────────────────────────────
-- The /api/v2/classification/<symbol> endpoint reads this view to
-- assemble the full spine (sector + sub-sector + top themes + waves)
-- in one query. Detail rows (all themes, all evidence) come from the
-- underlying tables when the user expands "Why".
-- ─────────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_stock_classification_v2 AS
SELECT
    ssv.security_id,
    ssv.sector,
    ssv.sub_sector_slug,
    cs.name              AS sub_sector_name,
    cs.parent_sector     AS sub_sector_parent,
    ssv.primary_theme,
    th.name              AS primary_theme_name,
    ssv.primary_wave,
    wv.name              AS primary_wave_name,
    wv.accent_color      AS primary_wave_accent,
    ssv.confidence,
    ssv.source,
    ssv.classified_at,
    ssv.updated_at
FROM stock_sector_v2 ssv
LEFT JOIN custom_sectors_v2 cs ON cs.slug = ssv.sub_sector_slug
LEFT JOIN themes_v2         th ON th.slug = ssv.primary_theme
LEFT JOIN waves_v2          wv ON wv.slug = ssv.primary_wave;


-- ─────────────────────────────────────────────────────────────────────
-- 12. updated_at trigger function (shared, idempotent)
-- ─────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION trg_set_updated_at_v2()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_waves_v2_updated') THEN
        CREATE TRIGGER trg_waves_v2_updated BEFORE UPDATE ON waves_v2
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_v2();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_themes_v2_updated') THEN
        CREATE TRIGGER trg_themes_v2_updated BEFORE UPDATE ON themes_v2
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_v2();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_custom_sectors_v2_updated') THEN
        CREATE TRIGGER trg_custom_sectors_v2_updated BEFORE UPDATE ON custom_sectors_v2
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_v2();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_stock_themes_v2_updated') THEN
        CREATE TRIGGER trg_stock_themes_v2_updated BEFORE UPDATE ON stock_themes_v2
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_v2();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_stock_custom_sector_v2_updated') THEN
        CREATE TRIGGER trg_stock_custom_sector_v2_updated BEFORE UPDATE ON stock_custom_sector_v2
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_v2();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_stock_sector_v2_updated') THEN
        CREATE TRIGGER trg_stock_sector_v2_updated BEFORE UPDATE ON stock_sector_v2
            FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at_v2();
    END IF;
END $$;


-- ════════════════════════════════════════════════════════════════════
-- DONE. V1 tables remain untouched. V2 is empty until the seed scripts
-- (Backend/config/custom_sectors_seed_v2.py, themes_seed_v2.py) and the
-- V2 classifier (Backend/services/classification_v2/) populate it.
-- ════════════════════════════════════════════════════════════════════
