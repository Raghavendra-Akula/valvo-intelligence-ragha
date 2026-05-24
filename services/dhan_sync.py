"""
Auto-sync Dhan broker fills into positions + journal_trades.

Triggered by:
  * pg_cron every 5 min during market hours → POST /api/dhan/sync-fills
  * Reconciler thread fired by the order ticket at +3s / +10s / +30s
  * `place_order_with_position()` for the immediate-position-from-OrderTicket path

Two semantic operations on a Dhan fill:
  BUY  → either reconcile a pre-filled placeholder, pyramid into an existing
         active position, or create a new one
  SELL → consume oldest lots first (FIFO across initial + pyramid_history),
         append to positions.sell_history, mirror into journal e1/e2/e3,
         close position when fully exited

Idempotency is keyed on Dhan's `exchangeTradeId` via dhan_synced_fills.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any

from database.database import get_db, close_db
from services import dhan_service
from services import dhan_sl_service


def _safe_sync_sl(user_id: str, position_id: int, conn) -> None:
    """Best-effort SL sync. Errors are stamped onto the row and never raise."""
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, user_id, stock_name, security_id, status, source_broker,
                   stop_loss, quantity, dhan_sl_order_id, dhan_sl_trigger,
                   dhan_sl_qty, dhan_sl_status
            FROM positions WHERE id = %s
            """,
            (position_id,),
        )
        row = cur.fetchone()
        if not row:
            return
        dhan_sl_service.sync_sl(user_id, dict(row), conn=conn)
    except Exception:
        # Never break the parent transaction over an SL plumbing issue.
        pass


# ── Helpers ────────────────────────────────────────────────────────────────


def _to_int(v, default: int = 0) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _to_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _normalize_fill(raw: dict) -> dict | None:
    """Extract canonical fields from a Dhan /trades record. Returns None if unusable."""
    side = (raw.get("transactionType") or "").upper()
    if side not in ("BUY", "SELL"):
        return None
    qty = _to_int(raw.get("tradedQuantity"))
    price = _to_float(raw.get("tradedPrice"))
    if qty <= 0 or not price:
        return None

    trade_id = (
        raw.get("exchangeTradeId")
        or raw.get("orderTradeId")
        or raw.get("tradeNumber")
    )
    order_id = raw.get("orderId") or raw.get("exchangeOrderId")
    if not trade_id:
        # Synthetic fallback so we still dedupe even when Dhan omits exchangeTradeId.
        trade_id = f"{order_id}:{raw.get('createTime', '')}:{qty}:{price}"

    fill_time = raw.get("createTime") or raw.get("updateTime")
    return {
        "trade_id": str(trade_id),
        "order_id": str(order_id) if order_id else None,
        "side": side,
        "symbol": (raw.get("tradingSymbol") or "").strip().upper(),
        "security_id": str(raw.get("securityId") or "").strip() or None,
        "qty": qty,
        "price": price,
        "fill_time": fill_time,
        "raw": raw,
    }


def _get_active_position(cur, user_id: str, *, security_id: str | None = None,
                         dhan_order_id: str | None = None) -> dict | None:
    """Find an active position by Dhan order_id (preferred), else security_id."""
    if dhan_order_id:
        cur.execute(
            """
            SELECT * FROM positions
            WHERE user_id = %s AND dhan_order_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, dhan_order_id),
        )
        row = cur.fetchone()
        if row:
            return dict(row)
    if security_id:
        cur.execute(
            """
            SELECT * FROM positions
            WHERE user_id = %s AND security_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, security_id),
        )
        row = cur.fetchone()
        if row:
            return dict(row)
    return None


