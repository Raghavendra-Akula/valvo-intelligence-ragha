from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable
from flask import g

from database.init_valvo_ai_v2_db import init_valvo_ai_v2_tables
from database.database import get_db

from services.journal_position_sync import (
    TRAILING_ACTIVE_MODES,
    sync_journal_trade_to_position,
    sync_position_to_journal,
)
from services import portfolio_capital_log as capital_log
from .utils import current_fy_start, to_jsonable


def _get_user_id():
    """Get user_id from Flask g, with clear error if missing."""
    try:
        uid = getattr(g, 'user_id', None)
        if uid:
            return uid
    except RuntimeError:
        pass
    raise RuntimeError("user_id not available — request context lost (SSE generator without stream_with_context?)")


class ValvoActionError(Exception):
    """Action-level error that carries a machine-actionable hint for the LLM.

    Plain ValueError gives the model a sentence; ValvoActionError gives it
    a decision tree. When the LLM sees
      {"ok": false, "error_code": "stock_already_held",
       "suggested_action": "pyramid_position", ...}
    it can retry the right tool without apologizing to the user first.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        suggested_action: str | None = None,
        suggested_payload: dict | None = None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.suggested_action = suggested_action
        self.suggested_payload = suggested_payload or {}
        self.details = details or {}

    def to_dict(self) -> dict:
        out = {
            "ok": False,
            "error": self.message,
            "error_code": self.error_code,
        }
        if self.suggested_action:
            out["suggested_action"] = self.suggested_action
        if self.suggested_payload:
            out["suggested_payload"] = self.suggested_payload
        if self.details:
            out["details"] = self.details
        return out


READ_ONLY_TABLES = {
    "legacy_trades",
    "legacy_trades_fy2223",
    "legacy_trades_fy2324",
    "legacy_trades_fy2324_backup",
    "legacy_trades_fy2425",
    "legacy_monthly_fy2223",
    "legacy_monthly_fy2324",
    "legacy_monthly_fy2425",
    "legacy_monthly_summary",
    "nexus_analytics",
    "nexus_monthly",
    "nexus_trades",
    "journal_trades_computed",
}

ALLOWED_WRITE_TABLES = {
    "positions",
    "journal_trades",
    "watchlists",
    "watchlist_items",
    "user_settings",
    "user_fy_config",
    "market_regime_history",
    "journal_settings",
    "journal_fund_months",
    "price_alerts",
}


def _db():
    return get_db()


_tables_initialized = False


def _ensure_support_tables():
    global _tables_initialized
    if _tables_initialized:
        return
    init_valvo_ai_v2_tables()
    _tables_initialized = True


def _find_active_position(cur, stock_name: str):
    cur.execute(
        """
        SELECT id, stock_name, security_id, entry_price, stop_loss, quantity, current_price,
               bucket_sold_pct, bucket_cap, sell_history, risk_pct
        FROM positions
        WHERE status = 'active' AND user_id = %s AND stock_name ILIKE %s
        ORDER BY stock_name
        """,
        (_get_user_id(), f"%{stock_name}%",),
    )
    rows = cur.fetchall()
    if not rows:
        return None, f"No active position found for {stock_name}"
    exact = [row for row in rows if row["stock_name"].lower() == stock_name.lower()]
    if len(exact) == 1:
        return exact[0], None
    if len(rows) > 1:
        names = ", ".join(row["stock_name"] for row in rows[:5])
        return None, f"Ambiguous position reference for {stock_name}: {names}"
    return rows[0], None


def _audit(cur, action_name: str, target_table: str, target_ref: str | None, request_text: str, payload, before_state, after_state, outcome: str):
    cur.execute(
        """
        INSERT INTO valvo_ai_v2_audit_log
            (action_name, target_table, target_ref, request_text, payload, before_state, after_state, outcome, user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            action_name,
            target_table,
            target_ref,
            request_text,
            json.dumps(to_jsonable(payload)),
            json.dumps(to_jsonable(before_state)) if before_state is not None else None,
            json.dumps(to_jsonable(after_state)) if after_state is not None else None,
            outcome,
            _get_user_id(),
        ),
    )


def _store_pending_action(cur, action_name: str, target_table: str, target_ref: str | None, request_text: str, payload, preview):
    pending_id = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO valvo_ai_v2_pending_actions
            (id, action_name, target_table, target_ref, request_text, payload, preview, user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            pending_id,
            action_name,
            target_table,
            target_ref,
            request_text,
            json.dumps(to_jsonable(payload)),
            json.dumps(to_jsonable(preview)),
            _get_user_id(),
        ),
    )
    return pending_id


def _load_pending_action(cur, pending_id: str):
    cur.execute(
        """
        SELECT id, action_name, target_table, target_ref, request_text, payload, preview
        FROM valvo_ai_v2_pending_actions
        WHERE id = %s AND user_id = %s AND expires_at > NOW()
        """,
        (pending_id, _get_user_id()),
    )
    row = cur.fetchone()
    if not row:
        return None
    row["payload"] = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
    row["preview"] = row["preview"] if isinstance(row["preview"], dict) else json.loads(row["preview"] or "{}")
    return row


def _delete_pending_action(cur, pending_id: str):
    cur.execute("DELETE FROM valvo_ai_v2_pending_actions WHERE id = %s", (pending_id,))


def _ensure_current_year_trade(payload):
    trade_date = payload.get("trade_date")
    if not trade_date:
        return None
    trade_dt = date.fromisoformat(trade_date)
    if trade_dt < current_fy_start():
        return "Journal trade writes are limited to the current financial year only"
    return None


