-- ═══════════════════════════════════════════════════════════════════════════
-- valvo_magic_persistence
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Add a single TIMESTAMPTZ column to user_settings that records when the
-- user's Valvo Magic (sector grouping) preference expires.
--
--   valvo_magic_until = NULL          → off
--   valvo_magic_until <= NOW()        → off (expired)
--   valvo_magic_until > NOW()         → on  (still within their TTL)
--
-- TTL is decided server-side from the user's plan:
--   paid → 7 days from the click
--   free → 1 day  from the click
--
-- The frontend hydrates sectorMode on mount from /api/user/valvo-magic and
-- calls the same endpoint on every toggle so both Screener and Watchlist
-- pages share one persistent preference per user.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS valvo_magic_until TIMESTAMPTZ;
