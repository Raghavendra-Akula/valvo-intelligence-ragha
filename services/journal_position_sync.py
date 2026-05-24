"""
journal_position_sync.py

Shared helpers to keep live positions and journal trades aligned.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable


TRAILING_ACTIVE_MODES = {"cost", "5ma", "10ma", "20ma", "custom"}

# Phase 2a: pyramid fields no longer live on journal_trades. Sync only cares
# about exit slots and entry-related fields that remain on journal.
EXIT_FIELDS = {
    "e1_price", "e1_qty", "e1_date",
    "e2_price", "e2_qty", "e2_date",
    "e3_price", "e3_qty", "e3_date",
    "initial_qty", "avg_entry", "entry_price",
}


def _open_trade_order_sql() -> str:
    # Pyramid legs live on positions.pyramid_history now — to tell whether a
    # journal trade is "open" we compare initial_qty against its exits only.
    # A trade with exits < initial_qty is still open. Pyramids don't affect
    # open/closed status from the journal's point of view.
    return """
        ORDER BY
            CASE
                WHEN COALESCE(initial_qty, 0) > (
                    COALESCE(e1_qty, 0) + COALESCE(e2_qty, 0) + COALESCE(e3_qty, 0)
                ) THEN 0
                ELSE 1
            END,
            trade_date DESC NULLS LAST,
            id DESC
    """


def find_matching_position(cur, user_id: str, *, symbol: str | None = None, name: str | None = None, security_id: str | None = None):
    """Find the best active position match for a journal trade."""
    if security_id:
        cur.execute(
            """
            SELECT id, stock_name, security_id
            FROM positions
            WHERE status = 'active' AND user_id = %s AND security_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, security_id),
        )
        row = cur.fetchone()
        if row:
            return row

    for candidate in (symbol, name):
        if not candidate:
            continue
        cur.execute(
            """
            SELECT id, stock_name, security_id
            FROM positions
            WHERE status = 'active' AND user_id = %s AND LOWER(stock_name) = LOWER(%s)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, candidate),
        )
        row = cur.fetchone()
        if row:
            return row
    return None


def ensure_trade_position_link(cur, user_id: str, trade_row: dict) -> int | None:
    """Backfill position_id on a journal trade when we can confidently match it."""
    position_id = trade_row.get("position_id")
    if position_id:
        return position_id

    match = find_matching_position(
        cur,
        user_id,
        symbol=trade_row.get("symbol"),
        name=trade_row.get("name"),
        security_id=trade_row.get("security_id"),
    )
    if not match:
        return None

    cur.execute(
        """
        UPDATE journal_trades
        SET position_id = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND user_id = %s
        """,
        (match["id"], trade_row["id"], user_id),
    )
    trade_row["position_id"] = match["id"]
    return match["id"]


def ensure_position_trade_link(cur, user_id: str, pos_id: int) -> int | None:
    """Backfill journal.position_id for a position if a matching trade exists."""
    cur.execute(
        """
        SELECT id
        FROM journal_trades
        WHERE position_id = %s AND user_id = %s
        ORDER BY trade_date DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        (pos_id, user_id),
    )
    linked = cur.fetchone()
    if linked:
        return linked["id"]

    cur.execute(
        """
        SELECT id, stock_name, security_id
        FROM positions
        WHERE id = %s AND user_id = %s
        """,
        (pos_id, user_id),
    )
    position = cur.fetchone()
    if not position:
        return None

    if position.get("security_id"):
        cur.execute(
            f"""
            SELECT id
            FROM journal_trades
            WHERE user_id = %s AND position_id IS NULL AND security_id = %s
            {_open_trade_order_sql()}
            LIMIT 1
            """,
            (user_id, position["security_id"]),
        )
        match = cur.fetchone()
        if match:
            cur.execute(
                """
                UPDATE journal_trades
                SET position_id = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND user_id = %s
                """,
                (pos_id, match["id"], user_id),
            )
            return match["id"]

    cur.execute(
        f"""
        SELECT id
        FROM journal_trades
        WHERE user_id = %s AND position_id IS NULL
          AND (LOWER(symbol) = LOWER(%s) OR LOWER(name) = LOWER(%s))
        {_open_trade_order_sql()}
        LIMIT 1
        """,
        (user_id, position["stock_name"], position["stock_name"]),
    )
    match = cur.fetchone()
    if match:
        cur.execute(
            """
            UPDATE journal_trades
            SET position_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND user_id = %s
            """,
            (pos_id, match["id"], user_id),
        )
        return match["id"]
    return None