def _preview_message(action_name: str, payload, reason: str):
    # Price alert preview: include direction + threshold so the user sees
    # exactly what they're about to arm ("Alert NALCO above Rs450").
    if action_name == "create_price_alert":
        sym = payload.get("symbol") or "?"
        cond = payload.get("condition") or "crosses"
        thr = payload.get("threshold")
        try:
            thr_fmt = f"Rs{float(thr):,.2f}" if thr is not None else "?"
        except (TypeError, ValueError):
            thr_fmt = str(thr)
        return f"Alert {sym} {cond} {thr_fmt} is staged. Confirmation is required because {reason}."
    if action_name == "set_fy_base_capital":
        amt = payload.get("base_capital")
        fy = payload.get("fy") or "current FY"
        try:
            amt_fmt = f"Rs{float(amt):,.0f}" if amt is not None else "?"
        except (TypeError, ValueError):
            amt_fmt = str(amt)
        return f"Base capital for FY {fy} will be set to {amt_fmt}. Confirmation is required because {reason}."
    if action_name == "pyramid_position":
        name = payload.get("stock_name") or "?"
        try:
            qty = int(payload.get("add_qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        try:
            price = f"Rs{float(payload.get('add_price')):,.2f}" if payload.get("add_price") is not None else "?"
        except (TypeError, ValueError):
            price = str(payload.get("add_price"))
        sl_note = ""
        if payload.get("new_stop_loss") is not None:
            try:
                sl_note = f" with SL Rs{float(payload['new_stop_loss']):,.2f}"
                if payload.get("trailing_mode") and payload["trailing_mode"] != "custom":
                    sl_note += f" ({payload['trailing_mode']})"
            except (TypeError, ValueError):
                sl_note = ""
        return (
            f"Pyramiding {name}: +{qty} shares @ {price}{sl_note}. "
            f"Confirmation is required because {reason}."
        )
    if action_name == "log_trade":
        name = payload.get("stock_name") or "?"
        try:
            qty = int(payload.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        try:
            entry = f"Rs{float(payload.get('entry_price')):,.2f}" if payload.get("entry_price") is not None else "?"
        except (TypeError, ValueError):
            entry = str(payload.get("entry_price"))
        sl_note = ""
        if payload.get("stop_loss") is not None:
            try:
                sl_note = f", SL Rs{float(payload['stop_loss']):,.2f}"
            except (TypeError, ValueError):
                sl_note = ""
        elif payload.get("risk_pct") is not None:
            try:
                sl_note = f", SL {float(payload['risk_pct']):.1f}% below entry"
            except (TypeError, ValueError):
                sl_note = ""
        if payload.get("exit_price") is not None:
            try:
                exit_p = f"Rs{float(payload['exit_price']):,.2f}"
            except (TypeError, ValueError):
                exit_p = str(payload["exit_price"])
            return (
                f"Round-trip {name}: BUY {qty} @ {entry}{sl_note}, then EXIT @ {exit_p}. "
                f"Confirmation is required because {reason}."
            )
        return (
            f"Buy {qty} {name} @ {entry}{sl_note}. "
            f"Confirmation is required because {reason}."
        )
    target = payload.get("stock_name") or payload.get("symbol") or payload.get("nsecode") or payload.get("regime") or "target"
    return f"{action_name} is staged for {target}. Confirmation is required because {reason}."


def _log_trade(cur, payload):
    """Single atomic write that adds a row to BOTH positions and
    journal_trades, pre-linked via position_id. This is the only write path
    Valvo AI uses to add a new trade — replaces the old create_position +
    create_journal_trade split that left half-written rows when one side
    succeeded and the other didn't.

    Architectural rules (FY26-27 lock):
      - stock_name MUST resolve to a single row in stock_universe — without
        a security_id the chart/MA/CMP pipeline silently breaks. We fail
        with `unresolvable_security` rather than persisting an orphan.
      - Ambiguous matches (DVR / partly-paid variants) raise
        `ambiguous_stock_reference` so the LLM asks the user to pick.
      - If the stock is already an active position, raise `stock_already_held`
        with a suggested pyramid_position payload — never silently duplicate.

    Same-day exit (the "exit gets logged as SL" bug):
      - Pass `exit_price` and we close the position AND fill journal.e1_*
        in the same commit. There is NO separate SL field for exits — exits
        are exits, full stop.
    """
    from .catalog import resolve_stock_reference_strict

    raw_name = (payload.get("stock_name") or "").strip()
    if not raw_name:
        raise ValueError("stock_name is required")

    try:
        entry_price = float(payload["entry_price"])
        quantity = int(payload["quantity"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("entry_price (number) and quantity (integer) are required")
    if entry_price <= 0 or quantity <= 0:
        raise ValueError("entry_price and quantity must be positive")

    # 1) Strict security_id resolution. The whole point of log_trade is that
    #    the AI cannot create a row without a real catalog hit.
    try:
        resolved = resolve_stock_reference_strict(cur, symbol_hint=raw_name)
    except Exception as resolve_err:
        print(f"[valvo_ai_v2] log_trade resolve failed for {raw_name}: {resolve_err}")
        resolved = None

    if isinstance(resolved, dict) and resolved.get("ambiguous"):
        matches = resolved.get("matches", [])
        preview = ", ".join(
            f"{m.get('symbol')} ({m.get('company_name')})"
            for m in matches[:4]
        )
        raise ValvoActionError(
            f"'{raw_name}' matches multiple stocks in the catalog: {preview}. "
            "Ask the user to pick one by exact NSE symbol.",
            error_code="ambiguous_stock_reference",
            details={
                "stock_name_given": raw_name,
                "matches": [
                    {
                        "security_id": str(m.get("security_id")),
                        "symbol": m.get("symbol"),
                        "company_name": m.get("company_name"),
                    }
                    for m in matches
                ],
            },
        )

    if not resolved or not resolved.get("security_id"):
        raise ValvoActionError(
            f"Could not resolve '{raw_name}' to a security in the catalog. "
            "Ask the user for the exact NSE symbol (e.g. RELIANCE, TCS) or "
            "full company name.",
            error_code="unresolvable_security",
            details={"stock_name_given": raw_name},
        )

    security_id = str(resolved["security_id"])
    canonical_name = resolved.get("company_name") or raw_name
    canonical_symbol = (resolved.get("symbol") or raw_name).upper()

    # 2) Reject duplicate active position — direct the AI to pyramid instead
    user_id = _get_user_id()
    cur.execute(
        "SELECT id, stock_name FROM positions "
        "WHERE user_id = %s AND security_id = %s AND status = 'active' "
        "LIMIT 1",
        (user_id, security_id),
    )
    existing = cur.fetchone()
    if existing:
        raise ValvoActionError(
            f"{existing['stock_name']} is already an active position. "
            "Use pyramid_position to add a leg, or close the existing one first.",
            error_code="stock_already_held",
            suggested_action="pyramid_position",
            suggested_payload={
                "stock_name": existing["stock_name"],
                "add_qty": quantity,
                "add_price": entry_price,
            },
            details={"existing_position_id": existing.get("id")},
        )

    # 3) Stop-loss: explicit value wins; otherwise default to 4% below entry
    if payload.get("stop_loss") is not None:
        stop_loss = float(payload["stop_loss"])
    else:
        risk_pct_arg = float(payload.get("risk_pct") or 4)
        stop_loss = round(entry_price * (1 - risk_pct_arg / 100), 2)
    if stop_loss <= 0 or stop_loss >= entry_price:
        raise ValueError("stop_loss must be positive and below entry_price")
    risk_pct = round((entry_price - stop_loss) / entry_price * 100, 2)

    trade_date = payload.get("trade_date") or date.today().isoformat()
    market_regime = payload.get("market_regime") or "bull"
    notes = payload.get("notes")
    setup = payload.get("setup")
    sector = payload.get("sector")
    entry_type = payload.get("entry_type") or "BREAKOUT"
    exit_price = payload.get("exit_price")
    exit_date = payload.get("exit_date") or trade_date
    exit_trigger = payload.get("exit_trigger")

    position_value = entry_price * quantity
    one_r = abs(entry_price - stop_loss) * quantity

    # Suppress the journal→positions sync trigger so the explicit journal
    # insert below doesn't try to mutate the position we're about to create.
    cur.execute("SET LOCAL app.syncing_from_positions = 'true'")

    # 4) INSERT positions row
    cur.execute(
        """
        INSERT INTO positions
            (stock_name, entry_price, initial_entry_price, stop_loss, quantity,
             position_value, one_r_value, risk_pct, source, market_regime,
             regime_source, current_price, leg_base_price, valvo_ref_price,
             security_id, status, total_cost_outlay, entry_date,
             created_at, updated_at, user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'valvo_ai_v2', %s, 'v2',
                %s, %s, %s, %s, 'active', %s, %s, NOW(), NOW(), %s)
        RETURNING id, stock_name, entry_price, stop_loss, quantity,
                  security_id, status
        """,
        (
            canonical_name, entry_price, entry_price, stop_loss, quantity,
            position_value, one_r, risk_pct, market_regime,
            entry_price, entry_price, entry_price, security_id,
            round(position_value, 2), trade_date, user_id,
        ),
    )
    position_row = cur.fetchone()
    position_id = position_row["id"]

    # 5) INSERT journal_trades row, pre-linked
    cur.execute(
        "SELECT COALESCE(MAX(trade_no), 0) + 1 AS next_trade_no "
        "FROM journal_trades WHERE user_id = %s",
        (user_id,),
    )
    trade_no = int(cur.fetchone()["next_trade_no"])

    cur.execute(
        """
        INSERT INTO journal_trades
            (trade_no, trade_date, symbol, name, entry_type, entry_price,
             avg_entry, sl, initial_qty, security_id, sector, notes, setup,
             position_id, user_id, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                NOW(), NOW())
        RETURNING id, trade_no, symbol, trade_date, entry_price, sl,
                  initial_qty, position_id, security_id
        """,
        (
            trade_no, trade_date, canonical_symbol, canonical_name,
            entry_type, entry_price, entry_price, stop_loss, quantity,
            security_id, sector, notes,
            json.dumps(setup) if setup is not None else None,
            position_id, user_id,
        ),
    )
    journal_row = cur.fetchone()

    # 6) Same-day round-trip: if the user said "I bought at X and exited at Y",
    #    close the position AND fill journal.e1_* in the same commit. This is
    #    the fix for "exit gets stored as SL" — exits go to e1_price, never sl.
    closed_same_day = False
    if exit_price is not None:
        try:
            ex = float(exit_price)
        except (TypeError, ValueError):
            raise ValueError("exit_price must be a number")
        if ex <= 0:
            raise ValueError("exit_price must be positive")
        total_pnl = (ex - entry_price) * quantity
        total_pnl_pct = ((ex - entry_price) / entry_price * 100) if entry_price else 0
        cur.execute(
            """
            UPDATE positions
            SET status = 'closed',
                exit_price = %s,
                exit_date = %s::timestamp,
                total_pnl = %s,
                total_pnl_pct = %s,
                updated_at = NOW()
            WHERE id = %s AND user_id = %s
            RETURNING id, stock_name, status, exit_price, total_pnl,
                      total_pnl_pct
            """,
            (ex, exit_date, total_pnl, total_pnl_pct, position_id, user_id),
        )
        position_row = cur.fetchone()

        cur.execute(
            """
            UPDATE journal_trades
            SET e1_price = %s,
                e1_qty   = %s,
                e1_date  = %s::date,
                exit_trigger = COALESCE(exit_trigger, %s),
                updated_at = NOW()
            WHERE id = %s AND user_id = %s
            RETURNING id, trade_no, e1_price, e1_qty, e1_date
            """,
            (ex, quantity, exit_date, exit_trigger or "same-day exit",
             journal_row["id"], user_id),
        )
        journal_row = cur.fetchone()
        closed_same_day = True

    return {
        "position": position_row,
        "journal_trade": journal_row,
        "security_id": security_id,
        "canonical_name": canonical_name,
        "canonical_symbol": canonical_symbol,
        "closed_same_day": closed_same_day,
    }


def _pyramid_position(cur, payload):
    """Add a pyramid leg to an existing active position.

    Mirrors POST /api/positions/<id>/pyramid so a manual pyramid from the UI
    and a Valvo-AI-driven pyramid produce identical rows (weighted-avg entry,
    pyramid_history slot p1/p2, journal_trades.avg_entry resync, max 2 legs).

    Optional fields:
      - add_date: ISO date for the leg; defaults to today.
      - new_stop_loss: if supplied, the new SL is applied in the same action
        (custom SL or trailing mode via trailing_mode). If omitted, the AI
        prompt is expected to ask the user for an SL before calling.
      - trailing_mode: 'custom', 'ema20', 'ema50', etc. Used alongside
        new_stop_loss — same semantics as update_stop_loss.
    """
    from datetime import datetime as _dt

    stock_name = payload["stock_name"].strip()
    add_qty = int(payload["add_qty"])
    add_price = float(payload["add_price"])
    add_date = payload.get("add_date")
    new_stop_loss = payload.get("new_stop_loss")
    trailing_mode = payload.get("trailing_mode")

    if add_qty <= 0 or add_price <= 0:
        raise ValueError("add_qty and add_price must be positive")

    position, error = _find_active_position(cur, stock_name)
    if error:
        raise ValueError(error)

    # Load the full row (find_active_position returns a subset)
    cur.execute("SELECT * FROM positions WHERE id = %s AND user_id = %s",
                (position["id"], _get_user_id()))
    pos = cur.fetchone()
    if not pos:
        raise ValueError(f"Position for {stock_name} vanished mid-request")
    if (pos.get("status") or "").lower() == "closed":
        raise ValueError(f"Cannot pyramid into a closed position ({pos['stock_name']})")
    # Snapshot before the UPDATE so run_action's audit trail gets a proper
    # before/after diff (matches update_stop_loss / record_sell / edit_position).
    before_snapshot = dict(pos)

    pyramid_history = pos.get("pyramid_history") or []
    if isinstance(pyramid_history, str):
        try:
            pyramid_history = json.loads(pyramid_history)
        except Exception:
            pyramid_history = []
    if len(pyramid_history) >= 2:
        raise ValvoActionError(
            f"{pos['stock_name']} is already at max 2 pyramids (P1, P2). "
            "Tell the user they must edit or delete a leg from Position Manager "
            "before another pyramid is possible.",
            error_code="pyramid_cap_reached",
            details={
                "stock_name": pos["stock_name"],
                "current_legs": len(pyramid_history),
                "max_legs": 2,
            },
        )

    old_qty = int(pos["quantity"])
    old_entry = float(pos["entry_price"])
    new_qty = old_qty + add_qty
    new_entry = round(((old_entry * old_qty) + (add_price * add_qty)) / new_qty, 2)
    new_value = round(new_entry * new_qty, 2)

    # Re-base risk_pct against the (possibly new) stop so one_r_value stays
    # meaningful after the entry shifts.
    risk_pct = float(pos.get("risk_pct") or 4.0)
    effective_sl = float(new_stop_loss) if new_stop_loss is not None else pos.get("stop_loss")
    if effective_sl and float(effective_sl) < new_entry:
        risk_pct = round((new_entry - float(effective_sl)) / new_entry * 100, 2)
    one_r_value = round(new_value * (risk_pct / 100.0), 2) if risk_pct else None

    # Re-base bucket_sold_pct — sells that happened before pyramiding are now
    # a smaller % of the enlarged position.
    sell_history = pos.get("sell_history") or []
    if isinstance(sell_history, str):
        try:
            sell_history = json.loads(sell_history)
        except Exception:
            sell_history = []
    sold_shares = sum(int(s.get("shares") or s.get("qty") or 0) for s in sell_history)
    new_bucket_pct = round((sold_shares / new_qty) * 100, 2) if new_qty else 0
    new_trail_pct = max(0.0, round(100.0 - new_bucket_pct, 2))

    now = _dt.now()
    next_slot = "p1" if len(pyramid_history) == 0 else "p2"
    iso_date = add_date or now.isoformat()[:10]
    pyramid_history.append({
        "date": iso_date,
        "price": add_price,
        "shares": add_qty,
        "slot": next_slot,
    })

    # Silence reverse trigger — we're authoritative on quantity/entry_price
    # from the new leg and don't want journal.initial_qty getting clobbered
    # with the new total (which would erase the original entry size).
    cur.execute("SET LOCAL app.syncing_from_journal = 'true'")

    # Apply SL in the same update if the user provided one. Trailing modes go
    # on trailing_sl; custom/explicit SL goes on stop_loss (same split as
    # update_stop_loss).
    sl_set_clause = ""
    sl_params = []
    if new_stop_loss is not None:
        mode = trailing_mode or "custom"
        if mode in TRAILING_ACTIVE_MODES:
            sl_set_clause = ", trailing_sl = %s, trailing_mode = %s"
            sl_params = [float(new_stop_loss), mode]
        else:
            sl_set_clause = ", stop_loss = %s, trailing_mode = %s"
            sl_params = [float(new_stop_loss), mode]

    # Bump lifetime cash-deployed counter (never subtracted — honest
    # denominator for return-on-full-capital on close).
    prior_cost_outlay = float(pos.get("total_cost_outlay") or (old_entry * old_qty))
    new_cost_outlay = round(prior_cost_outlay + (add_price * add_qty), 2)

    cur.execute(
        f"""
        UPDATE positions SET
            quantity = %s, entry_price = %s, position_value = %s,
            risk_pct = %s, one_r_value = %s,
            bucket_sold_pct = %s, trail_remaining_pct = %s,
            pyramid_history = %s,
            total_cost_outlay = %s,
            updated_at = NOW()
            {sl_set_clause}
        WHERE id = %s AND user_id = %s
        RETURNING id, stock_name, quantity, entry_price, position_value,
                  stop_loss, trailing_sl, trailing_mode
        """,
        [
            new_qty, new_entry, new_value,
            risk_pct, one_r_value,
            new_bucket_pct, new_trail_pct,
            json.dumps(pyramid_history),
            new_cost_outlay,
            *sl_params,
            pos["id"], _get_user_id(),
        ],
    )
    after = cur.fetchone()

    # Keep journal.avg_entry aligned with the new weighted entry. (Journal
    # pyramid legs themselves live on positions.pyramid_history — see
    # journal_position_sync docstring.)
    cur.execute(
        "SELECT id FROM journal_trades WHERE position_id = %s AND user_id = %s",
        (pos["id"], _get_user_id()),
    )
    jrow = cur.fetchone()
    if jrow:
        cur.execute(
            "UPDATE journal_trades SET avg_entry = %s, updated_at = NOW() "
            "WHERE id = %s AND user_id = %s",
            (new_entry, jrow["id"], _get_user_id()),
        )
        if new_stop_loss is not None:
            sync_position_to_journal(
                cur, _get_user_id(), pos["id"],
                {"stop_loss": float(new_stop_loss), "trailing_mode": trailing_mode or "custom"},
            )

    # run_action unpacks (before, after) for every action not in the
    # "pure-insert" list — returning a single dict here raises
    # "too many values to unpack" and surfaces as a blank reply in chat.
    return before_snapshot, after


def _update_stop_loss(cur, payload):
    position, error = _find_active_position(cur, payload["stock_name"])
    if error:
        raise ValueError(error)
    before = dict(position)
    new_stop = float(payload["new_stop_loss"])
    mode = payload.get("trailing_mode") or "custom"
    if mode in TRAILING_ACTIVE_MODES:
        cur.execute(
            """
            UPDATE positions
            SET trailing_sl = %s,
                trailing_mode = %s,
                updated_at = NOW()
            WHERE id = %s
            RETURNING id, stock_name, stop_loss, trailing_sl, trailing_mode
            """,
            (new_stop, mode, position["id"]),
        )
        after = cur.fetchone()
        sync_position_to_journal(cur, _get_user_id(), position["id"], {"trailing_sl": new_stop, "trailing_mode": mode})
        return before, after

    cur.execute(
        """
        UPDATE positions
        SET stop_loss = %s,
            trailing_mode = %s,
            updated_at = NOW()
        WHERE id = %s
        RETURNING id, stock_name, stop_loss, trailing_sl, trailing_mode
        """,
        (
            new_stop,
            mode,
            position["id"],
        ),
    )
    after = cur.fetchone()
    sync_position_to_journal(cur, _get_user_id(), position["id"], {"stop_loss": new_stop, "trailing_mode": mode})
    return before, after


def _find_journal_trade(cur, *, symbol: str | None = None, trade_no: int | None = None):
    if trade_no is not None:
        cur.execute(
            """
            SELECT *
            FROM journal_trades
            WHERE user_id = %s AND trade_no = %s
            LIMIT 1
            """,
            (_get_user_id(), int(trade_no)),
        )
        row = cur.fetchone()
        if row:
            return row, None

    symbol = (symbol or "").strip()
    if not symbol:
        return None, "symbol or trade_no is required"

    cur.execute(
        """
        SELECT *
        FROM journal_trades
        WHERE user_id = %s AND (UPPER(symbol) = UPPER(%s) OR UPPER(name) = UPPER(%s))
        ORDER BY
            CASE
                WHEN COALESCE(initial_qty, 0) > (
                    COALESCE(e1_qty, 0) + COALESCE(e2_qty, 0) + COALESCE(e3_qty, 0)
                ) THEN 0
                ELSE 1
            END,
            trade_date DESC NULLS LAST,
            id DESC
        LIMIT 2
        """,
        (_get_user_id(), symbol, symbol),
    )
    rows = cur.fetchall()
    if not rows:
        return None, f"No journal trade found for {symbol}"
    if len(rows) > 1:
        return None, f"Ambiguous journal reference for {symbol}. Please include trade_no."
    return rows[0], None


def _update_journal_stop(cur, payload):
    trade, error = _find_journal_trade(cur, symbol=payload.get("symbol"), trade_no=payload.get("trade_no"))
    if error:
        raise ValueError(error)
    before = dict(trade)

    fields = []
    values = []
    changed = set()

    if "sl" in payload:
        fields.append("sl = %s")
        values.append(float(payload["sl"]) if payload["sl"] not in (None, "") else None)
        changed.add("sl")
    if "tsl" in payload:
        fields.append("tsl = %s")
        values.append(float(payload["tsl"]) if payload["tsl"] not in (None, "") else None)
        changed.add("tsl")

    if not fields:
        raise ValueError("No journal stop fields were provided")

    fields.append("updated_at = NOW()")
    values.extend([trade["id"], _get_user_id()])
    cur.execute(
        f"""
        UPDATE journal_trades
        SET {', '.join(fields)}
        WHERE id = %s AND user_id = %s
        RETURNING *
        """,
        values,
    )
    after = cur.fetchone()
    sync_journal_trade_to_position(cur, _get_user_id(), after, changed_fields=changed)
    return before, after


def _edit_position(cur, payload):
    position, error = _find_active_position(cur, payload["stock_name"])
    if error:
        raise ValueError(error)
    allowed = {
        "entry_price",
        "stop_loss",
        "quantity",
        "market_regime",
        "ma_followed",
        "ma_grade",
        "shakeout_count",
        "qualifies_for_trailing",
        "bucket_sold_pct",
        "bucket_cap",
        "first_sell_done",
        "trailing_mode",
        "defensive_status",
        "risk_pct",
        "current_price",
        "leg_base_price",
        "valvo_ref_price",
        "security_id",
    }
    updates = {key: value for key, value in (payload.get("fields") or {}).items() if key in allowed}
    if not updates:
        raise ValueError("No valid position fields were provided")
    before = dict(position)
    set_parts = []
    values = []
    for key, value in updates.items():
        set_parts.append(f"{key} = %s")
        values.append(value)
    set_parts.append("updated_at = NOW()")
    values.append(position["id"])
    cur.execute(
        f"UPDATE positions SET {', '.join(set_parts)} WHERE id = %s RETURNING id, stock_name, entry_price, stop_loss, quantity, market_regime",
        values,
    )
    return before, cur.fetchone()


def _record_sell(cur, payload):
    position, error = _find_active_position(cur, payload["stock_name"])
    if error:
        raise ValueError(error)
    before = dict(position)
    history = position.get("sell_history") or []
    if isinstance(history, str):
        history = json.loads(history)
    sell_pct = float(payload["sell_pct"])
    sell_price = float(payload["sell_price"])
    history.append(
        {
            "date": date.today().isoformat(),
            "pct": sell_pct,
            "price": sell_price,
            "trigger": payload.get("trigger") or "manual",
        }
    )
    new_bucket = min(float(position.get("bucket_sold_pct") or 0) + sell_pct, 100)
    cur.execute(
        """
        UPDATE positions
        SET sell_history = %s,
            bucket_sold_pct = %s,
            first_sell_done = TRUE,
            updated_at = NOW()
        WHERE id = %s
        RETURNING id, stock_name, bucket_sold_pct, sell_history
        """,
        (json.dumps(history), new_bucket, position["id"]),
    )
    return before, cur.fetchone()


def _close_position(cur, payload):
    position, error = _find_active_position(cur, payload["stock_name"])
    if error:
        raise ValueError(error)
    before = dict(position)
    exit_price = float(payload.get("exit_price") or position.get("current_price") or position["entry_price"])
    entry_price = float(position.get("entry_price") or 0)
    quantity = int(position.get("quantity") or 0)
    total_pnl = (exit_price - entry_price) * quantity
    total_pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0
    cur.execute(
        """
        UPDATE positions
        SET status = 'closed',
            exit_price = %s,
            exit_date = NOW(),
            total_pnl = %s,
            total_pnl_pct = %s,
            updated_at = NOW()
        WHERE id = %s
        RETURNING id, stock_name, status, exit_price, total_pnl, total_pnl_pct
        """,
        (exit_price, total_pnl, total_pnl_pct, position["id"]),
    )
    return before, cur.fetchone()


def _delete_position(cur, payload):
    position, error = _find_active_position(cur, payload["stock_name"])
    if error:
        raise ValueError(error)
    before = dict(position)
    # Drop equity-curve log rows before the position itself so realized P&L
    # from this deleted trade doesn't keep inflating Portfolio Capital. FK
    # CASCADE covers this too; this matches position_routes.delete_position.
    capital_log.delete_for_position(cur, _get_user_id(), position["id"])
    cur.execute("DELETE FROM positions WHERE id = %s", (position["id"],))
    return before, {"deleted": True, "stock_name": position["stock_name"]}


# NOTE: the legacy _create_position and _create_journal_trade executors were
# removed in favour of the single _log_trade above. They were the root of the
# "Apollo / KRN / Data Patterns landed in journal but not positions" bug —
# the LLM could call one without the other, leaving orphan rows. The single
# log_trade path is atomic by construction: either both rows land or neither.


def _set_market_regime(cur, payload):
    # market_regime_history is a GLOBAL reference table (no user_id column).
    # Historical INSERTs with user_id silently failed; we now match the schema.
    cur.execute(
        """
        INSERT INTO market_regime_history (regime, note, updated_at)
        VALUES (%s, %s, NOW())
        RETURNING id, regime, note, updated_at
        """,
        (payload["regime"], payload.get("note") or ""),
    )
    return cur.fetchone()


def _create_price_alert(cur, payload):
    """Create a single price alert for the authenticated user. Resolves
    the symbol → security_id via stock_universe so the alert ties back
    to the same identifier the rest of the app uses. Returns only the
    `after` snapshot (no `before` — this is a pure insert)."""
    uid = _get_user_id()
    if not uid:
        raise ValueError("No authenticated user for price alert")

    symbol = (payload.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol is required")
    condition = (payload.get("condition") or "").strip().lower()
    if condition not in ("above", "below"):
        raise ValueError("condition must be 'above' or 'below'")
    try:
        threshold = float(payload.get("threshold"))
    except (TypeError, ValueError):
        raise ValueError("threshold must be a number")
    if threshold <= 0:
        raise ValueError("threshold must be positive")

    notes = (payload.get("notes") or "").strip() or None
    notification_method = (payload.get("notification_method") or "in_app").strip()

    # Resolve symbol → security_id. Fuzzy match so "NALCO", "NATIONALUM",
    # or "Nalco Limited" all map to the same row.
    cur.execute(
        "SELECT security_id, symbol, company_name "
        "FROM stock_universe "
        "WHERE symbol ILIKE %s OR company_name ILIKE %s "
        "ORDER BY CASE WHEN symbol ILIKE %s THEN 0 ELSE 1 END "
        "LIMIT 1",
        (symbol, f"%{symbol}%", symbol),
    )
    stock = cur.fetchone()
    if not stock:
        raise ValueError(f"No stock found for '{symbol}'")

    canonical_symbol = stock["symbol"]
    security_id = stock["security_id"]

    cur.execute(
        """
        INSERT INTO price_alerts
            (user_id, security_id, symbol, condition, threshold,
             active, triggered, trigger_count, recurring,
             cooldown_hours, notification_method, notes, created_at)
        VALUES (%s, %s, %s, %s, %s, true, false, 0, false, 24, %s, %s, NOW())
        RETURNING id, symbol, condition, threshold, active, created_at
        """,
        (uid, security_id, canonical_symbol, condition, threshold,
         notification_method, notes),
    )
    return cur.fetchone()


def _set_fy_base_capital(cur, payload):
    """Upsert base_capital for the authenticated user + target FY in
    user_fy_config (the source of truth after the capital consolidation).
    Defaults FY to the current one if unspecified. Returns the `after`
    row (pure-insert action — no `before`)."""
    uid = _get_user_id()
    if not uid:
        raise ValueError("No authenticated user for base-capital update")

    try:
        base_capital = float(payload.get("base_capital"))
    except (TypeError, ValueError):
        raise ValueError("base_capital must be a number")
    if base_capital < 0:
        raise ValueError("base_capital must be non-negative")

    fy = (payload.get("fy") or "").strip()
    if not fy:
        from datetime import date
        today = date.today()
        year = today.year if today.month >= 4 else today.year - 1
        end = str((year + 1) % 100).zfill(2)
        fy = f"{year}-{end}"

    from services.user_analytics_service import set_user_base_capital
    saved = set_user_base_capital(cur, uid, fy, base_capital)
    return {"fy": fy, "base_capital": saved}


def _update_user_settings(cur, payload):
    # Scope by caller (see commit 15f9ac1 for why "WHERE id = 1" was wrong).
    # base_capital is DELIBERATELY not in the accepted field list anymore —
    # it now lives in user_fy_config per-FY (commit ???). If the user asks
    # the AI "set my base capital to 5L", the AI should call a dedicated
    # per-FY update path, not shoehorn it into this cosmetic-settings write.
    fields = {}
    for key in ("display_name", "palette", "show_52w"):
        if key in payload:
            fields[key] = payload[key]

    # Friendly redirect if someone still tries to set base_capital via
    # this action — user sees an actionable error instead of a silent no-op.
    if "base_capital" in payload and not fields:
        raise ValueError(
            "Base capital is now per-FY. Use Settings → Capital to update "
            "the current FY's base capital."
        )

    if not fields:
        raise ValueError("No user settings fields were provided")

    uid = _get_user_id()
    if not uid:
        raise ValueError("No authenticated user for settings update")

    cur.execute("SELECT * FROM user_settings WHERE user_id = %s", (uid,))
    before = cur.fetchone() or {}
    set_parts = []
    values = []
    for key, value in fields.items():
        set_parts.append(f"{key} = %s")
        values.append(value)
    set_parts.append("updated_at = NOW()")
    values.append(uid)
    cur.execute(
        f"UPDATE user_settings SET {', '.join(set_parts)} WHERE user_id = %s RETURNING *",
        values,
    )
    return before, cur.fetchone()


@dataclass(frozen=True)
class ActionDefinition:
    name: str
    description: str
    input_schema: dict
    target_table: str
    executor: Callable
    # Optional contract fields — consumed by build_action_contracts_block()
    # to generate the "PLAYBOOK" section of the system prompt. Keeping them
    # co-located with the executor means the prompt can't drift from code.
    # Each field is optional so existing actions keep working without them.
    preconditions: tuple[str, ...] = ()
    common_failures: tuple[dict, ...] = ()   # [{"code": "...", "when": "...", "do": "..."}]
    related_actions: tuple[str, ...] = ()
    example: str | None = None


ACTIONS = {
    "log_trade": ActionDefinition(
        name="log_trade",
        description=(
            "Log a new trade — atomically writes to BOTH positions and "
            "journal_trades, pre-linked. This is the ONLY way to add a new "
            "trade. Use it for: new positions ('buy 10 TCS at 3500'), "
            "same-day round trips ('I bought X at A and exited at B'), "
            "and any backdated entry within the current FY. Refuses to "
            "write if stock_name doesn't resolve to stock_universe."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "stock_name":  {"type": "string", "description": "Free text or NSE symbol (e.g. 'TCS', 'Tata Consultancy', 'RELIANCE'). Must resolve to one row in stock_universe."},
                "entry_price": {"type": "number"},
                "quantity":    {"type": "integer"},
                "stop_loss":   {"type": "number", "description": "Optional. Defaults to 4% below entry_price."},
                "risk_pct":    {"type": "number", "description": "Optional. Used only if stop_loss is omitted (defaults to 4)."},
                "trade_date":  {"type": "string", "description": "ISO date YYYY-MM-DD. Defaults to today."},
                "exit_price":  {"type": "number", "description": "If present, the position is opened AND closed in the same write — used for same-day round trips. Goes to journal.e1_price, NOT to sl."},
                "exit_date":   {"type": "string", "description": "ISO date for the exit. Defaults to trade_date."},
                "exit_trigger":{"type": "string", "description": "Why the same-day exit happened (e.g. 'breakout failed', 'tagged SL'). Optional."},
                "entry_type":  {"type": "string", "description": "BREAKOUT | PULLBACK | etc. Defaults to BREAKOUT."},
                "market_regime":{"type": "string"},
                "sector":      {"type": "string"},
                "notes":       {"type": "string"},
                "setup":       {"type": "object", "description": "Optional JSON describing the setup (chart pattern, EMA, etc.)."},
            },
            "required": ["stock_name", "entry_price", "quantity"],
        },
        target_table="positions",
        executor=_log_trade,
        preconditions=(
            "stock_name MUST resolve to a single row in stock_universe — without security_id the chart/MA/CMP pipeline silently breaks.",
            "The stock is NOT already in the user's active positions. If it is, use pyramid_position instead.",
            "Same-day exit: if the user mentions both an entry AND an exit in one breath ('bought at 100, exited at 105'), pass exit_price — do NOT put the exit value in stop_loss. SL and exit are different concepts.",
        ),
        common_failures=(
            {"code": "unresolvable_security", "when": "stock_name doesn't match stock_universe", "do": "ask the user for the exact NSE symbol (e.g. RELIANCE, TCS) or full company name"},
            {"code": "ambiguous_stock_reference", "when": "the name matches multiple rows (DVR + regular share class, or two distinct companies)", "do": "list details.matches symbols and ask the user to pick one"},
            {"code": "stock_already_held", "when": "stock is already an active position", "do": "call pyramid_position with suggested_payload"},
        ),
        related_actions=("pyramid_position", "close_position", "record_sell"),
        example='User: "buy 10 TCS at 3500 with 4% SL" → log_trade(stock_name="TCS", quantity=10, entry_price=3500, risk_pct=4). User: "I bought NALCO at 410 and exited same day at 415" → log_trade(stock_name="NALCO", entry_price=410, quantity=N, exit_price=415).',
    ),
    "pyramid_position": ActionDefinition(
        name="pyramid_position",
        description=(
            "Add a pyramid leg (P1 or P2) to an EXISTING active position. "
            "Use this — not create_position — when the user asks to add to a "
            "stock they already hold. Max 2 legs per position. Optionally "
            "updates stop loss in the same call (new_stop_loss + trailing_mode)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "stock_name": {"type": "string"},
                "add_qty": {"type": "integer"},
                "add_price": {"type": "number"},
                "add_date": {"type": "string", "description": "ISO date (YYYY-MM-DD). Defaults to today."},
                "new_stop_loss": {"type": "number", "description": "Optional new SL applied atomically."},
                "trailing_mode": {"type": "string", "description": "custom | ema20 | ema50 | ema200 — same semantics as update_stop_loss."},
            },
            "required": ["stock_name", "add_qty", "add_price"],
        },
        target_table="positions",
        executor=_pyramid_position,
        preconditions=(
            "The stock IS already in the user's active positions (see LIVE STATE).",
            "Current leg count is < 2. Each position caps at P1 + P2.",
            "Ask the user for SL treatment (keep existing / fixed level / EMA trail) BEFORE staging if they haven't specified.",
        ),
        common_failures=(
            {"code": "no_active_position", "when": "the stock isn't in the book yet", "do": "offer log_trade instead"},
            {"code": "pyramid_cap_reached", "when": "P1 and P2 are both filled", "do": "tell the user to edit a leg in Position Manager"},
            {"code": "ambiguous_stock_reference", "when": "the name matches multiple holdings", "do": "ask the user which one"},
        ),
        related_actions=("log_trade", "update_stop_loss", "get_positions"),
        example='User holds Ather Energy; says "add 3 more at cmp with existing SL" → pyramid_position(stock_name="Ather Energy", add_qty=3, add_price=<cmp>) with new_stop_loss omitted.',
    ),
    "update_stop_loss": ActionDefinition(
        name="update_stop_loss",
        description="Update the stop loss for an active position.",
        input_schema={
            "type": "object",
            "properties": {
                "stock_name": {"type": "string"},
                "new_stop_loss": {"type": "number"},
                "trailing_mode": {"type": "string"},
            },
            "required": ["stock_name", "new_stop_loss"],
        },
        target_table="positions",
        executor=_update_stop_loss,
        preconditions=(
            "The stock IS in the user's active positions.",
            "trailing_mode is one of: custom | ema20 | ema50 | ema200. 'custom' writes to stop_loss; EMA modes write to trailing_sl + trailing_mode.",
            "If the user says 'trail SL with 5MA' or similar, pick the matching ema mode — do NOT hardcode a price.",
            "For EMA modes, do NOT call get_live_market first. The backend computes the EMA from trailing_mode; passing any numeric new_stop_loss value is fine (the backend recomputes).",
        ),
        common_failures=(
            {"code": "no_active_position", "when": "the stock isn't in the book", "do": "tell the user; don't create one to update its SL"},
            {"code": "ambiguous_stock_reference", "when": "the name matches multiple holdings", "do": "ask which one"},
        ),
        related_actions=("pyramid_position", "get_positions"),
    ),
    "edit_position": ActionDefinition(
        name="edit_position",
        description="Edit safe mutable fields on an active position.",
        input_schema={
            "type": "object",
            "properties": {"stock_name": {"type": "string"}, "fields": {"type": "object"}},
            "required": ["stock_name", "fields"],
        },
        target_table="positions",
        executor=_edit_position,
    ),
    "record_sell": ActionDefinition(
        name="record_sell",
        description="Record a partial sell on an active position.",
        input_schema={
            "type": "object",
            "properties": {
                "stock_name": {"type": "string"},
                "sell_pct": {"type": "number"},
                "sell_price": {"type": "number"},
                "trigger": {"type": "string"},
            },
            "required": ["stock_name", "sell_pct", "sell_price"],
        },
        target_table="positions",
        executor=_record_sell,
        preconditions=(
            "The stock IS in the user's active positions.",
            "sell_pct is the percentage of CURRENT remaining shares (not original quantity). If the user says 'sell half', pass 50 — the backend computes shares against what's left.",
            "For a full exit use close_position, not record_sell with sell_pct=100.",
        ),
        common_failures=(
            {"code": "no_active_position", "when": "the stock isn't in the book", "do": "tell the user"},
        ),
        related_actions=("close_position", "get_positions"),
    ),
    "close_position": ActionDefinition(
        name="close_position",
        description="Close an active position completely.",
        input_schema={
            "type": "object",
            "properties": {"stock_name": {"type": "string"}, "exit_price": {"type": "number"}},
            "required": ["stock_name"],
        },
        target_table="positions",
        executor=_close_position,
    ),
    "delete_position": ActionDefinition(
        name="delete_position",
        description="Delete an active position from the operational book.",
        input_schema={
            "type": "object",
            "properties": {"stock_name": {"type": "string"}},
            "required": ["stock_name"],
        },
        target_table="positions",
        executor=_delete_position,
    ),
    "update_journal_stop": ActionDefinition(
        name="update_journal_stop",
        description="Update SL and/or TSL on a current-year journal trade and sync the linked live position.",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "trade_no": {"type": "integer"},
                "sl": {"type": "number"},
                "tsl": {"type": "number"},
            },
        },
        target_table="journal_trades",
        executor=_update_journal_stop,
    ),
    "set_market_regime": ActionDefinition(
        name="set_market_regime",
        description="Append a new market regime state to market_regime_history.",
        input_schema={
            "type": "object",
            "properties": {"regime": {"type": "string"}, "note": {"type": "string"}},
            "required": ["regime"],
        },
        target_table="market_regime_history",
        executor=_set_market_regime,
    ),
    "update_user_settings": ActionDefinition(
        name="update_user_settings",
        description=(
            "Update cosmetic user settings (display_name, palette, show_52w). "
            "Does NOT accept base_capital — that's stored per-FY in "
            "user_fy_config and editable via Settings > Capital."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "display_name": {"type": "string"},
                "palette":      {"type": "string"},
                "show_52w":     {"type": "boolean"},
            },
        },
        target_table="user_settings",
        executor=_update_user_settings,
    ),
    "set_fy_base_capital": ActionDefinition(
        name="set_fy_base_capital",
        description=(
            "Set the base capital for a specific financial year. Source of "
            "truth for all capital / risk / position-sizing calculations. "
            "Use when the user says \"set my base capital to X\" / \"update "
            "FY24-25 starting capital to Rs18L\". Defaults to the current FY "
            "if unspecified. Requires confirmation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "base_capital": {"type": "number", "description": "Starting capital in ₹."},
                "fy":           {"type": "string",  "description": "FY label like '2026-27'. Defaults to current FY."},
            },
            "required": ["base_capital"],
        },
        target_table="user_fy_config",
        executor=lambda cur, payload: _set_fy_base_capital(cur, payload),
    ),
    "create_price_alert": ActionDefinition(
        name="create_price_alert",
        description=(
            "Create a price alert that fires when the stock crosses a "
            "threshold. Use when the user asks \"alert me if X hits Y\" "
            "/ \"notify me when X drops below Y\" or when the assistant "
            "suggests an alert in a narrative answer. Requires confirmation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol":    {"type": "string", "description": "NSE ticker or fragment (ILIKE fuzzy)."},
                "condition": {"type": "string", "enum": ["above", "below"], "description": "Trigger direction."},
                "threshold": {"type": "number", "description": "Price at which the alert fires (Rs)."},
                "notes":     {"type": "string", "description": "Optional reason/context shown with the alert."},
            },
            "required": ["symbol", "condition", "threshold"],
        },
        target_table="price_alerts",
        executor=_create_price_alert,
    ),
}


