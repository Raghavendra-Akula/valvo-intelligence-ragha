-- ═══════════════════════════════════════════════════════════════
-- Stock view tracking — powers Explore V2's Trending + Recently
-- Viewed panels.
--
-- Two tables (kept separate so reads stay cheap):
--   stock_view_events   per-user append-only log; "Recently Viewed"
--                       reads the latest distinct symbols per user
--   stock_view_daily    global daily aggregate; "Trending Searches"
--                       reads top-N by view_count for today
--
-- Both are written (best-effort) by routes/explore_routes.track_stock_view
-- on every successful /api/explore/stock/<symbol> hit. Failures must
-- never break the user-facing response — wrap callers in try/except.
-- Idempotent: safe to re-run.
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS stock_view_events (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL,
  symbol TEXT NOT NULL,
  viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_view_events_user_recent
  ON stock_view_events (user_id, viewed_at DESC);

CREATE INDEX IF NOT EXISTS idx_stock_view_events_symbol
  ON stock_view_events (symbol);


CREATE TABLE IF NOT EXISTS stock_view_daily (
  view_date DATE NOT NULL,
  symbol TEXT NOT NULL,
  view_count INTEGER NOT NULL DEFAULT 0,
  last_viewed TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (view_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_stock_view_daily_top
  ON stock_view_daily (view_date, view_count DESC);
