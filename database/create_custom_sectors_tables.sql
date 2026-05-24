-- ══════════════════════════════════════════════════════════════
-- CUSTOM SECTORS — deep/granular sub-sector taxonomy
-- ══════════════════════════════════════════════════════════════
-- Complements the existing broad sector mapping (stock_universe.sector)
-- with a more detailed classification, e.g.
--   Banks & Finance   →  Private Banks, PSU Banks, NBFC, Housing Finance…
--   IT & Technology   →  IT Services, Product SaaS, IT Consulting…
--   Pharma & Healthcare → Generic Pharma, CDMO, Hospitals, Diagnostics…
--
-- Two tables:
--   custom_sectors         — the taxonomy (one row per sub-sector)
--   stock_custom_sector    — the security → sub-sector assignment
--
-- A single security may belong to multiple custom sectors (e.g. a
-- defence company that also does aerospace), hence composite PK
-- instead of a single "custom_sector_id" column on stock_universe.
-- ══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS custom_sectors (
    id              SERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,           -- url/key-safe, e.g. "private-banks"
    name            TEXT NOT NULL,                  -- display name,   e.g. "Private Banks"
    parent_sector   TEXT NOT NULL,                  -- one of the 20 broad buckets
    description     TEXT DEFAULT '',
    keywords        JSONB DEFAULT '[]'::jsonb,      -- for keyword-based auto-classification
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_custom_sectors_parent ON custom_sectors(parent_sector);
CREATE INDEX IF NOT EXISTS idx_custom_sectors_active ON custom_sectors(is_active);


CREATE TABLE IF NOT EXISTS stock_custom_sector (
    security_id         TEXT NOT NULL,                      -- matches stock_universe.security_id
    custom_sector_id    INTEGER NOT NULL REFERENCES custom_sectors(id) ON DELETE CASCADE,
    source              TEXT NOT NULL DEFAULT 'seed',       -- seed | keyword | manual | ai | segment_revenue | keyword_fallback
    confidence          NUMERIC(4,3) DEFAULT 1.000,         -- 0.000–1.000
    is_primary          BOOLEAN DEFAULT TRUE,               -- primary sub-sector for the stock
    note                TEXT DEFAULT '',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (security_id, custom_sector_id)
);

CREATE INDEX IF NOT EXISTS idx_stock_custom_sector_sid ON stock_custom_sector(security_id);
CREATE INDEX IF NOT EXISTS idx_stock_custom_sector_cs  ON stock_custom_sector(custom_sector_id);
CREATE INDEX IF NOT EXISTS idx_stock_custom_sector_primary
    ON stock_custom_sector(security_id) WHERE is_primary = TRUE;


-- ══════════════════════════════════════════════════════════════
-- Classification audit — tracks every attempt to assign a stock
-- so we can review low-confidence picks, retrain keywords, etc.
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS custom_sector_classification_log (
    id                  BIGSERIAL PRIMARY KEY,
    security_id         TEXT NOT NULL,       -- matches stock_universe.security_id
    custom_sector_id    INTEGER,
    source              TEXT NOT NULL,
    confidence          NUMERIC(4,3),
    matched_keyword     TEXT,
    raw_input           TEXT,                -- name/industry string that drove the decision
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_csc_log_sid  ON custom_sector_classification_log(security_id);
CREATE INDEX IF NOT EXISTS idx_csc_log_date ON custom_sector_classification_log(created_at);

-- ══════════════════════════════════════════════════════════════
-- Valvo sector column on stock_universe — denormalized primary
-- parent_sector string so scan/list queries avoid an extra join.
-- ══════════════════════════════════════════════════════════════
ALTER TABLE stock_universe ADD COLUMN IF NOT EXISTS valvo_sector TEXT;
CREATE INDEX IF NOT EXISTS idx_stock_universe_valvo_sector ON stock_universe(valvo_sector);