def build_action_playbook_block() -> str:
    """Render a 'PLAYBOOK' prompt section from the contract fields on each
    ActionDefinition. The output is code-derived, so new actions / rule
    changes show up in the prompt automatically — no hand-written prose to
    drift from the implementation.
    """
    lines = ["ACTION PLAYBOOK (read before calling any action tool):"]
    for name, d in ACTIONS.items():
        if not (d.preconditions or d.common_failures or d.related_actions or d.example):
            continue  # skip actions without contracts — baseline tool-desc is enough
        lines.append(f"\n{name}:")
        if d.preconditions:
            lines.append("  preconditions:")
            for p in d.preconditions:
                lines.append(f"    - {p}")
        if d.common_failures:
            lines.append("  failures:")
            for f in d.common_failures:
                code = f.get("code", "?")
                when = f.get("when", "")
                do = f.get("do", "")
                lines.append(f"    - {code}: if {when} → {do}")
        if d.related_actions:
            lines.append(f"  related: {', '.join(d.related_actions)}")
        if d.example:
            lines.append(f"  example: {d.example}")
    return "\n".join(lines) + "\n"


def get_action_tools():
    tools = []
    for definition in ACTIONS.values():
        tools.append(
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
        )
    return tools


def _needs_confirmation(action_name: str, payload: dict):
    if action_name == "log_trade":
        # Distinguish open vs round-trip in the message — same confirmation
        # gate, different framing so the user knows what they're approving.
        if payload.get("exit_price") is not None:
            return "it logs a complete round-trip (open + close) to your book"
        return "it adds a new position to your book"
    if action_name == "pyramid_position":
        return "it adds a pyramid leg — changes quantity and weighted avg entry"
    if action_name in {"close_position", "delete_position"}:
        return "it changes or deletes an open position"
    if action_name == "record_sell" and float(payload.get("sell_pct") or 0) >= 25:
        return "it sells a meaningful part of the position"
    if action_name == "create_price_alert":
        # Always confirm — alerts fire notifications / emails, so we want the
        # user to sanity-check the symbol and threshold before it lands.
        return "it creates a price alert that will notify you when triggered"
    if action_name == "set_fy_base_capital":
        # Capital changes cascade through every position-sizing / R-multiple
        # calculation downstream. Users should see the exact value staged
        # before it lands.
        return "changing base capital updates every position-sizing calc downstream"
    return None


