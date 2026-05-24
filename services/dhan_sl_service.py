"""
Broker-side SL order management for Dhan-synced positions.

Each active position with `source_broker = 'dhan'` carries a single
DAY-validity STOP_LOSS_MARKET sell order on Dhan, sized to the full open
quantity at trigger = positions.stop_loss. The position table tracks the
live order via four columns:

  dhan_sl_order_id    — the Dhan orderId (None until first placement)
  dhan_sl_trigger     — last triggerPrice we sent to Dhan
  dhan_sl_qty         — last quantity we sent to Dhan
  dhan_sl_status      — pending | live | cancelled | filled | failed

`sync_sl(user_id, position)` is the only entry point used by callers; it
diffs the position's current stop_loss / quantity against the last-pushed
values and calls place / modify / cancel as needed. It is market-hour
aware: out-of-hours requests stay as `pending` and are picked up by the
09:14 IST `dhan-place-morning-sl-orders` cron.

Failures never bubble up — every public function returns a status dict
and stamps any error text onto positions.dhan_sl_error so the position
card can surface it. This keeps the broker plumbing from breaking the
fill-sync loop.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from database.database import get_db, close_db
from services import dhan_service


# ── Market-hours guard ───────────────────────────────────────────────────────────
#
# DAY SL orders can only be placed while the exchange is open. Outside the
# window we mark the position as `pending` and let the morning cron pick it
# up. Mon–Fri, 09:15 → 15:30 IST = 03:45 → 10:00 UTC.

def _is_market_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:  # 5 = Sat, 6 = Sun
        return False
    minutes = now.hour * 60 + now.minute
    return 225 <= minutes <= 600  # 03:45 .. 10:00 UTC


# ── DB helpers ───────────────────────────────────────────────────────────────────


_SL_COLS = (
    "id, user_id, stock_name, security_id, status, source_broker, "
    "stop_loss, quantity, dhan_sl_order_id, dhan_sl_trigger, "
    "dhan_sl_qty, dhan_sl_status"
)


def _load_position(cur, position_id: int) -> dict | None:
    cur.execute(f"SELECT {_SL_COLS} FROM positions WHERE id = %s", (position_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def _stamp(cur, position_id: int, **fields) -> None:
    """UPDATE positions with whatever sl_* fields the caller passes in."""
    if not fields:
        return
    fields["dhan_sl_last_synced_at"] = datetime.now(timezone.utc)
    sets = ", ".join(f"{k} = %s" for k in fields)
    cur.execute(
        f"UPDATE positions SET {sets} WHERE id = %s",
        list(fields.values()) + [position_id],
    )


def _stamp_error(cur, position_id: int, status: str, message: str) -> None:
    _stamp(
        cur, position_id,
        dhan_sl_status=status,
        dhan_sl_error=message[:500] if message else None,
    )


# ── Dhan order helpers (DAY SL-M sell) ─────────────────────────────────────────


def _build_sl_body(token: dict, *, security_id: str, exchange_segment: str,
                   quantity: int, trigger_price: float, product: str = "CNC") -> dict:
    """Body for POST /orders — STOP_LOSS_MARKET sell, DAY validity."""
    return {
        "dhanClientId": token["dhan_client_id"],
        "transactionType": "SELL",
        "exchangeSegment": exchange_segment,
        "productType": product,
        "orderType": "STOP_LOSS_MARKET",
        "validity": "DAY",
        "securityId": str(security_id),
        "quantity": int(quantity),
        "disclosedQuantity": 0,
        "triggerPrice": round(float(trigger_price), 2),
        "afterMarketOrder": False,
    }


def _resolve(security_id: str | None, stock_name: str) -> tuple[str | None, str]:
    """Return (security_id, exchange_segment). Falls back to stock_universe lookup."""
    if security_id:
        return str(security_id), "NSE_EQ"
    resolved = dhan_service.resolve_symbol(stock_name, "NSE")
    if not resolved or not resolved.get("security_id"):
        return None, "NSE_EQ"
    return str(resolved["security_id"]), "NSE_EQ"


def _place_dhan_sl(user_id: str, *, security_id: str, exchange_segment: str,
                   quantity: int, trigger_price: float) -> tuple[str | None, str | None]:
    """Place SLM. Returns (order_id, error). On success error is None."""
    token = dhan_service.get_token(user_id)
    if not token:
        return None, "Dhan not connected"
    body = _build_sl_body(
        token, security_id=security_id, exchange_segment=exchange_segment,
        quantity=quantity, trigger_price=trigger_price,
    )
    try:
        resp = dhan_service.call(user_id, "POST", "/orders", json=body, via_proxy=True)
    except dhan_service.DhanApiError as exc:
        return None, f"Dhan {exc.status}: {exc.body}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(resp, dict):
        return None, f"Unexpected Dhan response: {resp!r}"
    order_status = (resp.get("orderStatus") or resp.get("status") or "").upper()
    if order_status in ("REJECTED", "REJECT"):
        return None, f"rejected: {resp.get('rejectionReason') or resp}"
    order_id = resp.get("orderId") or resp.get("order_id") or resp.get("orderID")
    if not order_id:
        return None, f"no orderId in response: {resp}"
    return str(order_id), None


def _modify_dhan_sl(user_id: str, *, order_id: str, quantity: int,
                    trigger_price: float) -> str | None:
    """Modify the live SL order. Returns error string or None on success."""
    body = {
        "orderId": str(order_id),
        "orderType": "STOP_LOSS_MARKET",
        "quantity": int(quantity),
        "triggerPrice": round(float(trigger_price), 2),
        "validity": "DAY",
    }
    try:
        dhan_service.call(user_id, "PUT", f"/orders/{order_id}", json=body, via_proxy=True)
        return None
    except dhan_service.DhanApiError as exc:
        return f"Dhan {exc.status}: {exc.body}"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _cancel_dhan_sl(user_id: str, order_id: str) -> str | None:
    try:
        dhan_service.call(user_id, "DELETE", f"/orders/{order_id}", via_proxy=True)
        return None
    except dhan_service.DhanApiError as exc:
        # Already-cancelled / already-filled orders → treat as success.
        if exc.status in (404, 409):
            return None
        return f"Dhan {exc.status}: {exc.body}"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


# ── Public sync entry point ────────────────────────────────────────────────────


def sync_sl(user_id: str, position: dict, *, conn=None) -> dict:
    """Reconcile the broker SL order for a single position.

    Decides one of: noop, place, modify, cancel — based on the position's
    current stop_loss / quantity / status vs the last-pushed values. Always
    returns a status dict; never raises.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    cur = conn.cursor()
    try:
        pos = _load_position(cur, position["id"])
        if not pos:
            return {"action": "missing", "position_id": position["id"]}

        if pos.get("source_broker") != "dhan":
            return {"action": "noop", "reason": "not a broker-synced position"}

        status = pos.get("status")
        existing_order = pos.get("dhan_sl_order_id")
        sl = pos.get("stop_loss")
        qty = pos.get("quantity")
        last_trigger = pos.get("dhan_sl_trigger")
        last_qty = pos.get("dhan_sl_qty")

        # 1. Closed / inactive position → cancel any live SL.
        if status != "active":
            if existing_order:
                err = _cancel_dhan_sl(user_id, existing_order)
                _stamp(
                    cur, pos["id"],
                    dhan_sl_order_id=None,
                    dhan_sl_trigger=None,
                    dhan_sl_qty=None,
                    dhan_sl_status="cancelled" if not err else "failed",
                    dhan_sl_error=err,
                )
                if own_conn:
                    conn.commit()
                return {"action": "cancelled", "error": err}
            return {"action": "noop", "reason": "inactive, no live SL"}

        # 2. Active but no SL set or qty <= 0 → can't place an SL order.
        if not sl or not qty or int(qty) <= 0:
            if existing_order:
                err = _cancel_dhan_sl(user_id, existing_order)
                _stamp(
                    cur, pos["id"],
                    dhan_sl_order_id=None,
                    dhan_sl_trigger=None,
                    dhan_sl_qty=None,
                    dhan_sl_status="cancelled" if not err else "failed",
                    dhan_sl_error=err,
                )
            else:
                _stamp_error(cur, pos["id"], "pending", "no stop_loss set")
            if own_conn:
                conn.commit()
            return {"action": "noop", "reason": "no SL or qty"}

        # 3. Outside market hours → nothing to do; morning cron will place.
        if not _is_market_open():
            if not existing_order:
                _stamp_error(cur, pos["id"], "pending", "market closed; will place at 09:14 IST")
                if own_conn:
                    conn.commit()
            return {"action": "deferred", "reason": "market closed"}

        sec_id, segment = _resolve(pos.get("security_id"), pos.get("stock_name") or "")
        if not sec_id:
            _stamp_error(cur, pos["id"], "failed", "could not resolve security_id")
            if own_conn:
                conn.commit()
            return {"action": "error", "error": "no security_id"}

        target_trigger = round(float(sl), 2)
        target_qty = int(qty)

        # 4. No live order → place fresh SL.
        if not existing_order:
            order_id, err = _place_dhan_sl(
                user_id, security_id=sec_id, exchange_segment=segment,
                quantity=target_qty, trigger_price=target_trigger,
            )
            if err:
                _stamp_error(cur, pos["id"], "failed", err)
                if own_conn:
                    conn.commit()
                return {"action": "place_failed", "error": err}
            _stamp(
                cur, pos["id"],
                dhan_sl_order_id=order_id,
                dhan_sl_trigger=target_trigger,
                dhan_sl_qty=target_qty,
                dhan_sl_status="live",
                dhan_sl_error=None,
            )
            if own_conn:
                conn.commit()
            return {"action": "placed", "order_id": order_id}

        # 5. Live order → modify only if trigger or qty actually drifted.
        drift_trigger = (
            last_trigger is None
            or abs(float(last_trigger) - target_trigger) > 0.005
        )
        drift_qty = last_qty is None or int(last_qty) != target_qty
        if not drift_trigger and not drift_qty:
            return {"action": "noop", "reason": "no drift"}

        err = _modify_dhan_sl(
            user_id, order_id=existing_order,
            quantity=target_qty, trigger_price=target_trigger,
        )
        if err:
            _stamp_error(cur, pos["id"], "failed", err)
            if own_conn:
                conn.commit()
            return {"action": "modify_failed", "error": err}
        _stamp(
            cur, pos["id"],
            dhan_sl_trigger=target_trigger,
            dhan_sl_qty=target_qty,
            dhan_sl_status="live",
            dhan_sl_error=None,
        )
        if own_conn:
            conn.commit()
        return {"action": "modified", "order_id": existing_order}
    finally:
        if own_conn:
            close_db(conn)


