-- ═══════════════════════════════════════════════════════════════════════════
-- POSITION ↔ JOURNAL BIDIRECTIONAL SYNC TRIGGERS
-- ═══════════════════════════════════════════════════════════════════════════
--
-- These triggers keep live positions and journal_trades aligned whenever
-- either side's editable fields change (SL, entry, quantity). They are the
-- DB-level complement to Backend/services/journal_position_sync.py which
-- handles the complex cases (pyramids, exits, sell_history rebuild).
--
-- -------------------- HOW THEY WORK --------------------
--
--  positions row UPDATE  ─┐
--                         ├─► trg_sync_positions_to_journal  ─►  journal_trades
--                         │   (on stop_loss / entry_price / quantity)
--                         │
--  journal_trades UPDATE ─┤
--                         └─► trg_sync_journal_to_positions  ─►  positions
--                             (on sl / entry_price / initial_qty)
--
-- Matching key: BOTH user_id AND security_id must match.
--              No security_id → no sync (safe fallback).
--
-- -------------------- LOOP PREVENTION --------------------
--
-- The Python sync service sets a session-local flag before writing the
-- "authoritative" side, and the triggers honor it:
--
--   SET LOCAL app.syncing_from_journal = 'true'    (set by Python before
--                                                   writing positions from
--                                                   a journal edit — tells
--                                                   the positions trigger
--                                                   to stand down)
--
--   SET LOCAL app.syncing_from_positions = 'true'  (set inside the journal
--                                                   trigger before writing
--                                                   positions — tells the
--                                                   positions trigger to
--                                                   stand down)
--
-- Without these flags, an edit on either side would ricochet back and
-- potentially overwrite fields we just authoritatively wrote.
--
-- -------------------- HOW TO RE-APPLY --------------------
--
-- This file is idempotent — safe to run multiple times.
-- Run it in the Supabase SQL Editor or via:
--     psql "$DATABASE_URL" -f Backend/database/sync_triggers.sql
--
-- If you add new editable fields that should sync, update BOTH the
-- trigger's UPDATE OF clause AND the function body.
--
-- -------------------- SOURCE OF TRUTH --------------------
--
-- Extracted from live production DB on 2026-04-18 via Supabase MCP using
-- pg_get_functiondef() and pg_get_triggerdef(). If you change the live
-- triggers, re-export this file so the repo stays accurate.
-- ═══════════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────────
-- Drop existing triggers first so re-running is safe
-- ───────────────────────────────────────────────────────────────────────────

DROP TRIGGER IF EXISTS trg_sync_journal_to_positions ON public.journal_trades;
DROP TRIGGER IF EXISTS trg_sync_positions_to_journal ON public.positions;


-- ───────────────────────────────────────────────────────────────────────────
-- FUNCTION: sync_journal_to_positions
--   Fires when a journal_trades row's sl / entry_price / initial_qty changes
--   Writes corresponding updates to the matching active position
-- ───────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.sync_journal_to_positions()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
  -- Phase 2a: pyramid legs moved to positions.pyramid_history JSONB. journal
  -- only carries the initial-leg qty. When it changes, recompute
  -- position.quantity as new_initial_qty + Σ(pyramid_history.shares).
  new_total_qty integer := COALESCE(NEW.initial_qty, 0);
  old_total_qty integer := COALESCE(OLD.initial_qty, 0);