def _prevalidate_action(cur, action_name: str, payload: dict):
    """Raise ValvoActionError with a machine-readable hint when a staged
    action can't proceed. The LLM branches on error_code and retries the
    suggested_action rather than narrating the failure to the user."""
    if action_name in {"update_stop_loss", "edit_position", "record_sell", "close_position", "delete_position", "pyramid_position"}:
        name = (payload.get("stock_name") or "").strip()
        position, error = _find_active_position(cur, name)
        if error:
            # "no active position" is the most common — hint toward log_trade
            # so the LLM can offer it rather than asking "which stock?".
            if "No active position" in error:
                raise ValvoActionError(
                    error,
                    error_code="no_active_position",
                    suggested_action="log_trade",
                    details={"stock_name_searched": name},
                )
            # Ambiguous match — hint the LLM to disambiguate with the user.
            raise ValvoActionError(
                error,
                error_code="ambiguous_stock_reference",
                details={"stock_name_searched": name},
            )
    if action_name == "log_trade":
        # Prevent staging a duplicate. Without this the confirm modal pops,
        # the user confirms, and only then does _log_trade raise the
        # "already exists" error — by which point the UX is wasted. The
        # executor enforces this too (defence in depth), but catching it
        # before the confirm step is much friendlier.
        name = (payload.get("stock_name") or "").strip()
        if name:
            existing, _ = _find_active_position(cur, name)
            if existing:
                raise ValvoActionError(
                    f"{existing['stock_name']} is already an active position. "
                    f"Use pyramid_position to add a leg.",
                    error_code="stock_already_held",
                    suggested_action="pyramid_position",
                    suggested_payload={
                        "stock_name": existing["stock_name"],
                        "add_qty": payload.get("quantity"),
                        "add_price": payload.get("entry_price"),
                    },
                    details={"existing_position_id": existing.get("id")},
                )
    return None


