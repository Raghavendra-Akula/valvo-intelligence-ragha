-- ═══════════════════════════════════════════════════════════════════════════
-- USER AI CONTEXT — persistent memory for Valvo AI v5
-- ═══════════════════════════════════════════════════════════════════════════
--
-- One row per user. Stores durable facts the AI has learned across sessions
-- so it can greet users by name, reference their preferred framing, remember
-- their watchlist focus, etc. The AI is forbidden from fabricating portfolio
-- data from memory — it's purely for tone / emphasis / personalisation.
--
-- `context` is a JSONB bag with a loose but conventional shape:
--   {
--     "name":     "Rohit",
--     "bio":      "Equity momentum trader, 4% fixed SL, ~Rs5Cr portfolio",
--     "observations": [
--         "Asks about R-multiple before P&L%",
--         "Focus stocks: BSE, NALCO, HITACHI ENERGY",
--         "Interested in Metal sector rotation"
--     ]
--   }
-- The extraction prompt knows this shape but we keep it in JSONB (not columns)
-- so the schema can evolve without migrations as we learn what matters.
--
-- turn_count + last_extracted_at drive the "re-extract every 5 turns or 12h"
-- throttle that keeps memory fresh without thrashing Flash-Lite calls.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.user_ai_context (
    user_id           UUID PRIMARY KEY,
    context           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    turn_count        INTEGER     NOT NULL DEFAULT 0,
    last_extracted_at TIMESTAMP,
    created_at        TIMESTAMP   DEFAULT NOW(),
    updated_at        TIMESTAMP   DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_ai_context_updated
    ON public.user_ai_context (updated_at DESC);