def _create_position_from_fill(cur, user_id: str, fill: dict, *,
                               sl_override: float | None = None) -> tuple[int, int]:
    """Create a new active position + matching journal entry from a BUY fill."""
    price = float(fill["price"])
    qty = int(fill["qty"])
    sl = float(sl_override) if sl_override is not None else round(price * 0.96, 2)
    one_r = round(abs(price - sl) * qty, 2) if sl else None
    risk_pct = round(abs(price - sl) / price * 100, 2) if price and sl else None
    pos_value = round(price * qty, 2)

    cur.execute(
        """
        INSERT INTO positions (
            stock_name, entry_price, stop_loss, quantity,
            position_value, one_r_value, risk_pct,
            user_id, security_id, status,
            source, source_broker, dhan_order_id,
            created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active',
                'dhan_sync', 'dhan', %s, NOW(), NOW())
        RETURNING id
        """,
        (
            fill["symbol"], price, sl, qty,
            pos_value, one_r, risk_pct,
            user_id, fill.get("security_id") or None,
            fill.get("order_id"),
        ),
    )
    pos_id = cur.fetchone()["id"]

    cur.execute(
        """
        INSERT INTO journal_trades (
            trade_date, symbol, name, security_id,
            entry_price, avg_entry, sl, initial_qty,
            user_id, position_id, buy_sell, entry_type,
            created_at, updated_at
        ) VALUES (CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Buy', 'BREAKOUT', NOW(), NOW())
        RETURNING id
        """,
        (
            fill["symbol"], fill["symbol"], fill.get("security_id") or "",
            price, price, sl, qty,
            user_id, pos_id,
        ),
    )
    journal_id = cur.fetchone()["id"]
    return pos_id, journal_id


def _pyramid_into(cur, user_id: str, pos: dict, fill: dict) -> int:
    """Append a pyramid leg to an existing active position.

    Per Phase 2a: pyramids live ONLY on positions.pyramid_history. Journal's
    initial_qty / entry_price stay frozen at the original first leg. We set
    `app.syncing_from_journal` so the positions→journal trigger doesn't
    overwrite the journal's record of the original entry.
    """
    pyramid_history = pos.get("pyramid_history") or []
    if isinstance(pyramid_history, str):
        try:
            pyramid_history = json.loads(pyramid_history)
        except Exception:
            pyramid_history = []

    add_qty = int(fill["qty"])
    add_price = float(fill["price"])
    next_slot = f"P{len(pyramid_history) + 1}"
    pyramid_history.append({
        "date": str(fill.get("fill_time") or datetime.utcnow().isoformat()),
        "price": add_price,
        "shares": add_qty,
        "slot": next_slot,
        "dhan_order_id": fill.get("order_id"),
        "trade_id": fill.get("trade_id"),
    })

    new_qty = int(pos["quantity"]) + add_qty
    old_value = float(pos["entry_price"]) * int(pos["quantity"])
    new_entry = round((old_value + add_price * add_qty) / new_qty, 2)
    new_pos_value = round(new_entry * new_qty, 2)
    sl = float(pos["stop_loss"]) if pos.get("stop_loss") is not None else None
    new_one_r = round(abs(new_entry - sl) * new_qty, 2) if sl else None
    new_risk_pct = round(abs(new_entry - sl) / new_entry * 100, 2) if sl else None

    cur.execute("SET LOCAL app.syncing_from_journal = 'true'")
    cur.execute(
        """
        UPDATE positions SET
            pyramid_history = %s,
            quantity = %s,
            entry_price = %s,
            position_value = %s,
            one_r_value = %s,
            risk_pct = %s,
            updated_at = NOW()
        WHERE id = %s AND user_id = %s
        """,
        (json.dumps(pyramid_history), new_qty, new_entry,
         new_pos_value, new_one_r, new_risk_pct,
         pos["id"], user_id),
    )
    return pos["id"]