def _num(val):
    try:
        if val in (None, ""):
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _build_sell_history_from_trade(trade_row: dict, entry_price: float | None, total_bought_qty: float) -> tuple[list[dict], float]:
    """Rebuild positions.sell_history JSONB from journal E1/E2/E3 slots (sells only — P1/P2 are pyramids).

    Returns (sell_history, total_exited_qty).
    Percent is computed against total_bought_qty (initial + pyramids).
    """
    slots = [
        ("e1", "extension_milestone"),
        ("e2", "extension_milestone"),
        ("e3", "extension_milestone"),
    ]
    history: list[dict] = []
    total_qty = 0.0

    for prefix, default_trigger in slots:
        price = _num(trade_row.get(f"{prefix}_price"))
        qty = _num(trade_row.get(f"{prefix}_qty"))
        date = trade_row.get(f"{prefix}_date")
        if not price or not qty:
            continue
        pct = round((qty / total_bought_qty) * 100, 2) if total_bought_qty > 0 else 0
        profit = round((price - (entry_price or 0)) * qty, 2) if entry_price else 0
        extension = round(((price - entry_price) / entry_price * 100), 2) if entry_price else 0
        iso_date: str
        if hasattr(date, "isoformat"):
            iso_date = date.isoformat()
        elif date:
            iso_date = str(date)
        else:
            iso_date = datetime.now().isoformat()
        history.append({
            "date": iso_date,
            "pct": pct,
            "price": price,
            "extension": extension,
            "r_multiple": 0,
            "trigger": default_trigger,
            "shares": int(qty),
            "profit": profit,
        })
        total_qty += qty

    return history, total_qty


def _compute_pyramid_state(trade_row: dict, pyramid_history=None) -> tuple[float, float]:
    """Return (total_bought_qty, weighted_avg_entry) from initial leg + pyramid_history.

    Phase 2a: pyramid_history is a JSONB array on positions with shape
    [{date, price, shares, slot}]. Passed in from the caller (who already has
    the linked position row). Falls back to empty if not provided — only the
    initial leg contributes in that case.
    """
    initial_qty = _num(trade_row.get("initial_qty")) or 0
    initial_price = _num(trade_row.get("entry_price")) or _num(trade_row.get("avg_entry")) or 0

    legs = [(initial_qty, initial_price)]
    if pyramid_history:
        if isinstance(pyramid_history, str):
            try:
                pyramid_history = json.loads(pyramid_history)
            except Exception:
                pyramid_history = []
        for leg in pyramid_history or []:
            q = _num(leg.get("shares"))
            p = _num(leg.get("price"))
            if q and p:
                legs.append((q, p))

    total_qty = sum(q for q, _ in legs)
    if total_qty <= 0:
        return 0, initial_price
    weighted = sum(q * p for q, p in legs) / total_qty
    return total_qty, round(weighted, 2)