def _maybe_fire_rationale_prompt(cur, action_name: str, after) -> None:
    """Hand off to rationale_service when a position closes. We only fire
    on close events: explicit close_position, or log_trade with a same-day
    exit. record_sell DOESN'T close (it just trims), so it's excluded.

    Swallows all errors — the rationale loop is a coaching layer, not a
    correctness layer. A bug here must never break the user's trade close.
    """
    try:
        position_id = None
        if action_name == "close_position" and after:
            # _close_position returns a row with id, stock_name, status, ...
            position_id = after.get("id") if isinstance(after, dict) else None
        elif action_name == "log_trade" and isinstance(after, dict):
            # _log_trade returns {"position": {...}, "closed_same_day": bool, ...}
            if after.get("closed_same_day"):
                pos = after.get("position") or {}
                position_id = pos.get("id")
        if not position_id:
            return
        from services.rationale_service import maybe_create_prompt
        user_id = _get_user_id()
        if not user_id:
            return
        prompt_id = maybe_create_prompt(cur, user_id, int(position_id))
        if prompt_id:
            print(f"[rationale] created prompt {prompt_id} for position {position_id} ({action_name})")
    except Exception as exc:
        print(f"[rationale] hook failed (swallowed): {exc}")


def run_action(action_name: str, payload: dict | None, request_text: str = "", force_execute: bool = False):
    _ensure_support_tables()
    definition = ACTIONS.get(action_name)
    if not definition:
        return {"ok": False, "error": f"Unknown action: {action_name}"}

    if definition.target_table in READ_ONLY_TABLES or definition.target_table not in ALLOWED_WRITE_TABLES:
        return {"ok": False, "error": f"{definition.target_table} is read-only in Valvo AI v2"}

    payload = payload or {}
    boundary_error = _ensure_current_year_trade(payload) if action_name == "log_trade" else None
    if boundary_error:
        return {"ok": False, "error": boundary_error}

    conn = _db()
    if not conn:
        return {"ok": False, "error": "Database unavailable"}

    try:
        cur = conn.cursor()
        # _prevalidate_action raises ValvoActionError for structured hints;
        # the outer except ValvoActionError below turns it into the canonical
        # tool-result shape the LLM can branch on.
        _prevalidate_action(cur, action_name, payload)
        confirm_reason = None if force_execute else _needs_confirmation(action_name, payload)
        if confirm_reason:
            pending_id = _store_pending_action(
                cur,
                action_name,
                definition.target_table,
                payload.get("stock_name") or payload.get("symbol") or payload.get("nsecode"),
                request_text,
                payload,
                {
                    "message": _preview_message(action_name, payload, confirm_reason),
                    "reason": confirm_reason,
                    "payload": to_jsonable(payload),
                },
            )
            conn.commit()
            return {
                "ok": True,
                "executed": False,
                "requires_confirmation": True,
                "pending_action": {
                    "id": pending_id,
                    "action_name": action_name,
                    "target_table": definition.target_table,
                    "target_ref": payload.get("stock_name") or payload.get("symbol") or payload.get("nsecode"),
                    "reason": confirm_reason,
                    "payload": to_jsonable(payload),
                },
                "message": _preview_message(action_name, payload, confirm_reason),
            }

        if action_name in ("log_trade",
                           "set_market_regime", "create_price_alert",
                           "set_fy_base_capital"):
            # Pure-insert actions: executor returns the `after` row only.
            after = definition.executor(cur, payload)
            before = None
        else:
            before, after = definition.executor(cur, payload)
        _audit(
            cur,
            action_name,
            definition.target_table,
            payload.get("stock_name") or payload.get("symbol") or payload.get("nsecode"),
            request_text,
            payload,
            before,
            after,
            "executed",
        )
        # Behavioral coaching hook — fire AFTER the executor + audit but
        # BEFORE the commit, so the rationale-prompt INSERT is in the same
        # transaction as the trade close. Failures here are swallowed by
        # the service so a coaching bug never blocks an actual trade.
        _maybe_fire_rationale_prompt(cur, action_name, after)
        conn.commit()
        return {
            "ok": True,
            "executed": True,
            "requires_confirmation": False,
            "message": f"{action_name} executed successfully.",
            "target_table": definition.target_table,
            "after": to_jsonable(after),
        }
    except ValvoActionError as exc:
        # Structured error — the LLM gets error_code + suggested_action so
        # it can retry the right tool without apologizing to the user.
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[run_action] {action_name} rejected: {exc.error_code} — {exc}")
        return exc.to_dict()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[run_action] {action_name} failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "error_code": "unexpected"}
    finally:
        conn.close()


