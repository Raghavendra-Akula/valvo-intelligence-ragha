-- ═══════════════════════════════════════════════════════════════════════════
-- INDEX DAILY SUMMARY — per-index snapshot with 252-day rolling context
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Mirror of stock_daily_summary but for market indices (NIFTY, BANKNIFTY,
-- sector indices like NIFTY METAL, NIFTY PHARMA, NIFTY IT, etc.). One row per
-- symbol. Used by the Valvo AI agent and the Leading Sectors feature to answer
-- "what sectors are leading right now" automatically, without the user having
-- to maintain a manual list in Settings.
--
-- Refresh is lazy-on-demand (background thread from the sector read path) +
-- an admin POST endpoint. Source table is candles_indices (which itself is
-- updated by the WebSocket ticker → DB triggers chain, same as candles_daily).
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.index_daily_summary (
    symbol          TEXT PRIMARY KEY,
    category        TEXT        NOT NULL DEFAULT 'other',   -- 'sector' | 'broad' | 'thematic' | 'other'
    prev_close      DOUBLE PRECISION,
    ma50            DOUBLE PRECISION,
    ma200           DOUBLE PRECISION,
    high_52w        DOUBLE PRECISION,
    low_52w         DOUBLE PRECISION,
    ath             DOUBLE PRECISION,
    close_5d        DOUBLE PRECISION,
    close_20d       DOUBLE PRECISION,
    close_60d       DOUBLE PRECISION,
    close_120d      DOUBLE PRECISION,
    close_252d      DOUBLE PRECISION,
    return_5d       DOUBLE PRECISION,      -- (prev_close - close_5d)/close_5d * 100
    return_20d      DOUBLE PRECISION,
    return_60d      DOUBLE PRECISION,
    return_252d     DOUBLE PRECISION,
    above_ma50      BOOLEAN,               -- prev_close > ma50
    above_ma200     BOOLEAN,
    leadership_score DOUBLE PRECISION,     -- composite ranking (sector only)
    computed_date   DATE,
    updated_at      TIMESTAMP              DEFAULT NOW()
);

-- Helpful indexes for the leading-sectors query (sort by score within category)
CREATE INDEX IF NOT EXISTS idx_index_summary_category
    ON public.index_daily_summary (category);
CREATE INDEX IF NOT EXISTS idx_index_summary_category_score
    ON public.index_daily_summary (category, leadership_score DESC NULLS LAST);