def sync_journal_trade_to_position(cur, user_id: str, trade_row: dict, changed_fields: Iterable[str] | None = None) -> int | None:
    """Push journal updates into the linked live position.

    - SL / TSL edits propagate to positions.stop_loss / trailing_sl
    - E1/E2/E3 edits rebuild positions.sell_history + bucket_sold_pct; auto-closes
      when total sold shares >= total bought shares (initial + P1 + P2)
    - P1/P2 (pyramids) edits update positions.quantity + entry_price (weighted avg)
    - Editing exits back to empty re-opens a closed position
    """
    changed = set(changed_fields or [])
    watched = {"sl", "tsl", "symbol", "name", "security_id", "position_id"} | EXIT_FIELDS
    if not changed.intersection(watched):
        return trade_row.get("position_id")

    pos_id = ensure_trade_position_link(cur, user_id, trade_row)
    if not pos_id:
        return None

    cur.execute(
        """
        SELECT id, trailing_mode, entry_price, quantity, bucket_cap, status, pyramid_history
        FROM positions
        WHERE id = %s AND user_id = %s
        """,
        (pos_id, user_id),
    )
    position = cur.fetchone()
    if not position:
        return None

    sets: list[str] = []
    values: list = []

    if "sl" in changed:
        sets.append("stop_loss = %s")
        values.append(trade_row.get("sl"))

    if "tsl" in changed:
        tsl_value = trade_row.get("tsl")
        if tsl_value not in (None, "", 0, 0.0):
            sets.append("trailing_sl = %s")
            values.append(tsl_value)
            mode = position.get("trailing_mode") if position.get("trailing_mode") in TRAILING_ACTIVE_MODES else "custom"
            sets.append("trailing_mode = %s")
            values.append(mode)
        else:
            sets.append("trailing_sl = NULL")
            if position.get("trailing_mode") == "custom":
                sets.append("trailing_mode = %s")
                values.append("original")

    # Recompute position qty + avg entry from initial leg (journal) + pyramid
    # legs (positions.pyramid_history). The pyramid side is authoritative now
    # and can't be changed via journal edits — only via POST /pyramid.
    total_bought_qty, weighted_entry = _compute_pyramid_state(trade_row, position.get("pyramid_history"))
    if total_bought_qty <= 0:
        total_bought_qty = _num(position.get("quantity")) or 0
        weighted_entry = _num(position.get("entry_price")) or weighted_entry

    # Journal can still edit initial_qty / entry_price / avg_entry; those
    # trigger a recompute of position.quantity and position.entry_price.
    pyramid_changed = bool(changed.intersection({"initial_qty", "avg_entry", "entry_price"}))
    if pyramid_changed and total_bought_qty > 0:
        sets.append("quantity = %s")
        values.append(int(total_bought_qty))
        sets.append("entry_price = %s")
        values.append(weighted_entry)
        sets.append("position_value = %s")
        values.append(round(weighted_entry * total_bought_qty, 2))

    # Exit slots (E1/E2/E3) changed → rebuild sell_history + close/reopen status
    _sidecar_hot = None  # phase-1 split: hot cols go to positions_live after main UPDATE
    if changed.intersection({"e1_price", "e1_qty", "e1_date", "e2_price", "e2_qty", "e2_date", "e3_price", "e3_qty", "e3_date"}) or pyramid_changed:
        history, total_exited = _build_sell_history_from_trade(trade_row, weighted_entry, total_bought_qty)

        sets.append("sell_history = %s")
        values.append(json.dumps(history))

        sold_pct = round((total_exited / total_bought_qty) * 100, 2) if total_bought_qty > 0 else 0
        sold_pct = max(0.0, min(sold_pct, 100.0))
        trail_remaining = max(0.0, round(100.0 - sold_pct, 2))

        sets.append("bucket_sold_pct = %s")
        values.append(sold_pct)
        sets.append("trail_remaining_pct = %s")
        values.append(trail_remaining)
        sets.append("first_sell_done = %s")
        values.append(bool(history))

        # Close when all bought shares have been sold; re-open if user edited back down.
        # Both branches also sync current_price so the frontend never shows a
        # stale live-tick price on a closed book (live-price trigger only
        # updates active rows, so on close current_price is frozen until we
        # explicitly reset it to match the exit / un-close state).
        if total_bought_qty > 0 and total_exited >= total_bought_qty:
            last_price = history[-1]["price"] if history else None
            last_date = history[-1]["date"] if history else datetime.now().isoformat()
            total_pnl = round(sum(h.get("profit") or 0 for h in history), 2)
            # Return on cost basis — NOT the last sell's price move. Price-move
            # math silently loses the earlier partials' contribution when the
            # sells happen at different prices. cost_basis = weighted_entry
            # * total_bought_qty (total rupees invested across all purchase
            # legs).
            cost_basis = (weighted_entry or 0) * (total_bought_qty or 0)
            total_pnl_pct = round((total_pnl / cost_basis * 100), 2) if cost_basis else 0
            sets.append("status = 'closed'")
            sets.append("exit_price = %s")
            values.append(last_price)
            sets.append("exit_date = %s")
            values.append(last_date)
            sets.append("total_pnl = %s")
            values.append(total_pnl)
            sets.append("total_pnl_pct = %s")
            values.append(total_pnl_pct)
            # current_price + current_r_multiple live on positions_live (hot
            # sidecar, phase-1 split). We'll UPDATE that row after the main
            # positions UPDATE commits — tracked via _sidecar_hot dict below.
            _sidecar_hot = {"current_price": last_price, "current_r_multiple": None}
        elif position.get("status") == "closed":
            sets.append("status = 'active'")
            sets.append("exit_price = NULL")
            sets.append("exit_date = NULL")
            sets.append("total_pnl = NULL")
            sets.append("total_pnl_pct = NULL")
            # Re-open: reset hot columns on the sidecar (phase-1 split).
            reopen_price = _num(position.get("entry_price")) or weighted_entry
            _sidecar_hot = {"current_price": reopen_price} if reopen_price else None

    if not sets:
        return pos_id

    sets.append("updated_at = NOW()")
    values.extend([pos_id, user_id])
    # Silence trg_sync_positions_to_journal — we've already written the authoritative
    # journal slots (iq / entry_price / p1/p2 / e1/e2/e3) above. Without this flag the
    # reverse trigger would overwrite initial_qty/entry_price with the post-pyramid
    # values we just computed, corrupting the journal's record of the original entry.
    cur.execute("SET LOCAL app.syncing_from_journal = 'true'")
    cur.execute(
        f"UPDATE positions SET {', '.join(sets)} WHERE id = %s AND user_id = %s",
        values,
    )

    # Phase-1 hot-cold split: close/reopen need to also reset hot columns on
    # positions_live. _sidecar_hot is set above when either branch fired.
    if _sidecar_hot:
        sc_sets, sc_values = [], []
        for col, v in _sidecar_hot.items():
            if v is None:
                sc_sets.append(f"{col} = NULL")
            else:
                sc_sets.append(f"{col} = %s")
                sc_values.append(v)
        sc_sets.append("updated_at = NOW()")
        sc_values.append(pos_id)
        cur.execute(
            f"UPDATE positions_live SET {', '.join(sc_sets)} WHERE position_id = %s",
            sc_values,
        )

    # Rebuild the equity-curve log if exits were touched by this journal edit.
    # Lazy import — module is imported by routes that import this file at
    # startup, and we want to avoid a circular import during boot.
    if changed.intersection({"e1_price", "e1_qty", "e1_date", "e2_price", "e2_qty", "e2_date", "e3_price", "e3_qty", "e3_date", "initial_qty", "avg_entry", "entry_price"}):
        try:
            from services import portfolio_capital_log as _capital_log
            _capital_log.rebuild_for_position(cur, user_id, pos_id)
        except Exception as e:
            print(f"[journal_position_sync] capital_log rebuild failed for pos {pos_id}: {e}")

    return pos_id


def sync_position_to_journal(cur, user_id: str, pos_id: int, data: dict) -> int | None:
    """Push live position SL/TSL updates back into the linked journal trade."""
    if not {"stop_loss", "trailing_sl", "trailing_mode"}.intersection(data.keys()):
        return None

    trade_id = ensure_position_trade_link(cur, user_id, pos_id)
    if not trade_id:
        return None

    sets = []
    values = []

    if "stop_loss" in data:
        sets.append("sl = %s")
        values.append(data.get("stop_loss"))

    if "trailing_sl" in data:
        sets.append("tsl = %s")
        values.append(data.get("trailing_sl"))
    elif data.get("trailing_mode") == "original":
        sets.append("tsl = NULL")

    if not sets:
        return trade_id

    sets.append("updated_at = CURRENT_TIMESTAMP")
    values.extend([pos_id, user_id])
    cur.execute(
        f"UPDATE journal_trades SET {', '.join(sets)} WHERE position_id = %s AND user_id = %s",
        values,
    )
    return trade_id