# ── Bulk: morning rollover + post-fills reconciliation ────────────────────────


def place_morning_sl_orders() -> dict:
    """Place fresh DAY SLM orders for every active broker position.

    Hit by the 09:14 IST pg_cron job. Yesterday's DAY orders have auto-
    expired, so we wipe the stale dhan_sl_order_id first and let sync_sl()
    place a new one. Iterates every connected dhan_tokens user.
    """
    conn = get_db()
    summaries: list[dict] = []
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM dhan_tokens WHERE expires_at > NOW()")
        user_ids = [str(row["user_id"]) for row in cur.fetchall()]

        for uid in user_ids:
            cur.execute(
                """
                SELECT id FROM positions
                WHERE user_id = %s AND status = 'active' AND source_broker = 'dhan'
                """,
                (uid,),
            )
            position_ids = [int(r["id"]) for r in cur.fetchall()]
            # Wipe stale order IDs in one go — yesterday's DAY orders are gone.
            if position_ids:
                cur.execute(
                    """
                    UPDATE positions
                       SET dhan_sl_order_id = NULL,
                           dhan_sl_trigger  = NULL,
                           dhan_sl_qty      = NULL,
                           dhan_sl_status   = 'pending'
                     WHERE id = ANY(%s)
                    """,
                    (position_ids,),
                )
                conn.commit()

            user_results: list[dict] = []
            for pid in position_ids:
                pos_row = _load_position(cur, pid)
                if pos_row:
                    user_results.append({"id": pid, **sync_sl(uid, pos_row, conn=conn)})
            summaries.append({"user_id": uid, "results": user_results})
    finally:
        close_db(conn)

    placed = sum(
        1 for s in summaries for r in s.get("results", []) if r.get("action") == "placed"
    )
    return {"users": len(summaries), "placed": placed, "summaries": summaries}


def reconcile_user_sl(user_id: str) -> dict:
    """Walk every active broker position for one user and call sync_sl().

    Used by the 5-min `dhan-sync-fills` cron after the fills loop, so trail
    SL bumps and manual SL edits are pushed to Dhan within ~5 minutes.
    """
    conn = get_db()
    results: list[dict] = []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id FROM positions
            WHERE user_id = %s AND status = 'active' AND source_broker = 'dhan'
            """,
            (user_id,),
        )
        position_ids = [int(r["id"]) for r in cur.fetchall()]
        for pid in position_ids:
            pos_row = _load_position(cur, pid)
            if pos_row:
                results.append({"id": pid, **sync_sl(user_id, pos_row, conn=conn)})
    finally:
        close_db(conn)
    changed = sum(
        1 for r in results
        if r.get("action") in ("placed", "modified", "cancelled")
    )
    return {"checked": len(results), "changed": changed, "results": results}