BEGIN
  -- Short-circuit if the positions side is already driving this sync
  IF current_setting('app.syncing_from_positions', true) = 'true' THEN
    RETURN NEW;
  END IF;

  IF NEW.security_id IS NOT NULL AND NEW.security_id != '' THEN
    PERFORM set_config('app.syncing_from_journal', 'true', true);

    UPDATE positions SET
      stop_loss = CASE
        WHEN NEW.sl IS DISTINCT FROM OLD.sl AND NEW.sl IS NOT NULL AND NEW.sl > 0
        THEN NEW.sl::double precision ELSE stop_loss END,
      entry_price = CASE
        WHEN NEW.entry_price IS DISTINCT FROM OLD.entry_price AND NEW.entry_price IS NOT NULL AND NEW.entry_price > 0
        THEN NEW.entry_price::double precision ELSE entry_price END,
      quantity = CASE
        -- Add any pyramid legs already stored on positions.pyramid_history so
        -- edits to initial_qty preserve the pyramid contribution.
        WHEN new_total_qty IS DISTINCT FROM old_total_qty AND new_total_qty > 0
        THEN new_total_qty
             + COALESCE((SELECT SUM((leg->>'shares')::int)
                          FROM jsonb_array_elements(positions.pyramid_history) AS leg), 0)
        ELSE quantity END,
      risk_pct = CASE
        WHEN NEW.sl IS DISTINCT FROM OLD.sl AND NEW.sl IS NOT NULL AND NEW.sl > 0 AND COALESCE(NEW.entry_price, OLD.entry_price) > 0
        THEN ROUND((ABS(COALESCE(NEW.entry_price, OLD.entry_price)::numeric - NEW.sl::numeric) / COALESCE(NEW.entry_price, OLD.entry_price)::numeric * 100), 2)::double precision
        ELSE risk_pct END,
      position_value = CASE
        WHEN (NEW.entry_price IS DISTINCT FROM OLD.entry_price OR new_total_qty IS DISTINCT FROM old_total_qty)
          AND NEW.entry_price IS NOT NULL AND new_total_qty > 0
        THEN ROUND((NEW.entry_price * new_total_qty)::numeric)::double precision
        ELSE position_value END,
      one_r_value = CASE
        WHEN (NEW.sl IS DISTINCT FROM OLD.sl OR NEW.entry_price IS DISTINCT FROM OLD.entry_price OR new_total_qty IS DISTINCT FROM old_total_qty)
          AND NEW.sl IS NOT NULL AND NEW.entry_price IS NOT NULL AND new_total_qty > 0 AND NEW.sl > 0
        THEN ROUND((ABS(NEW.entry_price::numeric - NEW.sl::numeric) * new_total_qty)::numeric)::double precision
        ELSE one_r_value END,
      updated_at = NOW()
    WHERE security_id = NEW.security_id
      AND user_id = NEW.user_id
      AND status = 'active';

    PERFORM set_config('app.syncing_from_journal', 'false', true);
  END IF;

  RETURN NEW;
END;
$function$;


-- ───────────────────────────────────────────────────────────────────────────
-- FUNCTION: sync_positions_to_journal
--   Fires when a positions row's stop_loss / entry_price / quantity changes
--   Writes corresponding updates to the most recent OPEN journal entry
--   for the same (user_id, security_id)
-- ───────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.sync_positions_to_journal()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  -- Short-circuit if the journal side is already driving this sync
  IF current_setting('app.syncing_from_journal', true) = 'true' THEN
    RETURN NEW;
  END IF;

  IF NEW.security_id IS NOT NULL AND NEW.security_id != '' THEN
    PERFORM set_config('app.syncing_from_positions', 'true', true);

    -- Match by position_id when linked (covers reopened trades where exits == initial_qty);
    -- fall back to security_id for unlinked trades.
    UPDATE journal_trades SET
      sl = CASE
        WHEN NEW.stop_loss IS DISTINCT FROM OLD.stop_loss AND NEW.stop_loss IS NOT NULL AND NEW.stop_loss > 0
        THEN NEW.stop_loss ELSE sl END,
      entry_price = CASE
        WHEN NEW.entry_price IS DISTINCT FROM OLD.entry_price AND NEW.entry_price IS NOT NULL AND NEW.entry_price > 0
        THEN NEW.entry_price ELSE entry_price END,
      avg_entry = CASE
        WHEN NEW.entry_price IS DISTINCT FROM OLD.entry_price AND NEW.entry_price IS NOT NULL AND NEW.entry_price > 0
        THEN NEW.entry_price ELSE avg_entry END,
      initial_qty = CASE
        WHEN NEW.quantity IS DISTINCT FROM OLD.quantity AND NEW.quantity IS NOT NULL AND NEW.quantity > 0
        THEN NEW.quantity ELSE initial_qty END,
      updated_at = NOW()
    WHERE user_id = NEW.user_id
      AND (
        position_id = NEW.id
        OR (position_id IS NULL AND security_id = NEW.security_id)
      );

    PERFORM set_config('app.syncing_from_positions', 'false', true);
  END IF;

  RETURN NEW;
END;
$function$;


-- ───────────────────────────────────────────────────────────────────────────
-- TRIGGERS
-- ───────────────────────────────────────────────────────────────────────────

-- Full UPDATE OF list — covers everything the sync function actually reads
-- from NEW/OLD. Keeps the DB-level safety net in lockstep with the Python
-- service (sync_journal_trade_to_position) so direct SQL edits or admin-tool
-- writes don't drift. Date-only and tsl-only edits previously wouldn't fire
-- this trigger; Python sync masked it, but only when writes went through the
-- API. No behaviour change for API traffic — purely defensive.
CREATE TRIGGER trg_sync_journal_to_positions
  AFTER UPDATE OF sl, tsl, entry_price, initial_qty,
                  e1_qty, e1_date, e2_qty, e2_date, e3_qty, e3_date
  ON public.journal_trades
  FOR EACH ROW
  EXECUTE FUNCTION sync_journal_to_positions();

