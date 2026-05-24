-- ═══════════════════════════════════════════════════════════════════════════
-- journal_entry_metrics
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Add two snapshot-at-entry metrics to journal_trades and positions:
--   liquidity_at_entry — 20-day average (volume × close) at the entry date,
--                        in Crores (₹). Source: candles_daily.
--   mcap_at_entry      — market capitalisation at the entry date, in Crores.
--                        Source: stock_universe.shares_outstanding × close.
--
-- Both columns are server-side autofilled at entry-creation time and are
-- not surfaced in the journal grid UI. They give the Valvo AI / analytics
-- layer a frozen "what was the stock's footprint when I bought" reading,
-- so we can later answer questions like "do I do better in mid-caps vs
-- small-caps" or "did this name's liquidity dry up after I entered".
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE journal_trades ADD COLUMN IF NOT EXISTS liquidity_at_entry REAL;
ALTER TABLE journal_trades ADD COLUMN IF NOT EXISTS mcap_at_entry      REAL;

ALTER TABLE positions      ADD COLUMN IF NOT EXISTS liquidity_at_entry REAL;
ALTER TABLE positions      ADD COLUMN IF NOT EXISTS mcap_at_entry      REAL;