def _consume_sell_fifo(cur, user_id: str, pos: dict, fill: dict) -> tuple[int, int | None] | None:
    """Apply a SELL fill using FIFO across initial + pyramid_history lots.

    Returns (position_id, journal_id) or None if nothing was consumable.
    """
    pyramid_history = pos.get("pyramid_history") or []
    if isinstance(pyramid_history, str):
        try:
            pyramid_history = json.loads(pyramid_history)
        except Exception:
            pyramid_history = []
    sell_history = pos.get("sell_history") or []
    if isinstance(sell_history, str):
        try:
            sell_history = json.loads(sell_history)
        except Exception:
            sell_history = []

    total_bought = int(pos["quantity"])
    shares_already_sold = sum(int(s.get("shares") or 0) for s in sell_history)
    available = total_bought - shares_already_sold
    if available <= 0:
        return None

    sell_qty = min(int(fill["qty"]), available)
    fill_price = float(fill["price"])

    cur.execute(
        """
        SELECT id, entry_price, initial_qty, e1_qty, e2_qty, e3_qty
        FROM journal_trades
        WHERE position_id = %s AND user_id = %s
        ORDER BY id DESC LIMIT 1
        """,
        (pos["id"], user_id),
    )
    journal_row = cur.fetchone()
    journal = dict(journal_row) if journal_row else None

    pyramid_shares = sum(int(leg.get("shares") or 0) for leg in pyramid_history)
    if journal and journal.get("entry_price"):
        # journal.entry_price is the original first-leg price, never weighted-avg.
        initial_price = float(journal["entry_price"])
        initial_qty = int(journal.get("initial_qty") or (total_bought - pyramid_shares))
    else:
        initial_price = float(pos["entry_price"])
        initial_qty = total_bought - pyramid_shares

    lots = [{"date": pos.get("created_at"), "price": initial_price, "qty": initial_qty}]
    for leg in pyramid_history:
        lots.append({
            "date": leg.get("date") or "",
            "price": float(leg.get("price") or 0),
            "qty": int(leg.get("shares") or 0),
        })
    lots.sort(key=lambda l: str(l["date"] or ""))

    remaining_already = shares_already_sold
    for lot in lots:
        if remaining_already <= 0:
            break
        eat = min(lot["qty"], remaining_already)
        lot["qty"] -= eat
        remaining_already -= eat

    realised_profit = 0.0
    qty_to_sell = sell_qty
    for lot in lots:
        if qty_to_sell <= 0:
            break
        if lot["qty"] <= 0:
            continue
        take = min(lot["qty"], qty_to_sell)
        realised_profit += (fill_price - lot["price"]) * take
        lot["qty"] -= take
        qty_to_sell -= take

    pct = round(sell_qty / total_bought * 100, 2) if total_bought else 0
    extension = round((fill_price - initial_price) / initial_price * 100, 2) if initial_price else 0
    new_sell = {
        "date": str(fill.get("fill_time") or datetime.utcnow().isoformat()),
        "pct": pct,
        "price": fill_price,
        "extension": extension,
        "trigger": "dhan_sync",
        "r_multiple": 0,
        "shares": sell_qty,
        "profit": round(realised_profit, 2),
        "dhan_order_id": fill.get("order_id"),
        "trade_id": fill.get("trade_id"),
    }
    sell_history.append(new_sell)

    new_total_sold = shares_already_sold + sell_qty
    new_pct = round(new_total_sold / total_bought * 100, 2) if total_bought else 0
    new_pct = max(0.0, min(new_pct, 100.0))
    is_closed = new_total_sold >= total_bought

    sets = [
        "sell_history = %s",
        "bucket_sold_pct = %s",
        "trail_remaining_pct = %s",
        "first_sell_done = TRUE",
    ]
    values: list = [json.dumps(sell_history), new_pct, max(0.0, round(100.0 - new_pct, 2))]

    if is_closed:
        cost_basis = initial_price * initial_qty + sum(
            float(l.get("price") or 0) * int(l.get("shares") or 0) for l in pyramid_history
        )
        total_pnl = round(sum((s.get("profit") or 0) for s in sell_history), 2)
        total_pnl_pct = round(total_pnl / cost_basis * 100, 2) if cost_basis else 0
        sets.extend([
            "status = 'closed'",
            "exit_price = %s",
            "exit_date = %s",
            "total_pnl = %s",
            "total_pnl_pct = %s",
        ])
        values.extend([fill_price, fill.get("fill_time") or datetime.utcnow(), total_pnl, total_pnl_pct])
    sets.append("updated_at = NOW()")

    cur.execute("SET LOCAL app.syncing_from_journal = 'true'")
    cur.execute(
        f"UPDATE positions SET {', '.join(sets)} WHERE id = %s AND user_id = %s",
        values + [pos["id"], user_id],
    )

    journal_id = None
    if journal:
        next_slot = None
        for slot in ("e1", "e2", "e3"):
            if not journal.get(f"{slot}_qty"):
                next_slot = slot
                break
        if next_slot:
            cur.execute(
                f"""UPDATE journal_trades SET
                    {next_slot}_price = %s,
                    {next_slot}_qty   = %s,
                    {next_slot}_date  = %s,
                    updated_at = NOW()
                WHERE id = %s AND user_id = %s""",
                (
                    fill_price, sell_qty,
                    fill.get("fill_time") or datetime.utcnow().date(),
                    journal["id"], user_id,
                ),
            )
            journal_id = journal["id"]

    return pos["id"], journal_id