CREATE TRIGGER trg_sync_positions_to_journal
  AFTER UPDATE OF stop_loss, entry_price, quantity
  ON public.positions
  FOR EACH ROW
  EXECUTE FUNCTION sync_positions_to_journal();


-- ═══════════════════════════════════════════════════════════════════════════
-- STRUCTURAL GUARD: auto-resolve security_id on every insert/update of
-- positions and journal_trades. Any code path (journal route, PM route,
-- Valvo AI v2–v5, direct SQL) gets self-heal — callers cannot forget to
-- bind it, which in turn keeps chart / CMP / MA pipelines working.
--
-- Why it exists: the GROWW / RAMCOIND / NBCC chart-blank bugs were all
-- the same shape — a writer forgot security_id and the row landed with
-- NULL. Rather than audit every writer forever, this trigger makes the
-- DB refuse to store an unresolved id when a resolution is available.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION public.resolve_security_id_from_universe(
  p_symbol text, p_name text
) RETURNS text
LANGUAGE sql
STABLE
AS $$
  WITH hits AS (
    SELECT security_id, 1 AS rank
    FROM public.stock_universe
    WHERE is_active = true
      AND p_symbol IS NOT NULL AND p_symbol <> ''
      AND UPPER(symbol) = UPPER(p_symbol)
    UNION ALL
    SELECT security_id, 2 AS rank
    FROM public.stock_universe
    WHERE is_active = true
      AND p_name IS NOT NULL AND LENGTH(p_name) >= 3
      AND company_name ILIKE ('%' || p_name || '%')
  )
  SELECT security_id FROM hits ORDER BY rank LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION public.trg_positions_resolve_security_id()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  -- Normalize empty string to NULL so all downstream checks can use IS NULL.
  IF NEW.security_id IS NOT NULL AND NEW.security_id = '' THEN
    NEW.security_id := NULL;
  END IF;
  IF NEW.security_id IS NULL AND NEW.stock_name IS NOT NULL THEN
    -- positions has no separate 'symbol' column; stock_name is both the
    -- display name AND the search key.
    NEW.security_id := public.resolve_security_id_from_universe(
      NEW.stock_name, NEW.stock_name
    );
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.trg_journal_resolve_security_id()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.security_id IS NOT NULL AND NEW.security_id = '' THEN
    NEW.security_id := NULL;
  END IF;
  IF NEW.security_id IS NULL AND (NEW.symbol IS NOT NULL OR NEW.name IS NOT NULL) THEN
    -- symbol is the exact match axis, name is fuzzy fallback.
    NEW.security_id := public.resolve_security_id_from_universe(NEW.symbol, NEW.name);
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_positions_resolve_security_id_ins ON public.positions;
CREATE TRIGGER trg_positions_resolve_security_id_ins
  BEFORE INSERT ON public.positions
  FOR EACH ROW EXECUTE FUNCTION public.trg_positions_resolve_security_id();

DROP TRIGGER IF EXISTS trg_positions_resolve_security_id_upd ON public.positions;
CREATE TRIGGER trg_positions_resolve_security_id_upd
  BEFORE UPDATE OF stock_name, security_id ON public.positions
  FOR EACH ROW
  WHEN (NEW.security_id IS NULL OR NEW.security_id = '')
  EXECUTE FUNCTION public.trg_positions_resolve_security_id();

DROP TRIGGER IF EXISTS trg_journal_resolve_security_id_ins ON public.journal_trades;
CREATE TRIGGER trg_journal_resolve_security_id_ins
  BEFORE INSERT ON public.journal_trades
  FOR EACH ROW EXECUTE FUNCTION public.trg_journal_resolve_security_id();

DROP TRIGGER IF EXISTS trg_journal_resolve_security_id_upd ON public.journal_trades;
CREATE TRIGGER trg_journal_resolve_security_id_upd
  BEFORE UPDATE OF symbol, name, security_id ON public.journal_trades
  FOR EACH ROW
  WHEN (NEW.security_id IS NULL OR NEW.security_id = '')
  EXECUTE FUNCTION public.trg_journal_resolve_security_id();


-- ═══════════════════════════════════════════════════════════════════════════
-- VERIFY (run after applying):
--
--   SELECT trigger_name, event_object_table
--   FROM information_schema.triggers
--   WHERE trigger_name IN (
--     'trg_sync_journal_to_positions',
--     'trg_sync_positions_to_journal',
--     'trg_positions_resolve_security_id_ins',
--     'trg_positions_resolve_security_id_upd',
--     'trg_journal_resolve_security_id_ins',
--     'trg_journal_resolve_security_id_upd'
--   );
--
-- Should return 6 rows.
-- ═══════════════════════════════════════════════════════════════════════════
