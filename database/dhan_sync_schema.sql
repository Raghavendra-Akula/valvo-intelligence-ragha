-- ═══════════════════════════════════════════════════════════════════════════
-- DHAN BROKER → POSITIONS / JOURNAL AUTO-SYNC
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Idempotent migration. Adds the plumbing required by Backend/services/dhan_sync.py
-- so every executed Dhan trade automatically materialises a position
-- (and via existing triggers, a journal_trades row).
--
-- ──────────────────────── WHAT IT ADDS ────────────────────────
--
--  1. dhan_synced_fills  — primary-key on Dhan's exchangeTradeId so the 5-min
--                          poller never double-creates a position.
--  2. positions.dhan_order_id   — links the optimistic position created at
--                                 order-placement time to the eventual fill.
--  3. positions.source_broker   — labels rows that originated from Dhan so
--                                 reporting can distinguish broker vs manual.
-- ═══════════════════════════════════════════════════════════════════════════


-- ── 1. dhan_synced_fills (idempotency ledger) ───────────────────────────────

CREATE TABLE IF NOT EXISTS public.dhan_synced_fills (
    trade_id        TEXT PRIMARY KEY,
    user_id         UUID NOT NULL,
    side            TEXT NOT NULL,
    security_id     TEXT,
    symbol          TEXT,
    quantity        INTEGER,
    fill_price      REAL,
    fill_time       TIMESTAMPTZ,
    dhan_order_id   TEXT,
    position_id     INTEGER REFERENCES public.positions(id) ON DELETE SET NULL,
    journal_id      INTEGER REFERENCES public.journal_trades(id) ON DELETE SET NULL,
    raw_fill        JSONB,
    synced_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dhan_synced_fills_user_time
  ON public.dhan_synced_fills(user_id, synced_at DESC);

CREATE INDEX IF NOT EXISTS idx_dhan_synced_fills_order
  ON public.dhan_synced_fills(dhan_order_id);

ALTER TABLE public.dhan_synced_fills ENABLE ROW LEVEL SECURITY;


-- ── 2. positions: link to Dhan order + provenance label ─────────────────────

ALTER TABLE public.positions
  ADD COLUMN IF NOT EXISTS dhan_order_id TEXT;

ALTER TABLE public.positions
  ADD COLUMN IF NOT EXISTS source_broker TEXT;

CREATE INDEX IF NOT EXISTS idx_positions_dhan_order_id
  ON public.positions(dhan_order_id)
  WHERE dhan_order_id IS NOT NULL;


-- ── 3. positions: live broker-side SL order tracking ────────────────────────
--
-- These columns mirror the SL order that lives on Dhan for a broker-synced
-- position. Auto-trail / pyramid / partial-sell paths all funnel through
-- services/dhan_sl_service.py, which compares the position's current
-- stop_loss / quantity to the last values pushed and modifies the broker
-- order on drift. DAY-validity SL orders are re-placed every morning at
-- 09:14 IST by the dhan-place-morning-sl-orders cron (see cron_jobs.sql).
--
--   dhan_sl_order_id          The active Dhan orderId for this position's SLM
--   dhan_sl_trigger           Last triggerPrice we sent to Dhan
--   dhan_sl_qty               Last quantity we sent to Dhan
--   dhan_sl_status            pending | live | cancelled | filled | failed
--   dhan_sl_last_synced_at    Wall-clock of last successful broker sync
--   dhan_sl_error             Last failure reason (sticky until next success)

ALTER TABLE public.positions
  ADD COLUMN IF NOT EXISTS dhan_sl_order_id TEXT;
ALTER TABLE public.positions
  ADD COLUMN IF NOT EXISTS dhan_sl_trigger REAL;
ALTER TABLE public.positions
  ADD COLUMN IF NOT EXISTS dhan_sl_qty INTEGER;
ALTER TABLE public.positions
  ADD COLUMN IF NOT EXISTS dhan_sl_status TEXT;
ALTER TABLE public.positions
  ADD COLUMN IF NOT EXISTS dhan_sl_last_synced_at TIMESTAMPTZ;
ALTER TABLE public.positions
  ADD COLUMN IF NOT EXISTS dhan_sl_error TEXT;

-- The morning rollover + 5-min reconciler walk every active broker position
-- — keep that scan fast.
CREATE INDEX IF NOT EXISTS idx_positions_active_broker
  ON public.positions(user_id, status)
  WHERE source_broker = 'dhan' AND status = 'active';


-- ═══════════════════════════════════════════════════════════════════════════
-- VERIFY:
--   SELECT column_name FROM information_schema.columns
--    WHERE table_name='positions' AND column_name IN ('dhan_order_id','source_broker',
--          'dhan_sl_order_id','dhan_sl_trigger','dhan_sl_qty','dhan_sl_status',
--          'dhan_sl_last_synced_at','dhan_sl_error');
--   SELECT 1 FROM information_schema.tables WHERE table_name='dhan_synced_fills';
-- ═══════════════════════════════════════════════════════════════════════════