def confirm_pending_action(pending_id: str, request_text: str = ""):
    _ensure_support_tables()
    conn = _db()
    if not conn:
        return {"ok": False, "error": "Database unavailable"}
    try:
        cur = conn.cursor()
        pending = _load_pending_action(cur, pending_id)
        if not pending:
            return {"ok": False, "error": "Pending action not found or expired"}
        _delete_pending_action(cur, pending_id)
        conn.commit()
    finally:
        conn.close()
    return run_action(pending["action_name"], pending["payload"], request_text=request_text or pending.get("request_text") or "", force_execute=True)


def cancel_pending_action(pending_id: str):
    _ensure_support_tables()
    conn = _db()
    if not conn:
        return {"ok": False, "error": "Database unavailable"}
    try:
        cur = conn.cursor()
        pending = _load_pending_action(cur, pending_id)
        if not pending:
            return {"ok": False, "error": "Pending action not found or expired"}
        _delete_pending_action(cur, pending_id)
        _audit(
            cur,
            pending["action_name"],
            pending["target_table"],
            pending.get("target_ref"),
            pending.get("request_text") or "",
            pending.get("payload") or {},
            None,
            None,
            "cancelled",
        )
        conn.commit()
        return {
            "ok": True,
            "cancelled": True,
            "message": f"Cancelled {pending['action_name']}.",
        }
    finally:
        conn.close()
