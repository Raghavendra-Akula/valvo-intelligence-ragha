"""
portfolio_capital_log.py — realized-PnL event log for equity-curve plotting.

Every position close, partial exit (E1/E2/E3), and pyramid-leg unwind writes
a row into portfolio_capital_log. The log is a *derived projection* of
positions.sell_history + pyramid_history[*].exits — fully rebuildable per
position by `rebuild_for_position`.

Hook points (callers wrap the existing transaction's cursor):
  - position_routes.close_position
  - position_routes.record_sell (regular + pyramid_idx paths)
  - position_routes._recompute_after_sellhistory_change (covers edit/delete)
  - position_routes.delete_position (drop rows)
  - journal_position_sync.sync_journal_trade_to_position (journal-driven close)

Capital running total is NOT stored — computed at query time
(base_capital(fy) + Σ realized_pnl ordered by event_ts) so a single
rebuild can't desync other positions' rows.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone


def fy_for_date(d: date | datetime) -> str:
    """Indian FY label ('YYYY-YY') from a date. April-onwards rolls into the
    new FY. Matches Backend/routes/settings_routes.py:_current_fy_label."""
    if isinstance(d, datetime):
        d = d.date()
    if d.month >= 4:
        return f"{d.year}-{str(d.year + 1)[-2:]}"
    return f"{d.year - 1}-{str(d.year)[-2:]}"


def _parse_ts(raw):
    """Accept ISO strings, datetimes, dates. Return (date, datetime). Both
    are required: event_date drives the FY bucket + day grouping; event_ts
    drives the running-total ordering when multiple events land same day."""
    if raw is None:
        return None, None
    if isinstance(raw, datetime):
        return raw.date(), raw
    if isinstance(raw, date):
        return raw, datetime(raw.year, raw.month, raw.day)
    s = str(raw)
    # Try full ISO with time first, then date-only
    for parser in (
        lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")),
        lambda x: datetime.strptime(x[:10], "%Y-%m-%d"),
    ):
        try:
            dt = parser(s)
            return dt.date(), dt
        except (ValueError, TypeError):
            continue
    return None, None


def _coerce_list(maybe_json):
    """positions.sell_history / pyramid_history come back as either list (psycopg2
    auto-decoded JSONB) or string (legacy / some code paths). Normalize."""
    if maybe_json is None:
        return []
    if isinstance(maybe_json, list):
        return maybe_json
    if isinstance(maybe_json, str):
        if not maybe_json.strip():
            return []
        try:
            parsed = json.loads(maybe_json)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _events_from_position(pos: dict) -> list[tuple]:
    """Walk a position's sell_history + pyramid_history[*].exits and yield
    insert tuples for portfolio_capital_log. Skips events with unparseable
    dates (treat as malformed and don't pollute the curve)."""
    pos_id = pos["id"]
    user_id = pos["user_id"]
    stock = pos.get("stock_name")
    out: list[tuple] = []

    sh = _coerce_list(pos.get("sell_history"))
    for i, s in enumerate(sh):
        d, ts = _parse_ts(s.get("date"))
        if d is None:
            continue
        is_close = (s.get("trigger") == "close_position")
        out.append((
            user_id,
            fy_for_date(d),
            pos_id,
            d,
            ts,
            "final_close" if is_close else "partial_exit",
            f"pos:{pos_id}:sh:{i}",
            stock,
            int(s.get("shares") or s.get("qty") or s.get("quantity") or 0),
            float(s.get("price") or 0),
            float(s.get("profit") or 0),
            s.get("trigger") or None,
        ))

    pyr = _coerce_list(pos.get("pyramid_history"))
    for li, leg in enumerate(pyr):
        if not isinstance(leg, dict):
            continue
        for ei, ex in enumerate(leg.get("exits") or []):
            d, ts = _parse_ts(ex.get("date"))
            if d is None:
                continue
            out.append((
                user_id,
                fy_for_date(d),
                pos_id,
                d,
                ts,
                "pyramid_exit",
                f"pos:{pos_id}:pyr:{li}:exit:{ei}",
                stock,
                int(ex.get("shares") or 0),
                float(ex.get("price") or 0),
                float(ex.get("profit") or 0),
                ex.get("trigger") or None,
            ))

    return out


_INSERT_SQL = """
    INSERT INTO portfolio_capital_log
        (user_id, fy, position_id, event_date, event_ts, event_type,
         source_key, stock_name, shares, price, realized_pnl, trigger)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (user_id, source_key) DO UPDATE SET
        fy            = EXCLUDED.fy,
        event_date    = EXCLUDED.event_date,
        event_ts      = EXCLUDED.event_ts,
        event_type    = EXCLUDED.event_type,
        stock_name    = EXCLUDED.stock_name,
        shares        = EXCLUDED.shares,
        price         = EXCLUDED.price,
        realized_pnl  = EXCLUDED.realized_pnl,
        trigger       = EXCLUDED.trigger
"""


def rebuild_for_position(cur, user_id, position_id) -> int:
    """Delete all log rows for (user_id, position_id) and re-insert from the
    current positions row. Idempotent. Returns rows inserted.

    Wrapped in a SAVEPOINT so a logging failure (bad date format in legacy
    sell_history, schema mismatch, etc.) can't abort the outer transaction
    and roll back the actual position update. The equity curve is
    observability, not critical correctness.
    """
    sp = f"pcl_rebuild_{position_id}"
    try:
        cur.execute(f"SAVEPOINT {sp}")
    except Exception as e:
        # Outer txn isn't open — nothing to log, skip silently
        print(f"[portfolio_capital_log] could not open savepoint: {e}")
        return 0
    try:
        cur.execute(
            "SELECT id, user_id, stock_name, sell_history, pyramid_history "
            "FROM positions WHERE id = %s AND user_id = %s",
            (position_id, user_id),
        )
        pos = cur.fetchone()
        if not pos:
            cur.execute(
                "DELETE FROM portfolio_capital_log WHERE user_id = %s AND position_id = %s",
                (user_id, position_id),
            )
            cur.execute(f"RELEASE SAVEPOINT {sp}")
            return 0

        cur.execute(
            "DELETE FROM portfolio_capital_log WHERE user_id = %s AND position_id = %s",
            (user_id, position_id),
        )
        rows = _events_from_position(dict(pos))
        if rows:
            cur.executemany(_INSERT_SQL, rows)
        cur.execute(f"RELEASE SAVEPOINT {sp}")
        return len(rows)
    except Exception as e:
        print(f"[portfolio_capital_log] rebuild_for_position({position_id}) failed: {e}")
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            cur.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception as rb:
            print(f"[portfolio_capital_log] savepoint rollback failed: {rb}")
        return 0


def delete_for_position(cur, user_id, position_id) -> None:
    """Drop all log rows for a position (called when the position itself is
    deleted). Wrapped in a savepoint so a failure here doesn't block the
    position DELETE."""
    sp = f"pcl_delete_{position_id}"
    try:
        cur.execute(f"SAVEPOINT {sp}")
    except Exception as e:
        print(f"[portfolio_capital_log] could not open savepoint: {e}")
        return
    try:
        cur.execute(
            "DELETE FROM portfolio_capital_log WHERE user_id = %s AND position_id = %s",
            (user_id, position_id),
        )
        cur.execute(f"RELEASE SAVEPOINT {sp}")
    except Exception as e:
        print(f"[portfolio_capital_log] delete_for_position({position_id}) failed: {e}")
        try:
            cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            cur.execute(f"RELEASE SAVEPOINT {sp}")
        except Exception as rb:
            print(f"[portfolio_capital_log] savepoint rollback failed: {rb}")


def backfill_user(cur, user_id) -> dict:
    """Rebuild the entire log for a user from their positions. Used by the
    one-shot admin route after migration. Returns counts."""
    cur.execute(
        "SELECT id, user_id, stock_name, sell_history, pyramid_history "
        "FROM positions WHERE user_id = %s",
        (user_id,),
    )
    positions = cur.fetchall()

    cur.execute("DELETE FROM portfolio_capital_log WHERE user_id = %s", (user_id,))

    total_events = 0
    positions_with_events = 0
    for pos in positions:
        rows = _events_from_position(dict(pos))
        if rows:
            cur.executemany(_INSERT_SQL, rows)
            total_events += len(rows)
            positions_with_events += 1

    return {
        "positions_scanned": len(positions),
        "positions_with_events": positions_with_events,
        "events_logged": total_events,
    }