def _reconcile_pre_filled_position(cur, user_id: str, pos: dict, fill: dict) -> int:
    """Update a pre-filled (order-ticket) position with the actual fill values."""
    actual_price = float(fill["price"])
    actual_qty = int(fill["qty"])
    pos_qty = int(pos["quantity"])
    pos_price = float(pos["entry_price"])
    if actual_price == pos_price and actual_qty == pos_qty:
        return pos["id"]

    sl = float(pos["stop_loss"]) if pos.get("stop_loss") is not None else None
    one_r = round(abs(actual_price - sl) * actual_qty, 2) if sl else None
    risk_pct = round(abs(actual_price - sl) / actual_price * 100, 2) if sl else None

    cur.execute("SET LOCAL app.syncing_from_journal = 'true'")
    cur.execute(
        """
        UPDATE positions SET
            entry_price = %s,
            quantity = %s,
            position_value = %s,
            one_r_value = %s,
            risk_pct = %s,
            updated_at = NOW()
        WHERE id = %s AND user_id = %s
        """,
        (actual_price, actual_qty, round(actual_price * actual_qty, 2),
         one_r, risk_pct, pos["id"], user_id),
    )
    cur.execute(
        """
        UPDATE journal_trades SET
            entry_price = %s, avg_entry = %s, initial_qty = %s, updated_at = NOW()
        WHERE position_id = %s AND user_id = %s
        """,
        (actual_price, actual_price, actual_qty, pos["id"], user_id),
    )
    return pos["id"]


# ── Routing ───────────────────────────────────────────────────────────────


def _route_buy_fill(cur, user_id: str, fill: dict) -> tuple[str, int | None]:
    pos = _get_active_position(cur, user_id, dhan_order_id=fill.get("order_id"))
    if pos:
        pos_id = _reconcile_pre_filled_position(cur, user_id, pos, fill)
        return "reconciled", pos_id

    if fill.get("security_id"):
        pos = _get_active_position(cur, user_id, security_id=fill["security_id"])
        if pos:
            pos_id = _pyramid_into(cur, user_id, pos, fill)
            return "pyramided", pos_id

    pos_id, _ = _create_position_from_fill(cur, user_id, fill)
    return "created", pos_id


def _route_sell_fill(cur, user_id: str, fill: dict) -> tuple[str, int | None]:
    pos = None
    if fill.get("security_id"):
        pos = _get_active_position(cur, user_id, security_id=fill["security_id"])
    if not pos:
        return "no_active_position", None

    result = _consume_sell_fifo(cur, user_id, pos, fill)
    if not result:
        return "nothing_to_consume", None
    pos_id, _ = result
    return "sold", pos_id


