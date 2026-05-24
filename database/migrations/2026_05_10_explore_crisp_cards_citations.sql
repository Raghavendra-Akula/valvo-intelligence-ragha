-- ════════════════════════════════════════════════════════════════════
--  Crisp Explore cards — citations from Gemini grounding
--
--  When the three crisp Explore cards (Why Theme / Why Sector /
--  Catalysts) switched from DeepSeek to Gemini 3.1 Pro with the
--  built-in google_search grounding tool, the response now carries
--  a list of source URLs alongside the synthesized text. We persist
--  them in a sibling JSONB column so the frontend can render
--  source-link chips under each card.
-- ════════════════════════════════════════════════════════════════════

ALTER TABLE stock_theme_explanations  ADD COLUMN IF NOT EXISTS citations_json JSONB;
ALTER TABLE stock_sector_explanations ADD COLUMN IF NOT EXISTS citations_json JSONB;
ALTER TABLE stock_catalysts           ADD COLUMN IF NOT EXISTS citations_json JSONB;