def _process_fill(cur, user_id: str, fill: dict) -> dict:
    cur.execute("SELECT 1 FROM dhan_synced_fills WHERE trade_id = %s", (fill["trade_id"],))
    if cur.fetchone():
        return {"trade_id": fill["trade_id"], "action": "skipped_duplicate"}

    if fill["side"] == "BUY":
        action, pos_id = _route_buy_fill(cur, user_id, fill)
    else:
        action, pos_id = _route_sell_fill(cur, user_id, fill)

    # Push the new SL state to Dhan whenever we touched a position. Covers
    # create / pyramid / reconcile-pre-fill / partial-sell / full-close.
    if pos_id and action not in ("error", "no_active_position", "nothing_to_consume"):
        _safe_sync_sl(user_id, pos_id, cur.connection)

    cur.execute(
        """
        INSERT INTO dhan_synced_fills (
            trade_id, user_id, side, security_id, symbol,
            quantity, fill_price, fill_time, dhan_order_id,
            position_id, raw_fill
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (trade_id) DO NOTHING
        """,
        (
            fill["trade_id"], user_id, fill["side"],
            fill.get("security_id"), fill.get("symbol"),
            fill["qty"], fill["price"], fill.get("fill_time"),
            fill.get("order_id"), pos_id, json.dumps(fill["raw"]),
        ),
    )
    return {
        "trade_id": fill["trade_id"],
        "action": action,
        "position_id": pos_id,
        "side": fill["side"],
        "symbol": fill.get("symbol"),
    }


# ── Public API ───────────────────────────────────────────────────────────────


def sync_user_fills(user_id: str) -> dict:
    """Pull /trades for one user, dedup, route every new fill."""
    try:
        trades = dhan_service.call(user_id, "GET", "/trades")
    except dhan_service.DhanNotConnected:
        return {"connected": False, "processed": 0, "results": []}
    except dhan_service.DhanApiError as exc:
        return {"error": "dhan_api", "status": exc.status, "detail": str(exc.body)}

    if not isinstance(trades, list):
        trades = trades.get("data", []) if isinstance(trades, dict) else []

    fills = [f for f in (_normalize_fill(t) for t in trades) if f]
    fills.sort(key=lambda f: str(f.get("fill_time") or ""))

    if not fills:
        # No new fills, but the trail or a manual edit may still have moved
        # SL since the last run. Reconcile broker SL state anyway.
        sl_summary: dict = {}
        try:
            sl_summary = dhan_sl_service.reconcile_user_sl(user_id)
        except Exception as exc:
            sl_summary = {"error": str(exc)}
        return {
            "connected": True, "processed": 0, "new": 0,
            "results": [], "sl_reconcile": sl_summary,
        }

    conn = get_db()
    results: list[dict] = []
    try:
        cur = conn.cursor()
        for fill in fills:
            try:
                results.append(_process_fill(cur, user_id, fill))
            except Exception as exc:
                results.append({
                    "trade_id": fill["trade_id"],
                    "action": "error",
                    "error": str(exc),
                })
        conn.commit()
    finally:
        close_db(conn)

    new_count = sum(1 for r in results if r.get("action") not in ("skipped_duplicate", "error"))

    # After fills are processed, walk every active broker position and push
    # the current SL to Dhan if it has drifted (auto-trail bumps, manual SL
    # edits, qty changes that we somehow missed). Best-effort.
    sl_summary: dict = {}
    try:
        sl_summary = dhan_sl_service.reconcile_user_sl(user_id)
    except Exception as exc:
        sl_summary = {"error": str(exc)}

    return {
        "connected": True,
        "processed": len(fills),
        "new": new_count,
        "results": results,
        "sl_reconcile": sl_summary,
    }


def reconcile_order(user_id: str, dhan_order_id: str) -> dict:
    """Quick check on a specific order — used by post-place reconciler."""
    try:
        trades = dhan_service.call(user_id, "GET", "/trades")
    except (dhan_service.DhanNotConnected, dhan_service.DhanApiError):
        return {"matched": False}
    if not isinstance(trades, list):
        trades = trades.get("data", []) if isinstance(trades, dict) else []

    for raw in trades:
        if str(raw.get("orderId") or "") != str(dhan_order_id):
            continue
        fill = _normalize_fill(raw)
        if not fill:
            continue
        conn = get_db()
        try:
            cur = conn.cursor()
            result = _process_fill(cur, user_id, fill)
            conn.commit()
            return {"matched": True, **result}
        finally:
            close_db(conn)
    return {"matched": False}


def schedule_post_place_reconciler(user_id: str, dhan_order_id: str) -> None:
    """Fire reconcile_order at +3s, +10s, +30s in a daemon thread."""
    if not dhan_order_id:
        return

    def _run() -> None:
        for delay in (3, 10, 30):
            try:
                time.sleep(delay)
                result = reconcile_order(user_id, dhan_order_id)
                if result.get("matched") and result.get("action") not in ("error",):
                    return
            except Exception:
                continue

    threading.Thread(target=_run, daemon=True).start()


# ── Order-ticket entry: place + create position immediately ─────────────


def place_order_with_position(user_id: str, *, symbol: str, exchange: str, side: str,
                              quantity: int, order_type: str = "MARKET",
                              product: str = "CNC", price: float | None = None,
                              trigger_price: float | None = None,
                              stop_loss: float | None = None,
                              validity: str = "DAY",
                              disclosed_quantity: int = 0,
                              after_market: bool = False,
                              amo_time: str = "OPEN") -> dict:
    """
    Place a Dhan order AND create the matching position immediately so the UI
    reflects the trade right away. The 5-min poller and post-place reconciler
    silently update entry_price/qty if the actual fill differs from the form.
    """
    dhan_response = dhan_service.place_simple_order(
        user_id,
        symbol=symbol, exchange=exchange, side=side,
        quantity=quantity, order_type=order_type, product=product,
        price=price, trigger_price=trigger_price, validity=validity,
        disclosed_quantity=disclosed_quantity,
        after_market=after_market, amo_time=amo_time,
    )

    order_id: str | None = None
    order_status: str = ""
    if isinstance(dhan_response, dict):
        order_id = (
            dhan_response.get("orderId")
            or dhan_response.get("order_id")
            or dhan_response.get("orderID")
        )
        order_status = (
            dhan_response.get("orderStatus")
            or dhan_response.get("status")
            or ""
        ).upper()

    if order_status in ("REJECTED", "REJECT"):
        return {"dhan": dhan_response, "position_id": None, "rejected": True}

    side_norm = (side or "").upper()
    if side_norm != "BUY":
        # Sell-side handling waits for the fill — no pre-creation makes sense.
        if order_id:
            schedule_post_place_reconciler(user_id, str(order_id))
        return {"dhan": dhan_response, "position_id": None, "wait_for_poll": True}

    fill_price = _to_float(price) if order_type in ("LIMIT", "STOP_LOSS") else None
    if fill_price is None:
        fill_price = _to_float(price) or _to_float(trigger_price)
    if not fill_price:
        if order_id:
            schedule_post_place_reconciler(user_id, str(order_id))
        return {"dhan": dhan_response, "position_id": None, "wait_for_poll": True}

    resolved = dhan_service.resolve_symbol(symbol, exchange)
    security_id = (resolved or {}).get("security_id")
    fill = {
        "trade_id": f"placeholder:{order_id}",
        "order_id": str(order_id) if order_id else None,
        "side": "BUY",
        "symbol": (symbol or "").strip().upper(),
        "security_id": str(security_id) if security_id else None,
        "qty": int(quantity),
        "price": float(fill_price),
        "fill_time": datetime.utcnow(),
        "raw": dhan_response if isinstance(dhan_response, dict) else {},
    }

    conn = get_db()
    try:
        cur = conn.cursor()
        existing: dict | None = None
        if fill["security_id"]:
            existing = _get_active_position(cur, user_id, security_id=fill["security_id"])
        if existing:
            pos_id = _pyramid_into(cur, user_id, existing, fill)
        else:
            pos_id, _ = _create_position_from_fill(cur, user_id, fill, sl_override=stop_loss)
        # Place / modify the broker SL order to match. Best-effort —
        # failures are stamped onto positions.dhan_sl_error.
        if pos_id:
            _safe_sync_sl(user_id, pos_id, conn)
        conn.commit()
    finally:
        close_db(conn)

    if order_id:
        schedule_post_place_reconciler(user_id, str(order_id))

    return {"dhan": dhan_response, "position_id": pos_id, "wait_for_poll": False}
