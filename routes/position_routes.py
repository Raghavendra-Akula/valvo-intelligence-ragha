"""
Position Manager Routes — Phase 1: Foundation
CRUD for positions + chart analysis + sell recording + journal
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter
import os
import json
from datetime import datetime, timedelta, date

from database.database import get_db, close_db
from database.init_positions_db import get_regime_params
from services.journal_position_sync import TRAILING_ACTIVE_MODES  # phase 2b: sync removed
from services import portfolio_capital_log as capital_log
from config.settings import count_trading_days_between, trading_days_until

position_bp = Blueprint("position", __name__)
UPLOAD_FOLDER = "uploads"


# ═══════════════════════════════════════════════════
# POSITIONS CRUD
# ═══════════════════════════════════════════════════


class InvariantError(ValueError):
    """Raised when a proposed position state would violate a structural
    invariant (e.g. total sold shares exceeds total bought, or a price /
    share count is non-positive). Write paths convert this to a 400 for
    the client so the user sees a clear 'why the edit was rejected'
    message instead of silent data corruption."""
    pass


def _detect_position_health_issue(row):
    """Non-raising structural check. Returns dict or None.

    Used by read endpoints (list_positions, get_position, list_trades via JOIN)
    to flag rows whose stored state violates invariants — legacy rows from
    before validation existed, manual SQL edits, or anything else that's
    already corrupt. The frontend quarantines flagged rows: excludes them
    from dashboard aggregates and renders a 'NEEDS FIX' indicator so the
    user knows to correct the data before its ₹-values are trusted.

    Pure function over a row dict (doesn't hit DB). Mirrors
    _validate_position_invariants but returns instead of raising.
    """
    try:
        entry_price = float(row.get("entry_price") or 0)
    except (TypeError, ValueError):
        entry_price = 0
    try:
        # Accept both 'quantity' (positions table field) and 'initial_qty'
        # (the alias journal list_trades uses after its JOIN). Without this
        # fallback, journal rows got flagged as 'quantity is 0 (must be
        # positive)' even when initial_qty was a perfectly valid 300.
        quantity = int(row.get("quantity") or row.get("initial_qty") or 0)
    except (TypeError, ValueError):
        quantity = 0

    sh = row.get("sell_history") or []
    if isinstance(sh, str):
        try: sh = json.loads(sh)
        except Exception: sh = []
    # Orphan / legacy journal rows don't have sell_history JSONB (no linked
    # position). Fall back to the journal's own e1/e2/e3 exit columns if
    # those are present so over-sold legacy rows still get flagged.
    if not sh and any(row.get(k) for k in ("e1_qty", "e2_qty", "e3_qty")):
        legacy = []
        for i in (1, 2, 3):
            q = row.get(f"e{i}_qty")
            p = row.get(f"e{i}_price")
            if q and p:
                legacy.append({"shares": int(q), "price": float(p)})
        sh = legacy
    ph = row.get("pyramid_history") or []
    if isinstance(ph, str):
        try: ph = json.loads(ph)
        except Exception: ph = []

    if quantity <= 0:
        return {"kind": "invalid_quantity", "message": f"quantity is {quantity} (must be positive)"}
    if entry_price <= 0:
        return {"kind": "invalid_entry", "message": f"entry_price is {entry_price} (must be positive)"}

    total_sold = 0
    for i, e in enumerate(sh):
        if not isinstance(e, dict):
            return {"kind": "malformed_sell", "message": f"sell_history[{i}] is malformed"}
        try:
            shares = int(e.get("shares") or e.get("qty") or e.get("quantity") or 0)
            price = float(e.get("price") or 0)
        except (TypeError, ValueError):
            return {"kind": "malformed_sell", "message": f"E{i+1} has non-numeric price/shares"}
        if shares <= 0:
            return {"kind": "invalid_exit", "message": f"E{i+1} shares is {shares} (must be positive)"}
        if price <= 0:
            return {"kind": "invalid_exit", "message": f"E{i+1} price is {price} (must be positive)"}
        total_sold += shares

    if total_sold > quantity:
        return {
            "kind": "oversold",
            "message": f"Sold {total_sold} shares but only {quantity} bought — "
                       f"reduce exits or fix quantity",
            "total_sold": total_sold,
            "quantity": quantity,
        }

    if len(sh) > 3:
        return {"kind": "too_many_exits", "message": f"{len(sh)} exit slots (max 3)"}

    for i, leg in enumerate(ph):
        if not isinstance(leg, dict):
            return {"kind": "malformed_pyramid", "message": f"pyramid_history[{i}] is malformed"}
        try:
            shares = int(leg.get("shares") or 0)
            price = float(leg.get("price") or 0)
        except (TypeError, ValueError):
            return {"kind": "malformed_pyramid", "message": f"P{i+1} has non-numeric price/shares"}
        if shares <= 0:
            return {"kind": "invalid_pyramid", "message": f"P{i+1} shares is {shares} (must be positive)"}
        if price <= 0:
            return {"kind": "invalid_pyramid", "message": f"P{i+1} price is {price} (must be positive)"}

    if len(ph) > 2:
        return {"kind": "too_many_pyramids", "message": f"{len(ph)} pyramid legs (max 2)"}

    return None


def _annotate_partial_exit_metadata(r):
    """Derive partial-exit fields from sell_history and mutate row in place.

    Used by every read path that returns positions to the frontend (the list
    endpoint AND refresh-all, which is what the Position page actually calls
    on mount). Without this, active positions with partial exits show their
    raw buy qty and the 'PARTIAL · X of Y sh' banner never renders.

    Also computes:
      - lifetime_return_pct = total_pnl / total_cost_outlay × 100 (closed)
      - risk_breakdown      = { main, per_leg, total } (active) — accounts
                              for separate-mode pyramid leg SLs.
      - leg_breakdown       = per-leg P&L for closed trades (main + pyramid)
    """
    sell_history = r.get("sell_history") or []
    if isinstance(sell_history, str):
        try:
            sell_history = json.loads(sell_history)
        except Exception:
            sell_history = []
    pyramid_history = r.get("pyramid_history") or []
    if isinstance(pyramid_history, str):
        try:
            pyramid_history = json.loads(pyramid_history)
        except Exception:
            pyramid_history = []
    if not isinstance(pyramid_history, list):
        pyramid_history = []

    shares_sold = 0
    realized_pnl = 0.0
    entry_price_f = float(r.get("entry_price") or 0)
    for sell in sell_history:
        sh = int(sell.get("shares") or sell.get("qty") or 0)
        if sh <= 0:
            continue
        shares_sold += sh
        p = sell.get("profit")
        if p is None and entry_price_f > 0:
            sp = float(sell.get("price") or 0)
            p = round((sp - entry_price_f) * sh, 2)
        realized_pnl += float(p or 0)

    # Pyramid-aware sells book their shares + P&L into leg.exits[] (not the
    # main sell_history), so the display numbers need to fold them in. For
    # the user, "total shares bought" includes every leg (even the exited
    # ones), "shares sold" includes pyramid-origin shares, and realized P&L
    # includes pyramid-origin profits. Accounting-wise, quantity on the row
    # was already decremented when the pyramid leg was exited — we track
    # that via pyramid_exited_shares_sold below so shares_bought_total lines
    # up with what the user actually purchased over the trade's life.
    pyramid_exited_shares_sold = 0
    pyramid_realized_pnl = 0.0
    pyramid_exits_for_merge = []
    for i, leg in enumerate(pyramid_history):
        if not isinstance(leg, dict):
            continue
        exits = leg.get("exits") or []
        if not isinstance(exits, list):
            continue
        slot_label = (leg.get("slot") or f"p{i+1}").upper()
        for ex in exits:
            esh = int(ex.get("shares") or 0)
            if esh <= 0:
                continue
            pyramid_exited_shares_sold += esh
            pyramid_realized_pnl += float(ex.get("profit") or 0)
            # Shape into sell_history-compatible dict so the frontend's
            # Sell History table can render pyramid exits in-line with
            # regular sells — tagged via pyramid_slot for display.
            pyramid_exits_for_merge.append({
                "date": ex.get("date"),
                "shares": esh,
                "price": ex.get("price"),
                "profit": ex.get("profit"),
                "trigger": ex.get("trigger"),
                "pyramid_slot": slot_label,
                "origin": "pyramid_aware",
            })

    total_qty = int(r.get("quantity") or 0)
    # "Shares ever bought over the life of the trade" — quantity on the row
    # has been decremented by any pyramid-aware reversal, so add those back.
    shares_bought_total = total_qty + pyramid_exited_shares_sold
    shares_remaining = max(0, total_qty - shares_sold)
    shares_sold_total = shares_sold + pyramid_exited_shares_sold
    realized_pnl_total = round(realized_pnl + pyramid_realized_pnl, 2)

    r["shares_sold"] = shares_sold_total
    r["shares_remaining"] = shares_remaining
    r["shares_bought_total"] = shares_bought_total
    r["realized_pnl"] = realized_pnl_total
    # Preserve the regular-only slice for any UI that wants to distinguish.
    r["realized_pnl_from_partials"] = round(realized_pnl, 2)
    r["realized_pnl_from_pyramids"] = round(pyramid_realized_pnl, 2)
    r["has_partial_exits"] = (shares_sold_total > 0) and r.get("status") == "active"

    # Merge pyramid exits into sell_history for display. Sort by date asc so
    # the Sell History table reads chronologically regardless of which path
    # each exit came through. Keep the stored sell_history untouched —
    # `sell_history_display` is a derived field the frontend renders.
    if pyramid_exits_for_merge:
        merged = list(sell_history) + pyramid_exits_for_merge
        try:
            merged.sort(key=lambda e: str(e.get("date") or ""))
        except Exception:
            pass
        r["sell_history_display"] = merged
    else:
        r["sell_history_display"] = None

    # ═══ LIFETIME RETURN — honest denominator on closed trades ═══
    # Answers "% return on all the cash I ever put into this trade", which
    # diverges from total_pnl_pct when the user did a pyramid-aware sell
    # that snapped entry_price back to the pre-pyramid average.
    cost_outlay = r.get("total_cost_outlay")
    try:
        cost_outlay_f = float(cost_outlay) if cost_outlay is not None else None
    except (TypeError, ValueError):
        cost_outlay_f = None
    if cost_outlay_f and cost_outlay_f > 0:
        total_pnl_val = r.get("total_pnl")
        # Open positions don't have a stamped total_pnl — fall back to the
        # realized-from-partials figure we already computed so the user sees
        # an honest mid-trade number too.
        if total_pnl_val is None:
            total_pnl_val = realized_pnl
        try:
            lifetime_return = (float(total_pnl_val) / cost_outlay_f) * 100
            r["lifetime_return_pct"] = round(lifetime_return, 4)
        except (TypeError, ValueError):
            r["lifetime_return_pct"] = None
    else:
        r["lifetime_return_pct"] = None

    # ═══ RISK BREAKDOWN — per-leg risk that honours separate-SL pyramids ═══
    # Main pool = initial buy + avg-mode pyramid shares (all covered by the
    # single main/trailing SL). Separate-mode legs carry their own leg_sl and
    # account for their own rupee risk independently.
    #
    # Main-pool risk uses INITIAL_ENTRY_PRICE (not the blended avg) so
    # adding shares at a higher price doesn't inflate the original risk.
    # If initial_entry_price isn't set (legacy rows), fall back to entry.
    if r.get("status") == "active" and shares_remaining > 0:
        initial_entry_raw = r.get("initial_entry_price")
        try:
            initial_entry_f = float(initial_entry_raw) if initial_entry_raw is not None else entry_price_f
        except (TypeError, ValueError):
            initial_entry_f = entry_price_f
        main_sl_raw = r.get("trailing_sl") or r.get("stop_loss")
        try:
            main_sl = float(main_sl_raw) if main_sl_raw is not None else 0.0
        except (TypeError, ValueError):
            main_sl = 0.0
        separate_legs = []
        separate_shares_remaining = 0
        separate_risk_total = 0.0
        for i, leg in enumerate(pyramid_history):
            if not isinstance(leg, dict) or leg.get("mode") != "separate":
                continue
            lshares_total = int(leg.get("shares") or 0)
            lexited = int(leg.get("exited_shares") or 0)
            lremaining = max(0, lshares_total - lexited)
            if lremaining <= 0:
                continue
            lprice = float(leg.get("price") or 0)
            lsl_raw = leg.get("leg_sl")
            try:
                lsl = float(lsl_raw) if lsl_raw is not None else None
            except (TypeError, ValueError):
                lsl = None
            # No SL yet (e.g. trailing MA picked but daemon hasn't fired):
            # treat risk as 0 for now; tooltip will show 'pending'.
            leg_risk = 0.0
            if lsl is not None and lsl > 0 and lsl < lprice:
                leg_risk = round((lprice - lsl) * lremaining, 2)
            separate_legs.append({
                "slot": (leg.get("slot") or f"p{i+1}").upper(),
                "shares": lremaining,
                "entry": round(lprice, 2),
                "leg_sl": round(lsl, 2) if lsl else None,
                "trailing_ma": leg.get("trailing_ma"),
                "risk": leg_risk,
            })
            separate_shares_remaining += lremaining
            separate_risk_total += leg_risk

        # Main-pool shares = remaining shares NOT belonging to a separate leg.
        # They're covered by main_sl (or trailing_sl if tighter) and priced at
        # initial_entry_price so pyramids at higher prices don't retroactively
        # widen the main-pool risk.
        main_pool_shares = max(0, shares_remaining - separate_shares_remaining)
        main_risk = 0.0
        if main_sl > 0 and initial_entry_f > 0 and main_sl < initial_entry_f and main_pool_shares > 0:
            main_risk = round((initial_entry_f - main_sl) * main_pool_shares, 2)

        r["risk_breakdown"] = {
            "main": {
                "shares": main_pool_shares,
                "entry": round(initial_entry_f, 2) if initial_entry_f > 0 else None,
                "avg_entry": round(entry_price_f, 2) if entry_price_f > 0 else None,
                "sl": round(main_sl, 2) if main_sl > 0 else None,
                "risk": main_risk,
            },
            "pyramid_legs": separate_legs,
            "total": round(main_risk + separate_risk_total, 2),
        }
    else:
        r["risk_breakdown"] = None

    # ═══ LEG BREAKDOWN — closed trades show origin of each ₹ of P&L ═══
    # Per-leg P&L requires knowing which sells mapped to which leg. Pyramid-
    # aware sells stamp `leg.exits[]` with {shares, price, profit} so we
    # sum those per leg. Regular sells + remaining original leg P&L fold
    # into the "entry leg" bucket.
    if r.get("status") == "closed":
        leg_breakdown = []
        pyramid_aware_exited_shares = 0
        pyramid_aware_exited_cost = 0.0
        pyramid_aware_pnl = 0.0
        for i, leg in enumerate(pyramid_history):
            if not isinstance(leg, dict):
                continue
            exits = leg.get("exits") or []
            if not exits:
                continue
            lprice = float(leg.get("price") or 0)
            leg_pnl = sum(float(e.get("profit") or 0) for e in exits)
            leg_shares_exited = sum(int(e.get("shares") or 0) for e in exits)
            if leg_shares_exited <= 0 or lprice <= 0:
                continue
            leg_cost = lprice * leg_shares_exited
            leg_breakdown.append({
                "slot": (leg.get("slot") or f"p{i+1}").upper(),
                "shares": leg_shares_exited,
                "entry": round(lprice, 2),
                "pnl": round(leg_pnl, 2),
                "pnl_pct": round((leg_pnl / leg_cost) * 100, 2) if leg_cost else 0,
                "source": "pyramid_aware",
            })
            pyramid_aware_exited_shares += leg_shares_exited
            pyramid_aware_exited_cost += leg_cost
            pyramid_aware_pnl += leg_pnl

        # Entry-leg bucket = everything else that hit via regular sell_history.
        # Its cost basis = total_cost_outlay − pyramid-aware exited cost.
        remaining_pnl = float(r.get("total_pnl") or 0) - pyramid_aware_pnl
        remaining_cost = (cost_outlay_f or 0) - pyramid_aware_exited_cost
        if remaining_cost > 0:
            leg_breakdown.insert(0, {
                "slot": "ENTRY",
                "shares": total_qty - pyramid_aware_exited_shares,
                "entry": round(entry_price_f, 2) if entry_price_f > 0 else None,
                "pnl": round(remaining_pnl, 2),
                "pnl_pct": round((remaining_pnl / remaining_cost) * 100, 2) if remaining_cost else 0,
                "source": "regular",
            })
        r["leg_breakdown"] = leg_breakdown
    else:
        r["leg_breakdown"] = None


def _validate_position_invariants(cur, pos_id, user_id):
    """Load the current state of a position and assert all structural
    invariants hold. Called at the end of every write path BEFORE commit —
    if it raises InvariantError, the transaction rolls back and the write
    never lands.

    Invariants checked:
      - quantity > 0 and entry_price > 0
      - total_sold (Σ sell_history.shares) ≤ quantity
      - Each sell_history entry: shares > 0, price > 0
      - Each pyramid_history entry: shares > 0, price > 0
      - ≤3 sell_history entries, ≤2 pyramid_history entries
    """
    cur.execute(
        "SELECT entry_price, quantity, sell_history, pyramid_history, stock_name "
        "FROM positions WHERE id = %s AND user_id = %s",
        (pos_id, user_id),
    )
    row = cur.fetchone()
    if not row:
        return

    entry_price = float(row.get("entry_price") or 0)
    quantity = int(row.get("quantity") or 0)
    name = row.get("stock_name") or f"Position #{pos_id}"

    sh = row.get("sell_history") or []
    if isinstance(sh, str):
        try: sh = json.loads(sh)
        except Exception: sh = []
    ph = row.get("pyramid_history") or []
    if isinstance(ph, str):
        try: ph = json.loads(ph)
        except Exception: ph = []

    if quantity <= 0:
        raise InvariantError(f"{name}: quantity must be positive (got {quantity}).")
    if entry_price <= 0:
        raise InvariantError(f"{name}: entry_price must be positive (got {entry_price}).")

    total_sold = sum(int(e.get("shares") or e.get("qty") or e.get("quantity") or 0) for e in sh)
    if total_sold > quantity:
        raise InvariantError(
            f"{name}: cannot have sold {total_sold} shares — position only has {quantity} bought "
            f"(initial + pyramids). Reduce exits first, then lower the quantity."
        )

    if len(sh) > 3:
        raise InvariantError(f"{name}: max 3 exit slots (E1/E2/E3), got {len(sh)}.")
    for i, e in enumerate(sh):
        shares = int(e.get("shares") or e.get("qty") or e.get("quantity") or 0)
        price = float(e.get("price") or 0)
        if shares <= 0:
            raise InvariantError(f"{name}: E{i+1} shares must be positive (got {shares}).")
        if price <= 0:
            raise InvariantError(f"{name}: E{i+1} price must be positive (got {price}).")

    if len(ph) > 2:
        raise InvariantError(f"{name}: max 2 pyramid legs (P1/P2), got {len(ph)}.")
    for i, leg in enumerate(ph):
        shares = int(leg.get("shares") or 0)
        price = float(leg.get("price") or 0)
        if shares <= 0:
            raise InvariantError(f"{name}: P{i+1} shares must be positive (got {shares}).")
        if price <= 0:
            raise InvariantError(f"{name}: P{i+1} price must be positive (got {price}).")


@position_bp.route("/api/positions", methods=["POST"])
@limiter.limit("30 per minute")
def create_position():
    """Create a new tracked position"""
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    stock_name = data.get("stock_name")
    entry_price = data.get("entry_price")
    quantity = data.get("quantity")
    entry_date = data.get("entry_date") or (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")

    if not stock_name or not entry_price or not quantity:
        return jsonify({"error": "stock_name, entry_price, and quantity are required"}), 400

    try:
        entry_price = float(entry_price)
        quantity = int(quantity)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid entry_price or quantity"}), 400

    stop_loss = data.get("stop_loss")
    if stop_loss:
        stop_loss = float(stop_loss)

    # Compute risk_pct from actual SL distance if SL provided
    risk_pct = data.get("risk_pct", 4.0)
    if stop_loss and stop_loss < entry_price:
        risk_pct = round((entry_price - stop_loss) / entry_price * 100, 2)
    initial_sl = stop_loss or (entry_price * (1 - risk_pct / 100))
    position_value = entry_price * quantity
    one_r_value = position_value * (risk_pct / 100.0) if risk_pct else None

    # Market regime
    regime = data.get("market_regime", "bull")
    regime_source = data.get("regime_source", "manual")
    regime_params = get_regime_params(regime)

    conn = get_db()
    try:
        cur = conn.cursor()

        # Block duplicate: no two active positions with same stock name or security_id
        cur.execute(
            "SELECT id, stock_name FROM positions WHERE (LOWER(stock_name) = LOWER(%s) OR (security_id IS NOT NULL AND security_id = %s)) AND status = 'active' AND user_id = %s",
            (stock_name, data.get("security_id") or "", g.user_id)
        )
        existing = cur.fetchone()
        if existing:
            close_db(conn)
            return jsonify({
                "error": f"Active position for '{existing['stock_name']}' already exists (ID: {existing['id']}). Close it first.",
                "duplicate": True,
                "existing_id": existing["id"],
            }), 409

        # Event-risk guard: block entries within 7 days of the next board meeting
        # unless the client has explicitly acknowledged the event risk. Keeps a
        # trader from accidentally sizing up right before an earnings gap.
        # Client bypasses by resending the payload with confirm_event_risk=true.
        if data.get("security_id") and not data.get("confirm_event_risk"):
            try:
                # Pull the next meeting within the next ~14 calendar days
                # (cheap SQL pre-filter), then check trading-day distance
                # in Python so weekends/holidays don't quietly bypass the
                # 7-trading-day guard. e.g. if today is Thu and the meeting
                # is the following Mon, that's 1 trading day, not 4.
                cur.execute("""
                    SELECT meeting_date, purpose,
                           (meeting_date - CURRENT_DATE) AS days_left
                    FROM forthcoming_results
                    WHERE security_id = %s
                      AND meeting_date >= CURRENT_DATE
                      AND (meeting_date - CURRENT_DATE) <= 14
                      AND (purpose ILIKE %s
                           OR raw_purpose ILIKE %s
                           OR raw_purpose ILIKE %s
                           OR raw_purpose ILIKE %s)
                    ORDER BY meeting_date ASC,
                             (purpose ILIKE %s) DESC,
                             (purpose ILIKE %s) DESC
                    LIMIT 1
                """, (
                    str(data["security_id"]),
                    "%result%", "%financial result%", "%audited%", "%quarterly result%",
                    "%financial result%", "%result%",
                ))
                ev = cur.fetchone()
                if ev:
                    md = ev.get("meeting_date")
                    tdl = trading_days_until(md)
                    if tdl is not None and tdl <= 7:
                        close_db(conn)
                        return jsonify({
                            "error": "event_risk",
                            "event_risk": True,
                            "next_result_date": md.isoformat() if md and hasattr(md, "isoformat") else md,
                            "next_result_purpose": ev.get("purpose"),
                            "next_result_days_left": ev.get("days_left"),
                            "next_result_trading_days_left": tdl,
                            "message": "This stock reports results within 7 trading days. Resend with confirm_event_risk=true to proceed.",
                        }), 409
            except Exception as ev_err:
                # Soft-fail — if the forthcoming_results lookup breaks, don't block
                # legitimate entries; just skip the guard.
                print(f"⚠️ event-risk guard failed: {ev_err}")

        # Snapshot 20-day liquidity + market cap at entry. Hidden from the UI
        # but stored on positions + journal_trades for downstream analytics
        # ("did mid-cap entries do better than small-cap?"). Computed once
        # here and re-used by _auto_create_journal_entry below via the data
        # dict so both rows agree.
        try:
            from services.entry_metrics import compute_entry_metrics
            liq_at_entry, mcap_at_entry = compute_entry_metrics(
                data.get("security_id"), entry_date, conn=conn
            )
        except Exception as me:
            print(f"⚠ entry metrics compute failed (non-blocking): {me}")
            liq_at_entry, mcap_at_entry = (None, None)

        cur.execute('''
            INSERT INTO positions
            (stock_name, entry_price, initial_entry_price, stop_loss, initial_sl, quantity, position_value,
             one_r_value, risk_pct, source, submission_id,
             market_regime, regime_source,
             first_sell_zone, max_sell_pct, min_trail_pct,
             reversal_detect_from, daily_sell_from, bucket_cap,
             leg_base_price, valvo_ref_price, current_price,
             entry_chart_url, security_id,
             last_5ma_price, last_10ma_price, last_20ma_price,
             entry_date, total_cost_outlay, user_id,
             liquidity_at_entry, mcap_at_entry)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            stock_name, entry_price, entry_price, stop_loss, initial_sl, quantity, position_value,
            one_r_value, risk_pct,
            data.get("source", "manual"),
            data.get("submission_id"),
            regime, regime_source,
            regime_params["first_sell_zone"],
            regime_params["max_sell_pct"],
            regime_params["min_trail_pct"],
            regime_params["reversal_detect_from"],
            regime_params["daily_sell_from"],
            regime_params["bucket_cap"],
            data.get("leg_base_price", entry_price),
            data.get("valvo_ref_price", entry_price),
            data.get("current_price") or entry_price,
            data.get("entry_chart_url") or None,
            data.get("security_id") or None,
            data.get("five_ma"),
            data.get("ten_ma"),
            data.get("twenty_ma"),
            entry_date or None,
            # Lifetime cash-deployed running counter. Initial buy is the first
            # deposit; pyramids add to it; sells never subtract.
            round(entry_price * quantity, 2),
            g.user_id,
            liq_at_entry, mcap_at_entry,
        ))
        row = cur.fetchone()
        pos_id = row["id"]
        conn.commit()
        print(f"📊 Position created: {stock_name} @ ₹{entry_price} x {quantity} | regime={regime}")

        # Auto-create journal entry (non-blocking)
        try:
            _auto_create_journal_entry(cur, conn, pos_id, {
                "stock_name": stock_name,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "quantity": quantity,
                "security_id": data.get("security_id"),
                "entry_date": entry_date,
                "liquidity_at_entry": liq_at_entry,
                "mcap_at_entry": mcap_at_entry,
            }, g.user_id)
        except Exception as je:
            print(f"⚠ Journal auto-create failed (non-blocking): {je}")

        return jsonify({"id": pos_id, "created": True}), 201

    except Exception as e:
        conn.rollback()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/positions", methods=["GET"])
@limiter.limit("60 per minute")
def list_positions():
    """List all positions. ?status=active|closed|all (default: active).

    Partial exits (active positions with sell_history) stay in the Active view
    and carry `has_partial_exits=true` + `shares_remaining` / `shares_sold` /
    `realized_pnl` so the frontend can render a "PARTIAL POSITION" banner on
    their cards. The Closed view returns ONLY fully-closed trades — nothing
    with open shares shows up there.
    """
    status = request.args.get("status", "active")

    conn = get_db()
    try:
        cur = conn.cursor()
        # Hot cols (current_price, current_r_multiple, defensive_status,
        # last_5/10/20ma_price) live on positions_live sidecar. Listing them
        # after p.* means psycopg2's RealDictCursor takes the sidecar value
        # (last key wins on duplicate), falling back to NULL if no sidecar row.
        # next upcoming result date for this stock, from BSE/NSE forthcoming-
        # results feed. LATERAL so Postgres picks just the nearest future row
        # per position. Filtered to rows where the board meeting is actually
        # about financial results (dropping dividend-only / generic "Board
        # Meeting Intimation" rows), and when a scrip has multiple same-day
        # rows we prefer the one that explicitly names "Financial Results"
        # over a vague "Board Meeting Intimation" that happens to mention them.
        # Nullable — populated only for stocks whose security_id matches.
        next_result_join = '''
            LEFT JOIN LATERAL (
                SELECT fr.meeting_date, fr.purpose
                FROM forthcoming_results fr
                WHERE fr.security_id = p.security_id
                  AND fr.meeting_date >= CURRENT_DATE
                  AND (fr.purpose ILIKE '%%result%%'
                       OR fr.raw_purpose ILIKE '%%financial result%%'
                       OR fr.raw_purpose ILIKE '%%audited%%'
                       OR fr.raw_purpose ILIKE '%%quarterly result%%')
                ORDER BY fr.meeting_date ASC,
                         (fr.purpose ILIKE '%%financial result%%') DESC,
                         (fr.purpose ILIKE '%%result%%') DESC
                LIMIT 1
            ) nr ON TRUE
            LEFT JOIN LATERAL (
                SELECT fr.meeting_date, fr.purpose
                FROM forthcoming_results fr
                WHERE fr.security_id = p.security_id
                  AND fr.meeting_date < CURRENT_DATE
                  AND fr.meeting_date >= CURRENT_DATE - INTERVAL '10 days'
                  AND (fr.purpose ILIKE '%%result%%'
                       OR fr.raw_purpose ILIKE '%%financial result%%'
                       OR fr.raw_purpose ILIKE '%%audited%%'
                       OR fr.raw_purpose ILIKE '%%quarterly result%%')
                ORDER BY fr.meeting_date DESC,
                         (fr.purpose ILIKE '%%financial result%%') DESC,
                         (fr.purpose ILIKE '%%result%%') DESC
                LIMIT 1
            ) rr ON TRUE
            -- Fallback for stocks BSE never advertised an intimation for: use
            -- the date our pipeline first ingested the latest quarterly row
            -- as a proxy for "results dropped." Coalesced into recent_result_*
            -- below; only wins when forthcoming_results has nothing.
            LEFT JOIN LATERAL (
                WITH latest AS (
                    SELECT MAX(period_end_date) AS q
                    FROM financials_quarterly
                    WHERE security_id = p.security_id
                )
                SELECT MIN(fq.created_at)::date AS reported_at,
                       (SELECT q FROM latest) AS period_end
                FROM financials_quarterly fq, latest
                WHERE fq.security_id = p.security_id
                  AND fq.period_end_date = latest.q
                  AND fq.created_at >= CURRENT_DATE - INTERVAL '10 days'
            ) frq ON TRUE
        '''
        next_result_cols = (
            ', nr.meeting_date AS next_result_date'
            ', nr.purpose AS next_result_purpose'
            ", COALESCE(rr.meeting_date, frq.reported_at) AS recent_result_date"
            ", COALESCE(rr.purpose,"
            "          CASE WHEN frq.reported_at IS NOT NULL"
            "               THEN 'Quarterly Results (' || frq.period_end::text || ')'"
            "          END) AS recent_result_purpose"
        )

        # Latest candle close STRICTLY before today IST (yesterday's session
        # close). candles_daily ticks intraday so today's row would give us
        # the live price, not the prev close. Joined here so dashboard widgets
        # don't have to fetch a separate bulk-candles call just to compute
        # day P&L.
        prev_close_join = '''
            LEFT JOIN LATERAL (
                SELECT cd.close
                FROM candles_daily cd
                WHERE cd.security_id = p.security_id
                  AND cd.date < (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                  AND cd.volume > 0
                ORDER BY cd.date DESC
                LIMIT 1
            ) pc ON TRUE
        '''
        prev_close_cols = ', pc.close AS prev_close'

        # Universe join — positions only store stock_name (a denormalised label
        # at trade-entry time). The chart-mode list wants the canonical ticker
        # symbol + full company_name, so we resolve them here rather than
        # forcing the frontend to make a second bulk lookup per row.
        universe_join = '''
            LEFT JOIN stock_universe su ON su.security_id = p.security_id
        '''
        universe_cols = ', su.symbol AS symbol, su.company_name AS company_name'

        if status == "all":
            cur.execute(f'''
                SELECT p.*, pl.current_price, pl.current_r_multiple, pl.defensive_status,
                       pl.last_5ma_price, pl.last_10ma_price, pl.last_20ma_price
                       {prev_close_cols}
                       {universe_cols}
                       {next_result_cols}
                FROM positions p
                LEFT JOIN positions_live pl ON pl.position_id = p.id
                {prev_close_join}
                {universe_join}
                {next_result_join}
                WHERE p.user_id = %s ORDER BY p.created_at DESC
            ''', (g.user_id,))
        else:
            cur.execute(f'''
                SELECT p.*, pl.current_price, pl.current_r_multiple, pl.defensive_status,
                       pl.last_5ma_price, pl.last_10ma_price, pl.last_20ma_price
                       {prev_close_cols}
                       {universe_cols}
                       {next_result_cols}
                FROM positions p
                LEFT JOIN positions_live pl ON pl.position_id = p.id
                {prev_close_join}
                {universe_join}
                {next_result_join}
                WHERE p.status = %s AND p.user_id = %s ORDER BY p.created_at DESC
            ''', (status, g.user_id))
        rows = cur.fetchall()

        positions = []
        today = date.today()
        for row in rows:
            r = dict(row)
            recent_raw = r.get("recent_result_date")
            for key in ["created_at", "updated_at", "exit_date", "entry_date", "next_result_date", "recent_result_date"]:
                if r.get(key) and hasattr(r[key], "isoformat"):
                    r[key] = r[key].isoformat()

            # Trading-days countdown to next result. Used by PositionCard to
            # decide whether to show the amber event-risk glow (≤7 trading
            # days). Calendar-day diff is misleading on weekends + holidays.
            r["next_result_trading_days_left"] = (
                trading_days_until(r["next_result_date"])
                if r.get("next_result_date") else None
            )
            # Calendar days since the last announcement (within 10 days). The
            # "recent" pill is informational, so trading-days isn't needed here.
            r["recent_result_days_ago"] = (
                (today - recent_raw).days if recent_raw else None
            )

            _annotate_partial_exit_metadata(r)

            # Flag structurally-corrupt rows so the frontend can quarantine
            # their ₹-values from dashboard aggregates until the user fixes
            # the underlying data.
            issue = _detect_position_health_issue(r)
            if issue:
                r["health_issue"] = issue
            positions.append(r)

        return jsonify({"positions": positions})

    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/positions/<int:pos_id>", methods=["GET"])
@limiter.limit("60 per minute")
def get_position(pos_id):
    """Get single position with full data"""
    conn = get_db()
    try:
        cur = conn.cursor()
        # Hot cols come from positions_live sidecar (phase-1 split). Listed
        # after p.* so RealDictCursor's last-key-wins semantics pick the
        # sidecar values.
        cur.execute('''
            SELECT p.*, pl.current_price, pl.current_r_multiple, pl.defensive_status,
                   pl.last_5ma_price, pl.last_10ma_price, pl.last_20ma_price,
                   nr.meeting_date AS next_result_date, nr.purpose AS next_result_purpose,
                   COALESCE(rr.meeting_date, frq.reported_at) AS recent_result_date,
                   COALESCE(rr.purpose,
                            CASE WHEN frq.reported_at IS NOT NULL
                                 THEN 'Quarterly Results (' || frq.period_end::text || ')'
                            END) AS recent_result_purpose
            FROM positions p
            LEFT JOIN positions_live pl ON pl.position_id = p.id
            LEFT JOIN LATERAL (
                SELECT fr.meeting_date, fr.purpose
                FROM forthcoming_results fr
                WHERE fr.security_id = p.security_id
                  AND fr.meeting_date >= CURRENT_DATE
                  AND (fr.purpose ILIKE '%%result%%'
                       OR fr.raw_purpose ILIKE '%%financial result%%'
                       OR fr.raw_purpose ILIKE '%%audited%%'
                       OR fr.raw_purpose ILIKE '%%quarterly result%%')
                ORDER BY fr.meeting_date ASC,
                         (fr.purpose ILIKE '%%financial result%%') DESC,
                         (fr.purpose ILIKE '%%result%%') DESC
                LIMIT 1
            ) nr ON TRUE
            LEFT JOIN LATERAL (
                SELECT fr.meeting_date, fr.purpose
                FROM forthcoming_results fr
                WHERE fr.security_id = p.security_id
                  AND fr.meeting_date < CURRENT_DATE
                  AND fr.meeting_date >= CURRENT_DATE - INTERVAL '10 days'
                  AND (fr.purpose ILIKE '%%result%%'
                       OR fr.raw_purpose ILIKE '%%financial result%%'
                       OR fr.raw_purpose ILIKE '%%audited%%'
                       OR fr.raw_purpose ILIKE '%%quarterly result%%')
                ORDER BY fr.meeting_date DESC,
                         (fr.purpose ILIKE '%%financial result%%') DESC,
                         (fr.purpose ILIKE '%%result%%') DESC
                LIMIT 1
            ) rr ON TRUE
            -- See get_positions_list — financials_quarterly fallback for
            -- stocks BSE never advertised an intimation for.
            LEFT JOIN LATERAL (
                WITH latest AS (
                    SELECT MAX(period_end_date) AS q
                    FROM financials_quarterly
                    WHERE security_id = p.security_id
                )
                SELECT MIN(fq.created_at)::date AS reported_at,
                       (SELECT q FROM latest) AS period_end
                FROM financials_quarterly fq, latest
                WHERE fq.security_id = p.security_id
                  AND fq.period_end_date = latest.q
                  AND fq.created_at >= CURRENT_DATE - INTERVAL '10 days'
            ) frq ON TRUE
            WHERE p.id = %s AND p.user_id = %s
        ''', (pos_id, g.user_id))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Position not found"}), 404

        r = dict(row)
        recent_raw = r.get("recent_result_date")
        for key in ["created_at", "updated_at", "exit_date", "entry_date", "next_result_date", "recent_result_date"]:
            if r.get(key) and hasattr(r[key], "isoformat"):
                r[key] = r[key].isoformat()

        # Trading-days countdown — see /api/positions list handler above
        # for rationale (calendar days mislead across weekends + holidays).
        r["next_result_trading_days_left"] = (
            trading_days_until(r["next_result_date"])
            if r.get("next_result_date") else None
        )
        r["recent_result_days_ago"] = (
            (date.today() - recent_raw).days if recent_raw else None
        )

        # Auto-heal legacy rows that were saved with security_id = NULL (older
        # Valvo AI create_position flow didn't hard-stop on resolve failure).
        # Without this the chart silently hides on PositionDetail.
        if not r.get("security_id") and r.get("stock_name"):
            try:
                from services.valvo_ai_v2.catalog import _resolve_stock_reference
                resolved = _resolve_stock_reference(cur, symbol_hint=r["stock_name"])
                if resolved and resolved.get("security_id"):
                    cur.execute(
                        "UPDATE positions SET security_id = %s, updated_at = NOW() "
                        "WHERE id = %s AND user_id = %s AND security_id IS NULL",
                        (resolved["security_id"], pos_id, g.user_id),
                    )
                    conn.commit()
                    r["security_id"] = resolved["security_id"]
            except Exception as heal_err:
                print(f"[position] security_id auto-heal failed for pos {pos_id}: {heal_err}")

        # Link to the journal trade row so the frontend can edit/delete
        # individual partial sells (E1/E2/E3) inline without another round-trip.
        # Prefer created_at DESC over id DESC so that if a position ever ends
        # up with >1 journal row (shouldn't, but data migrations or manual
        # INSERTs can cause it), we pick the most recently created trade
        # rather than whichever got the highest auto-increment.
        cur.execute(
            'SELECT id FROM journal_trades WHERE position_id = %s AND user_id = %s '
            'ORDER BY created_at DESC NULLS LAST, id DESC LIMIT 1',
            (pos_id, g.user_id),
        )
        jt = cur.fetchone()
        r["linked_journal_trade_id"] = jt["id"] if jt else None

        # Get daily updates
        cur.execute('''
            SELECT * FROM position_daily_updates
            WHERE position_id = %s AND user_id = %s ORDER BY date DESC LIMIT 30
        ''', (pos_id, g.user_id))
        updates = []
        for u in cur.fetchall():
            ud = dict(u)
            if ud.get("created_at"):
                ud["created_at"] = ud["created_at"].isoformat()
            if ud.get("date"):
                ud["date"] = ud["date"].isoformat()
            updates.append(ud)

        r["daily_updates"] = updates
        issue = _detect_position_health_issue(r)
        if issue:
            r["health_issue"] = issue
        return jsonify(r)

    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/positions/<int:pos_id>", methods=["PUT"])
@limiter.limit("30 per minute")
def update_position(pos_id):
    """Update position fields (regime, prices, etc.)"""
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400

    # When trailing_mode is being set WITH a stop_loss value,
    # the SL value goes to trailing_sl (not stop_loss which is the original risk)
    if "trailing_mode" in data and data["trailing_mode"] in TRAILING_ACTIVE_MODES and "stop_loss" in data:
        tsl_val = float(data.pop("stop_loss"))
        # Floor: trailing SL never goes below entry price (cost protection)
        # Fetch entry_price to enforce floor
        conn_check = get_db()
        try:
            cur_check = conn_check.cursor()
            cur_check.execute("SELECT entry_price FROM positions WHERE id = %s AND user_id = %s", (pos_id, g.user_id))
            pos_row = cur_check.fetchone()
            if pos_row and pos_row["entry_price"]:
                tsl_val = max(tsl_val, float(pos_row["entry_price"]))
        finally:
            close_db(conn_check)
        data["trailing_sl"] = tsl_val

    # Build dynamic SET clause for only provided fields.
    # Deliberately omits 'exit_price' — closing a position must route through
    # record_sell (final partial) or close_position so sell_history gets the
    # final block + total_pnl is computed correctly + journal stays in sync.
    # A direct PUT with exit_price would leave journal e1/e2/e3 stale and
    # risk silent reopens on any later inline edit.
    allowed_fields = [
        "market_regime", "regime_source", "stop_loss", "trailing_sl", "leg_base_price",
        "valvo_ref_price", "current_price", "status", "defensive_status",
        "grace_day_used", "ma_followed", "ma_grade", "shakeout_count",
        "qualifies_for_trailing", "trailing_mode",
    ]
    sets = []
    values = []
    for field in allowed_fields:
        if field in data:
            sets.append(f"{field} = %s")
            values.append(data[field])

    # If regime changed, update regime params too
    if "market_regime" in data:
        rp = get_regime_params(data["market_regime"])
        for k, v in rp.items():
            sets.append(f"{k} = %s")
            values.append(v)

    if not sets:
        return jsonify({"error": "No valid fields to update"}), 400

    sets.append("updated_at = %s")
    values.append(datetime.now())
    values.append(pos_id)
    values.append(g.user_id)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE positions SET {', '.join(sets)} WHERE id = %s AND user_id = %s RETURNING id",
            tuple(values)
        )
        updated = cur.fetchone()

        # phase 2b: sync removed. journal's sl/tsl columns no longer auto-
        # update from position edits. For linked journal rows the journal
        # display JOINs positions, so this is purely a storage consideration.

        conn.commit()

        if not updated:
            return jsonify({"error": "Position not found"}), 404
        return jsonify({"updated": True, "id": pos_id})

    except Exception as e:
        conn.rollback()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/positions/<int:pos_id>", methods=["DELETE"])
@limiter.limit("15 per minute")
def delete_position(pos_id):
    """Delete a position and its linked journal entry."""
    conn = get_db()
    try:
        cur = conn.cursor()

        # Delete linked journal entry
        cur.execute('DELETE FROM journal_trades WHERE position_id = %s AND user_id = %s', (pos_id, g.user_id))

        # Drop equity-curve log rows for this position (FK is nullable; clean up explicitly)
        capital_log.delete_for_position(cur, g.user_id, pos_id)

        cur.execute('DELETE FROM positions WHERE id = %s AND user_id = %s RETURNING id', (pos_id, g.user_id))
        deleted = cur.fetchone()
        conn.commit()
        if not deleted:
            return jsonify({"error": "Position not found"}), 404
        return jsonify({"deleted": True, "id": pos_id})
    except Exception as e:
        conn.rollback()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# CHART ANALYSIS (MA Detection — Phase 2 will add full AI)
# ═══════════════════════════════════════════════════

@position_bp.route("/api/positions/<int:pos_id>/analyze", methods=["POST"])
@limiter.limit("30 per minute")
def analyze_chart(pos_id):
    """
    Upload a daily chart for AI analysis.
    Phase 1: stores chart + basic data.
    Phase 2: will add full MA-following analysis with Claude + Gemini.
    """
    if "chart" not in request.files:
        return jsonify({"error": "No chart file provided"}), 400

    close_price = request.form.get("close_price")
    five_ma = request.form.get("five_ma_price")
    ten_ma = request.form.get("ten_ma_price")
    twenty_ma = request.form.get("twenty_ma_price")

    # Save chart
    file = request.files["chart"]
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    temp_path = os.path.join(UPLOAD_FOLDER, f"pos_{pos_id}_{file.filename}")
    file.save(temp_path)

    # Upload to Supabase Storage
    chart_url = None
    try:
        from services.storage_service import upload_chart_image
        chart_url = upload_chart_image(temp_path, f"pos_{pos_id}")
    except Exception as e:
        print(f"⚠️ Chart upload failed: {e}")

    # Clean up temp
    try:
        os.remove(temp_path)
    except:
        pass

    conn = get_db()
    try:
        cur = conn.cursor()

        # Get position
        cur.execute('SELECT * FROM positions WHERE id = %s AND user_id = %s', (pos_id, g.user_id))
        pos = cur.fetchone()
        if not pos:
            return jsonify({"error": "Position not found"}), 404

        # Calculate extensions
        entry_price = pos["entry_price"]
        leg_base = pos["leg_base_price"] or entry_price
        valvo_ref = pos["valvo_ref_price"] or entry_price
        cp = float(close_price) if close_price else pos["current_price"]

        leg_ext = ((cp - leg_base) / leg_base * 100) if leg_base else 0
        entry_ext = ((cp - entry_price) / entry_price * 100) if entry_price else 0
        valvo_ext = ((cp - valvo_ref) / valvo_ref * 100) if valvo_ref else 0

        risk_per_share = entry_price * (pos["risk_pct"] / 100.0)
        r_multiple = ((cp - entry_price) / risk_per_share) if risk_per_share else 0

        # Defensive check
        defensive = "safe"
        if five_ma and cp:
            five_ma_f = float(five_ma)
            diff_pct = ((cp - five_ma_f) / five_ma_f) * 100
            if diff_pct < -1.0:
                defensive = "break"
            elif diff_pct < 0:
                defensive = "marginal"

        # Insert daily update
        cur.execute('''
            INSERT INTO position_daily_updates
            (position_id, close_price, five_ma_price, ten_ma_price, twenty_ma_price,
             defensive_result, leg_extension_pct, entry_extension_pct,
             valvo_extension_pct, r_multiple, chart_url, user_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        ''', (
            pos_id,
            cp if cp else None,
            float(five_ma) if five_ma else None,
            float(ten_ma) if ten_ma else None,
            float(twenty_ma) if twenty_ma else None,
            defensive,
            round(leg_ext, 2), round(entry_ext, 2),
            round(valvo_ext, 2), round(r_multiple, 2),
            chart_url,
            g.user_id,
        ))
        update_id = cur.fetchone()["id"]

        # Update position — cold fields only (extensions + chart URL). Hot
        # columns go to positions_live sidecar (phase-1 hot-cold split).
        cur.execute('''
            UPDATE positions SET
                leg_extension_pct = %s,
                entry_extension_pct = %s, valvo_extension_pct = %s,
                latest_chart_url = %s,
                updated_at = %s
            WHERE id = %s AND user_id = %s
        ''', (
            round(leg_ext, 2), round(entry_ext, 2),
            round(valvo_ext, 2),
            chart_url, datetime.now(), pos_id, g.user_id,
        ))
        cur.execute('''
            UPDATE positions_live SET
                current_price = %s, current_r_multiple = %s,
                defensive_status = %s,
                last_5ma_price = %s,
                updated_at = NOW()
            WHERE position_id = %s
        ''', (
            cp, round(r_multiple, 2), defensive,
            float(five_ma) if five_ma else None, pos_id,
        ))

        conn.commit()

        return jsonify({
            "update_id": update_id,
            "close_price": cp,
            "extensions": {
                "leg": round(leg_ext, 2),
                "entry": round(entry_ext, 2),
                "valvo": round(valvo_ext, 2),
            },
            "r_multiple": round(r_multiple, 2),
            "defensive": defensive,
            "chart_url": chart_url,
        })

    except Exception as e:
        conn.rollback()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# JOURNAL AUTO-SYNC (unified: create on add, update on sell/close)
# ═══════════════════════════════════════════════════

def _auto_create_journal_entry(cur, conn, pos_id, pos_data, user_id):
    """Create journal_trades entry when a position is CREATED. Core fields only — journal-specific fields left empty for user to fill."""
    cur.execute('SELECT id FROM journal_trades WHERE position_id = %s AND user_id = %s', (pos_id, user_id))
    if cur.fetchone():
        return  # Already exists

    cur.execute('SELECT COALESCE(MAX(trade_no), 0) + 1 as next_no FROM journal_trades WHERE user_id = %s', (user_id,))
    next_no = cur.fetchone()["next_no"]

    now = datetime.now()
    trade_date = pos_data.get("entry_date") or now
    cur.execute('''
        INSERT INTO journal_trades (trade_no, trade_date, symbol, name, buy_sell,
            security_id, entry_price, avg_entry, sl, initial_qty,
            position_id, notes, created_at, updated_at, user_id,
            liquidity_at_entry, mcap_at_entry)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (
        next_no, trade_date, pos_data["stock_name"], pos_data["stock_name"],
        'Buy', pos_data.get("security_id"),
        pos_data["entry_price"], pos_data["entry_price"], pos_data.get("stop_loss"),
        int(pos_data["quantity"]),
        pos_id, "Auto-created from Position Manager — fill setup & rating",
        now, now, user_id,
        pos_data.get("liquidity_at_entry"), pos_data.get("mcap_at_entry"),
    ))
    conn.commit()
    print(f"📓 Journal auto-create: {pos_data['stock_name']} → trade #{next_no} (incomplete)")


def _auto_sync_journal(cur, conn, pos, pos_id, sell_history, exit_price, user_id):
    """Update existing journal_trades entry with exit data. If no entry exists, create one."""
    cur.execute('SELECT id FROM journal_trades WHERE position_id = %s AND user_id = %s', (pos_id, user_id))
    existing = cur.fetchone()

    # Map sell_history to exit slots (e1, e2, e3)
    # Canonical key is "shares" (see sell_entry at line ~590). Fallback chain
    # handles legacy entries that used "qty" or "quantity".
    e1_price = e1_qty = e1_date = None
    e2_price = e2_qty = e2_date = None
    e3_price = e3_qty = e3_date = None

    def _get_shares(entry):
        return entry.get("shares") or entry.get("qty") or entry.get("quantity")

    if len(sell_history) >= 1:
        e1_price = sell_history[0].get("price")
        e1_qty = _get_shares(sell_history[0])
        e1_date = sell_history[0].get("date", "")[:10] if sell_history[0].get("date") else None
    if len(sell_history) >= 2:
        e2_price = sell_history[1].get("price")
        e2_qty = _get_shares(sell_history[1])
        e2_date = sell_history[1].get("date", "")[:10] if sell_history[1].get("date") else None
    if len(sell_history) >= 3:
        e3_price = sell_history[2].get("price")
        e3_qty = _get_shares(sell_history[2])
        e3_date = sell_history[2].get("date", "")[:10] if sell_history[2].get("date") else None

    now = datetime.now()

    if existing:
        # UPDATE existing journal entry with exit data
        cur.execute('''
            UPDATE journal_trades SET
                e1_price = %s, e1_qty = %s, e1_date = %s,
                e2_price = %s, e2_qty = %s, e2_date = %s,
                e3_price = %s, e3_qty = %s, e3_date = %s,
                updated_at = %s
            WHERE id = %s AND user_id = %s
        ''', (
            e1_price, e1_qty, e1_date,
            e2_price, e2_qty, e2_date,
            e3_price, e3_qty, e3_date,
            now, existing["id"], user_id,
        ))
        conn.commit()
        print(f"📓 Journal updated: {pos['stock_name']} exits synced")
    else:
        # CREATE new entry (fallback — position was added before this feature)
        cur.execute('SELECT COALESCE(MAX(trade_no), 0) + 1 as next_no FROM journal_trades WHERE user_id = %s', (user_id,))
        next_no = cur.fetchone()["next_no"]
        entry = pos["entry_price"]
        # Reuse position's stored snapshot when present; fall back to a
        # fresh compute for legacy rows that pre-date the column.
        liq_at_entry = pos.get("liquidity_at_entry")
        mcap_at_entry = pos.get("mcap_at_entry")
        if liq_at_entry is None and mcap_at_entry is None and pos.get("security_id") and pos.get("entry_date"):
            try:
                from services.entry_metrics import compute_entry_metrics
                liq_at_entry, mcap_at_entry = compute_entry_metrics(
                    pos.get("security_id"), pos.get("entry_date"), conn=conn
                )
            except Exception as me:
                print(f"⚠ entry metrics compute failed (non-blocking): {me}")
        cur.execute('''
            INSERT INTO journal_trades (trade_no, trade_date, symbol, name, entry_type, buy_sell,
                security_id, entry_price, avg_entry, sl, initial_qty,
                e1_price, e1_qty, e1_date, e2_price, e2_qty, e2_date, e3_price, e3_qty, e3_date,
                position_id, notes, created_at, updated_at, user_id,
                liquidity_at_entry, mcap_at_entry)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s)
        ''', (
            next_no, pos.get("created_at", now), pos["stock_name"], pos["stock_name"],
            'BREAKOUT', 'Buy', pos.get("security_id"),
            entry, entry, pos.get("stop_loss"), int(pos["quantity"]),
            e1_price, e1_qty, e1_date, e2_price, e2_qty, e2_date, e3_price, e3_qty, e3_date,
            pos_id, "Auto-synced from Position Manager on close",
            now, now, user_id,
            liq_at_entry, mcap_at_entry,
        ))
        conn.commit()
        print(f"📓 Journal auto-sync (new): {pos['stock_name']} → trade #{next_no}")


# ═══════════════════════════════════════════════════
# SELL RECORDING
# ═══════════════════════════════════════════════════

@position_bp.route("/api/positions/<int:pos_id>/sell", methods=["POST"])
@limiter.limit("30 per minute")
def record_sell(pos_id):
    """Record a sell action — updates bucket and sell history.

    When the client passes target_pyramid_idx (the index of the leg in
    pyramid_history being exited), this is a pyramid-aware sell:
      - P&L is booked against the leg's own buy price, not the blended entry.
      - entry_price (and SL, if the leg was added in 'avg' mode) is snapped
        back to the pre-pyramid weighted average by subtracting the leg's
        contribution. If the user sells exactly the leg's qty, the position
        returns to its pre-pyramid state.
      - The leg is marked exited_shares += shares_this_sell.
      - bucket_sold_pct / sell_history are NOT touched — the pyramid shares
        never hit the exit bucket in this model; they unwind the leg itself.
    """
    data = request.json
    sell_pct = data.get("sell_pct")
    sell_price = data.get("sell_price")
    trigger = data.get("trigger", "manual")
    target_pyramid_idx = data.get("target_pyramid_idx")

    if not sell_pct or not sell_price:
        return jsonify({"error": "sell_pct and sell_price required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM positions WHERE id = %s AND user_id = %s', (pos_id, g.user_id))
        pos = cur.fetchone()
        if not pos:
            return jsonify({"error": "Position not found"}), 404

        sell_pct = float(sell_pct)
        sell_price = float(sell_price)

        # ═══ PYRAMID-AWARE SELL PATH ═══
        if target_pyramid_idx is not None:
            try:
                leg_idx = int(target_pyramid_idx)
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid target_pyramid_idx"}), 400
            pyr = pos.get("pyramid_history") or []
            if isinstance(pyr, str):
                try: pyr = json.loads(pyr)
                except Exception: pyr = []
            if leg_idx < 0 or leg_idx >= len(pyr):
                return jsonify({"error": f"No pyramid leg at index {leg_idx}"}), 400
            leg = pyr[leg_idx]
            leg_shares = int(leg.get("shares") or 0)
            leg_exited = int(leg.get("exited_shares") or 0)
            leg_remaining = leg_shares - leg_exited
            if leg_remaining <= 0:
                return jsonify({"error": "Pyramid leg already fully exited"}), 400
            # Resolve shares-to-sell — same logic as regular path but bounded
            # by the leg's remaining qty (not the whole position).
            pos_qty = int(pos["quantity"])
            requested_shares = int(pos_qty * sell_pct / 100)
            if requested_shares <= 0 or requested_shares > leg_remaining:
                return jsonify({
                    "error": f"Pyramid-leg sell must be 1..{leg_remaining} shares; got {requested_shares}",
                    "leg_remaining": leg_remaining,
                }), 400

            leg_price = float(leg.get("price") or 0)
            leg_mode = leg.get("mode") or "avg"
            leg_sl_on_leg = leg.get("leg_sl")
            shares_this_sell = requested_shares
            pnl_this = round((sell_price - leg_price) * shares_this_sell, 2)

            # Reverse the leg's contribution to entry_price and (avg mode) SL.
            current_entry = float(pos["entry_price"])
            current_sl = float(pos["stop_loss"]) if pos.get("stop_loss") else None
            new_qty = pos_qty - shares_this_sell
            if new_qty > 0:
                new_entry = round(((current_entry * pos_qty) - (leg_price * shares_this_sell)) / new_qty, 2)
                if leg_mode == "avg" and current_sl is not None and leg_sl_on_leg is not None:
                    new_sl = round(((current_sl * pos_qty) - (float(leg_sl_on_leg) * shares_this_sell)) / new_qty, 2)
                else:
                    new_sl = current_sl
            else:
                # Selling the last shares — position fully closes. Pre-close
                # entry/SL don't matter for remaining math but we keep them
                # so history reads are sane.
                new_entry = current_entry
                new_sl = current_sl

            # Leg bookkeeping
            pyr[leg_idx]["exited_shares"] = leg_exited + shares_this_sell
            pyr[leg_idx].setdefault("exits", []).append({
                "date": datetime.now().isoformat(),
                "shares": shares_this_sell,
                "price": sell_price,
                "profit": pnl_this,
                "trigger": trigger,
            })

            # Risk recompute on the new base.
            new_value = round(new_entry * new_qty, 2) if new_qty > 0 else 0
            new_risk_pct = float(pos.get("risk_pct") or 4.0)
            if new_sl and new_entry > 0 and new_sl < new_entry:
                new_risk_pct = round((new_entry - new_sl) / new_entry * 100, 2)
            new_one_r = round(new_value * (new_risk_pct / 100.0), 2) if new_risk_pct else None

            now = datetime.now()
            is_fully_sold = (new_qty == 0)
            if is_fully_sold:
                # Full close triggered by pyramid exit that happens to zero
                # out the position (rare — e.g. pyramid > original). Stamp
                # close fields like the regular path does.
                total_pnl = pnl_this + sum(float(s.get("profit") or 0) for s in (pos.get("sell_history") or []))
                cost_basis = current_entry * pos_qty
                total_pnl_pct = (total_pnl / cost_basis * 100) if cost_basis else 0
                cur.execute("SET LOCAL app.syncing_from_journal = 'true'")
                cur.execute('''
                    UPDATE positions SET
                        quantity = 0, pyramid_history = %s,
                        status = 'closed', exit_price = %s, exit_date = %s,
                        total_pnl = %s, total_pnl_pct = %s,
                        updated_at = %s
                    WHERE id = %s AND user_id = %s
                ''', (
                    json.dumps(pyr), sell_price, now,
                    round(total_pnl, 2), round(total_pnl_pct, 4), now,
                    pos_id, g.user_id,
                ))
            else:
                cur.execute("SET LOCAL app.syncing_from_journal = 'true'")
                cur.execute('''
                    UPDATE positions SET
                        quantity = %s, entry_price = %s, stop_loss = %s,
                        position_value = %s, risk_pct = %s, one_r_value = %s,
                        pyramid_history = %s, updated_at = %s
                    WHERE id = %s AND user_id = %s
                ''', (
                    new_qty, new_entry, new_sl,
                    new_value, new_risk_pct, new_one_r,
                    json.dumps(pyr), now,
                    pos_id, g.user_id,
                ))

            _validate_position_invariants(cur, pos_id, g.user_id)
            capital_log.rebuild_for_position(cur, g.user_id, pos_id)
            if is_fully_sold:
                try:
                    from services.rationale_service import maybe_create_prompt
                    maybe_create_prompt(cur, g.user_id, pos_id)
                except Exception as _re:
                    print(f"[position] rationale hook (pyramid-final) skipped: {_re}")
            conn.commit()
            print(f"🔻 Pyramid sell: leg {leg_idx} ({leg.get('slot')}) {shares_this_sell}@₹{sell_price} | P&L ₹{pnl_this} | entry ₹{current_entry}→₹{new_entry}")
            return jsonify({
                "sold_from_pyramid": True,
                "pyramid_idx": leg_idx,
                "shares_sold": shares_this_sell,
                "leg_price": leg_price,
                "sell_price": sell_price,
                "realized_pnl": pnl_this,
                "new_quantity": new_qty,
                "new_entry_price": new_entry,
                "new_stop_loss": new_sl,
                "closed": is_fully_sold,
            })

        current_sold = pos["bucket_sold_pct"] or 0
        bucket_cap = pos["bucket_cap"] or 66.0
        allow_overflow = bool(data.get("final_exit"))

        # Load existing sell_history early — needed for slot-count enforcement
        sell_history = pos["sell_history"] or []
        if isinstance(sell_history, str):
            sell_history = json.loads(sell_history)

        # Enforce max 3 exit slots (E1, E2, E3)
        if len(sell_history) >= 3:
            return jsonify({"error": "Already at max 3 exits (E1, E2, E3). Close the position or edit the journal."}), 400

        # Bucket cap is a soft guard — skip when client flags this as a final exit
        if not allow_overflow and current_sold + sell_pct > bucket_cap + 0.01:
            return jsonify({
                "error": f"Cannot sell {sell_pct}% — bucket cap is {bucket_cap}%, already sold {current_sold}%",
                "max_available": round(bucket_cap - current_sold, 2),
            }), 400

        # Calculate R and extension at sell
        entry = pos["entry_price"]
        risk_per_share = entry * (pos["risk_pct"] / 100.0)
        r_at_sell = ((sell_price - entry) / risk_per_share) if risk_per_share else 0
        ext_at_sell = ((sell_price - entry) / entry * 100) if entry else 0

        # Compute shares sold so-far and remaining to sell — needed to handle final-exit remainder precisely
        pos_qty = int(pos["quantity"])
        already_sold_shares = sum(int(s.get("shares") or s.get("qty") or 0) for s in sell_history)
        remaining_shares = max(0, pos_qty - already_sold_shares)

        # Final-exit flag from the client snaps the sell to all remaining shares (exactly closes the position)
        if allow_overflow:
            shares_this_sell = remaining_shares
            sell_pct = round((remaining_shares / pos_qty) * 100, 2) if pos_qty else sell_pct
        else:
            # Phase 2b-bis: error out on over-sell instead of silently capping.
            # Silent cap lets the user think they sold more than they did;
            # explicit error lets the UI surface a clear "only X shares left"
            # message or prompt for final_exit=true.
            requested_shares = int(pos_qty * sell_pct / 100)
            if requested_shares <= 0:
                return jsonify({"error": f"sell_pct {sell_pct} resolves to 0 shares — nothing to sell"}), 400
            if requested_shares > remaining_shares:
                return jsonify({
                    "error": f"Cannot sell {requested_shares} shares — only {remaining_shares} remaining. "
                             f"Use final_exit=true to sell all remaining, or reduce sell_pct.",
                    "remaining_shares": remaining_shares,
                }), 400
            shares_this_sell = requested_shares

        sell_entry = {
            "date": datetime.now().isoformat(),
            "pct": sell_pct,
            "price": sell_price,
            "extension": round(ext_at_sell, 2),
            "r_multiple": round(r_at_sell, 2),
            "trigger": trigger,
            "shares": shares_this_sell,
            "profit": round((sell_price - entry) * shares_this_sell, 2),
        }

        sell_history.append(sell_entry)

        # Total sold-shares after this sale
        total_sold_shares = already_sold_shares + shares_this_sell
        new_sold = round((total_sold_shares / pos_qty) * 100, 2) if pos_qty else current_sold + sell_pct
        trail_remaining = max(0.0, round(100.0 - new_sold, 2))
        first_sell_done = True

        # Close when total sold shares == total bought shares
        is_fully_sold = total_sold_shares >= pos_qty

        if is_fully_sold:
            # Close the position and auto-sync to journal.
            # total_pnl_pct = return on cost basis, NOT the last sell's price
            # move. Multi-partial trades otherwise would stamp a misleading
            # percentage — e.g. partials at +20% / +10% / +5% would show 5%
            # instead of the actual blended ~11.67% return.
            total_pnl = sum(s.get("profit", 0) for s in sell_history)
            cost_basis = entry * pos_qty if entry and pos_qty else 0
            total_pnl_pct = (total_pnl / cost_basis * 100) if cost_basis else 0
            # current_r_multiple is updated every live tick by
            # sync_live_price_to_dependents while the position is active; on
            # close we null it so the display doesn't show a stale peak-R
            # number on a trade that's already booked.
            cur.execute('''
                UPDATE positions SET
                    bucket_sold_pct = %s, trail_remaining_pct = 0,
                    first_sell_done = %s, sell_history = %s,
                    status = 'closed', exit_price = %s, exit_date = %s,
                    total_pnl = %s, total_pnl_pct = %s,
                    updated_at = %s
                WHERE id = %s AND user_id = %s
            ''', (
                new_sold, first_sell_done, json.dumps(sell_history),
                sell_price, datetime.now(), round(total_pnl, 2),
                round(total_pnl_pct, 2), datetime.now(),
                pos_id, g.user_id,
            ))
            # Hot sidecar: pin current_price to the sell price + null current_r_multiple
            # (phase-1 split — live trigger writes only to positions_live)
            cur.execute('''
                UPDATE positions_live SET
                    current_price = %s, current_r_multiple = NULL, updated_at = NOW()
                WHERE position_id = %s
            ''', (sell_price, pos_id))
        else:
            cur.execute('''
                UPDATE positions SET
                    bucket_sold_pct = %s, trail_remaining_pct = %s,
                    first_sell_done = %s, sell_history = %s,
                    updated_at = %s
                WHERE id = %s AND user_id = %s
            ''', (
                new_sold, trail_remaining, first_sell_done,
                json.dumps(sell_history), datetime.now(), pos_id, g.user_id,
            ))
        _validate_position_invariants(cur, pos_id, g.user_id)
        capital_log.rebuild_for_position(cur, g.user_id, pos_id)
        if is_fully_sold:
            try:
                from services.rationale_service import maybe_create_prompt
                maybe_create_prompt(cur, g.user_id, pos_id)
            except Exception as _re:
                print(f"[position] rationale hook (sell-final) skipped: {_re}")
        conn.commit()

        print(f"💰 Sell recorded: {pos['stock_name']} — {sell_pct}% @ ₹{sell_price} | bucket: {new_sold}/{bucket_cap}%{' [CLOSED]' if is_fully_sold else ''}")

        # phase 2b: journal e1/e2/e3 no longer written from here. Journal
        # display JOINs positions.sell_history for linked rows. Sync retired.

        return jsonify({
            "recorded": True,
            "sell_entry": sell_entry,
            "bucket_sold_pct": new_sold,
            "bucket_cap": bucket_cap,
            "trail_remaining_pct": trail_remaining,
            "closed": is_fully_sold,
        })

    except InvariantError as ie:
        conn.rollback()
        return jsonify({"error": str(ie)}), 400
    except Exception as e:
        conn.rollback()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# EDIT / DELETE a specific sell_history slot (Phase 2b)
# ═══════════════════════════════════════════════════
# These endpoints replace the old round-trip via PUT /api/journal/trades/<id>
# with e*_price/qty/date (which relied on sync_journal_trade_to_position to
# rebuild positions.sell_history from the journal slots). With sync removed,
# positions owns sell_history directly — edit/delete happen here.

def _recompute_after_sellhistory_change(cur, pos_id, user_id, sell_history):
    """Re-derive bucket_sold_pct, trail_remaining_pct, first_sell_done,
    status / exit_price / exit_date / total_pnl / total_pnl_pct from the new
    sell_history. Used by both edit and delete sell-slot endpoints."""
    cur.execute('SELECT entry_price, quantity FROM positions WHERE id = %s AND user_id = %s', (pos_id, user_id))
    row = cur.fetchone()
    if not row:
        return
    entry = float(row["entry_price"] or 0)
    qty = int(row["quantity"] or 0)
    total_sold = sum(int(e.get("shares") or e.get("qty") or e.get("quantity") or 0) for e in sell_history)
    total_profit = sum(float(e.get("profit") or 0) for e in sell_history)
    bucket_pct = round((total_sold / qty) * 100, 2) if qty > 0 else 0
    trail_pct = max(0.0, round(100.0 - bucket_pct, 2))
    first_sell_done = bool(sell_history)

    if qty > 0 and total_sold >= qty:
        # Fully sold — mark closed
        last_price = sell_history[-1].get("price") if sell_history else None
        last_date = sell_history[-1].get("date") if sell_history else datetime.now().isoformat()
        cost_basis = entry * qty if entry > 0 else 0
        total_pnl_pct = (total_profit / cost_basis * 100) if cost_basis else 0
        cur.execute('''
            UPDATE positions SET
                sell_history = %s, bucket_sold_pct = %s, trail_remaining_pct = 0,
                first_sell_done = %s, status = 'closed',
                exit_price = %s, exit_date = %s,
                total_pnl = %s, total_pnl_pct = %s, updated_at = NOW()
            WHERE id = %s AND user_id = %s
        ''', (
            json.dumps(sell_history), bucket_pct, first_sell_done,
            last_price, last_date, round(total_profit, 2),
            round(total_pnl_pct, 2), pos_id, user_id,
        ))
        if last_price is not None:
            cur.execute(
                'UPDATE positions_live SET current_price = %s, current_r_multiple = NULL, updated_at = NOW() WHERE position_id = %s',
                (last_price, pos_id),
            )
    else:
        # Still open (or re-opened from closed after deletion)
        cur.execute('''
            UPDATE positions SET
                sell_history = %s, bucket_sold_pct = %s, trail_remaining_pct = %s,
                first_sell_done = %s, status = 'active',
                exit_price = NULL, exit_date = NULL, total_pnl = NULL, total_pnl_pct = NULL,
                updated_at = NOW()
            WHERE id = %s AND user_id = %s
        ''', (
            json.dumps(sell_history), bucket_pct, trail_pct, first_sell_done,
            pos_id, user_id,
        ))
        # On re-open, reset hot sidecar current_price to entry (live tick will refresh)
        cur.execute(
            'UPDATE positions_live SET current_price = %s, updated_at = NOW() WHERE position_id = %s',
            (entry, pos_id),
        )


def _recompute_after_pyramid_history_change(cur, pos_id, user_id, new_pyramid_history):
    """Recompute position state after pyramid_history was edited.

    Anchors cost basis on entry_price × quantity (the current weighted-avg
    state of the position). total_cost_outlay is unreliable on positions
    pyramided before that column was tracked, and using it leads to garbage
    entry_price after a no-op recompute. Used by journal-side edits of
    P1/P2 cells; record_pyramid's append path still uses its own inline math.

    For stop_loss: blends initial_sl with avg-mode legs' leg_sl weighted by
    bought qty (approximation that matches record_pyramid exactly when there
    have been no sells, and is the same shape otherwise). Separate-mode legs
    leave position.stop_loss untouched.
    """
    cur.execute(
        '''SELECT quantity, entry_price, stop_loss, total_cost_outlay,
                  pyramid_history, risk_pct
           FROM positions WHERE id = %s AND user_id = %s''',
        (pos_id, user_id),
    )
    pos = cur.fetchone()
    if not pos:
        return

    old_quantity = int(pos["quantity"] or 0)
    old_entry = float(pos["entry_price"] or 0)
    old_sl = float(pos["stop_loss"]) if pos.get("stop_loss") else None
    old_outlay = old_entry * old_quantity

    old_pyr = pos.get("pyramid_history") or []
    if isinstance(old_pyr, str):
        try: old_pyr = json.loads(old_pyr)
        except Exception: old_pyr = []

    old_pyr_qty = sum(int(l.get("shares") or 0) for l in old_pyr)
    old_pyr_outlay = sum(float(l.get("price") or 0) * int(l.get("shares") or 0) for l in old_pyr)
    initial_qty = max(0, old_quantity - old_pyr_qty)
    initial_outlay = max(0.0, old_outlay - old_pyr_outlay)

    new_pyr_qty = sum(int(l.get("shares") or 0) for l in new_pyramid_history)
    new_pyr_outlay = sum(float(l.get("price") or 0) * int(l.get("shares") or 0) for l in new_pyramid_history)
    new_quantity = initial_qty + new_pyr_qty
    new_outlay = initial_outlay + new_pyr_outlay

    new_entry = round(new_outlay / new_quantity, 2) if new_quantity > 0 else old_entry

    new_sl = old_sl
    if old_sl is not None:
        # Back-derive the initial-leg SL contribution from the current
        # position.stop_loss, assuming the running blend is over
        # (initial_qty + Σ old avg-mode shares).
        old_avg_qty = sum(int(l.get("shares") or 0) for l in old_pyr if l.get("mode") == "avg")
        old_avg_sl_outlay = sum(
            float(l.get("leg_sl") or 0) * int(l.get("shares") or 0)
            for l in old_pyr if l.get("mode") == "avg" and l.get("leg_sl") is not None
        )
        denom_old = initial_qty + old_avg_qty
        if denom_old > 0:
            initial_sl_outlay = (old_sl * denom_old) - old_avg_sl_outlay
        else:
            initial_sl_outlay = old_sl * initial_qty if initial_qty > 0 else 0.0

        new_avg_qty = sum(int(l.get("shares") or 0) for l in new_pyramid_history if l.get("mode") == "avg")
        new_avg_sl_outlay = sum(
            float(l.get("leg_sl") or 0) * int(l.get("shares") or 0)
            for l in new_pyramid_history if l.get("mode") == "avg" and l.get("leg_sl") is not None
        )
        denom_new = initial_qty + new_avg_qty
        if denom_new > 0:
            new_sl = round((initial_sl_outlay + new_avg_sl_outlay) / denom_new, 2)

    new_position_value = round(new_entry * new_quantity, 2) if new_quantity > 0 else 0.0
    new_risk_pct = float(pos.get("risk_pct") or 0.0)
    new_one_r_value = None
    if new_sl is not None and new_sl > 0 and new_entry > new_sl:
        new_risk_pct = round((new_entry - new_sl) / new_entry * 100, 2)
        new_one_r_value = round((new_entry - new_sl) * new_quantity, 2)

    # Silence the reverse trigger — we're writing entry_price and quantity
    # authoritatively from the new pyramid_history, mirror would clobber.
    cur.execute("SET LOCAL app.syncing_from_journal = 'true'")
    cur.execute(
        '''UPDATE positions SET
              quantity = %s, entry_price = %s, stop_loss = %s,
              position_value = %s, risk_pct = %s, one_r_value = %s,
              pyramid_history = %s, total_cost_outlay = %s,
              updated_at = NOW()
           WHERE id = %s AND user_id = %s''',
        (
            new_quantity, new_entry, new_sl,
            new_position_value, new_risk_pct, new_one_r_value,
            json.dumps(new_pyramid_history), round(new_outlay, 2),
            pos_id, user_id,
        ),
    )


@position_bp.route("/api/positions/<int:pos_id>/sell/<int:idx>", methods=["PUT"])
@limiter.limit("30 per minute")
def edit_sell_slot(pos_id, idx):
    """Edit a specific entry in positions.sell_history by index."""
    data = request.json or {}
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT entry_price, sell_history FROM positions WHERE id = %s AND user_id = %s', (pos_id, g.user_id))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Position not found"}), 404
        sell_history = row["sell_history"] or []
        if isinstance(sell_history, str):
            try: sell_history = json.loads(sell_history)
            except Exception: sell_history = []
        if idx < 0 or idx >= len(sell_history):
            return jsonify({"error": f"Invalid sell slot index {idx} (have {len(sell_history)})"}), 400

        entry_existing = sell_history[idx]
        price = data.get("price")
        shares = data.get("shares")
        date_str = data.get("date")
        # Validate input values before mutating
        if price is not None:
            if not (float(price) > 0):
                return jsonify({"error": f"price must be positive (got {price})"}), 400
            entry_existing["price"] = float(price)
        if shares is not None:
            if not (int(shares) > 0):
                return jsonify({"error": f"shares must be positive (got {shares})"}), 400
            entry_existing["shares"] = int(shares)
        if date_str is not None: entry_existing["date"] = date_str

        # Re-derive profit + extension from updated price/shares
        entry_price = float(row["entry_price"] or 0)
        new_price = float(entry_existing.get("price") or 0)
        new_shares = int(entry_existing.get("shares") or 0)
        if entry_price > 0 and new_price > 0:
            entry_existing["profit"] = round((new_price - entry_price) * new_shares, 2)
            entry_existing["extension"] = round(((new_price - entry_price) / entry_price) * 100, 2)

        sell_history[idx] = entry_existing
        _recompute_after_sellhistory_change(cur, pos_id, g.user_id, sell_history)
        _validate_position_invariants(cur, pos_id, g.user_id)
        capital_log.rebuild_for_position(cur, g.user_id, pos_id)
        conn.commit()
        return jsonify({"updated": True, "sell_history": sell_history})
    except InvariantError as ie:
        conn.rollback()
        return jsonify({"error": str(ie)}), 400
    except Exception as e:
        conn.rollback()
        print(f"[position] edit_sell_slot error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/positions/<int:pos_id>/sell/<int:idx>", methods=["DELETE"])
@limiter.limit("15 per minute")
def delete_sell_slot(pos_id, idx):
    """Remove a specific entry from positions.sell_history. If the position
    was closed and this deletion drops total_sold below quantity, status flips
    back to active."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT sell_history FROM positions WHERE id = %s AND user_id = %s', (pos_id, g.user_id))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Position not found"}), 404
        sell_history = row["sell_history"] or []
        if isinstance(sell_history, str):
            try: sell_history = json.loads(sell_history)
            except Exception: sell_history = []
        if idx < 0 or idx >= len(sell_history):
            return jsonify({"error": f"Invalid sell slot index {idx} (have {len(sell_history)})"}), 400

        sell_history.pop(idx)
        _recompute_after_sellhistory_change(cur, pos_id, g.user_id, sell_history)
        _validate_position_invariants(cur, pos_id, g.user_id)
        capital_log.rebuild_for_position(cur, g.user_id, pos_id)
        conn.commit()
        return jsonify({"deleted": True, "sell_history": sell_history})
    except InvariantError as ie:
        conn.rollback()
        return jsonify({"error": str(ie)}), 400
    except Exception as e:
        conn.rollback()
        print(f"[position] delete_sell_slot error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# PYRAMID (add to existing position)
# ═══════════════════════════════════════════════════

@position_bp.route("/api/positions/<int:pos_id>/pyramid", methods=["POST"])
@limiter.limit("30 per minute")
def record_pyramid(pos_id):
    """Add shares to an existing position.

    Two pyramid modes (user picks on the UI; default is picked by extension):
      - mode='avg': weighted-average BOTH entry_price AND stop_loss across
        old + new shares. Preserves risk% (same SL distance from entry).
        Default when stock has moved <5% above current entry.
      - mode='separate': weighted-average entry_price only. The leg carries
        its own SL (custom ₹ or trailing MA). Main stop_loss is unchanged,
        so pyramid risk is tracked per-leg. Default when ext ≥5% or when
        the position is trailing with MAs — the user has typically moved
        the original SL up and doesn't want to re-average it down.

    Each leg stores enough metadata that a later pyramid-aware partial sell
    can reverse its contribution to entry_price (and SL in avg mode) and
    snap the position back to its pre-pyramid state.
    """
    data = request.json or {}
    add_qty = data.get("add_qty")
    add_price = data.get("add_price")
    add_date = data.get("add_date")
    mode = (data.get("mode") or "avg").lower()
    if mode not in ("avg", "separate"):
        mode = "avg"
    custom_sl = data.get("custom_sl")
    trailing_ma = data.get("trailing_ma")  # '5ma'|'10ma'|'20ma' or None

    if not add_qty or not add_price:
        return jsonify({"error": "add_qty and add_price required"}), 400

    try:
        add_qty = int(add_qty)
        add_price = float(add_price)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid add_qty or add_price"}), 400

    if add_qty <= 0 or add_price <= 0:
        return jsonify({"error": "add_qty and add_price must be positive"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM positions WHERE id = %s AND user_id = %s', (pos_id, g.user_id))
        pos = cur.fetchone()
        if not pos:
            return jsonify({"error": "Position not found"}), 404
        if pos.get("status") == "closed":
            return jsonify({"error": "Cannot pyramid into a closed position"}), 400

        # Enforce max 2 pyramids before any writes — check journal p1/p2 slots
        cur.execute('SELECT id FROM journal_trades WHERE position_id = %s AND user_id = %s', (pos_id, g.user_id))
        jtrade = cur.fetchone()
        # Backfill a journal entry if none exists for this position. Pre-journal
        # positions, or positions where the auto-create failed silently, would
        # otherwise lose the pyramid record (and, worse, close too early later
        # when sync_journal_trade_to_position compares total_exited against a
        # journal that's missing the pyramid legs).
        if not jtrade:
            try:
                _auto_create_journal_entry(cur, conn, pos_id, {
                    "stock_name": pos["stock_name"],
                    "entry_price": float(pos["entry_price"]),
                    "stop_loss": pos.get("stop_loss"),
                    "quantity": int(pos["quantity"]),
                    "security_id": pos.get("security_id"),
                    "entry_date": pos.get("entry_date"),
                }, g.user_id)
                cur.execute('SELECT id FROM journal_trades WHERE position_id = %s AND user_id = %s', (pos_id, g.user_id))
                jtrade = cur.fetchone()
            except Exception as je:
                print(f"⚠ Pyramid: journal auto-create failed (non-blocking): {je}")
        # Phase 2a: pyramid storage lives on positions.pyramid_history (JSONB).
        # Max 2 legs enforced from that array rather than journal slots.
        pyramid_history = pos.get("pyramid_history") or []
        if isinstance(pyramid_history, str):
            try: pyramid_history = json.loads(pyramid_history)
            except Exception: pyramid_history = []
        if len(pyramid_history) >= 2:
            return jsonify({"error": "Already at max 2 pyramids (P1, P2). Delete or edit an existing pyramid from Position Manager to adjust."}), 400

        old_qty = int(pos["quantity"])
        old_entry = float(pos["entry_price"])
        old_sl = float(pos["stop_loss"]) if pos.get("stop_loss") else None

        # Compute shares actually held right now (total bought minus already
        # exited via sell_history). The weighted average must use this, not
        # the total bought qty — exited shares were already booked at their
        # own sell price and must not re-enter the average cost of shares
        # still open.
        prior_sell_history = pos.get("pyramid_history") and None  # keep var separate from later sell_history load
        _sh = pos.get("sell_history") or []
        if isinstance(_sh, str):
            try: _sh = json.loads(_sh)
            except Exception: _sh = []
        sold_shares_before = sum(int(s.get("shares") or s.get("qty") or 0) for s in (_sh or []))
        shares_open_before = max(0, old_qty - sold_shares_before)
        shares_open_after = shares_open_before + add_qty

        new_qty = old_qty + add_qty  # quantity = total ever bought; unchanged semantics
        if shares_open_after > 0:
            new_entry = round(((old_entry * shares_open_before) + (add_price * add_qty)) / shares_open_after, 2)
        else:
            new_entry = old_entry
        # position_value reflects cost basis of currently-open shares (what
        # entry_price averages over). Don't multiply by total-bought — that
        # would double-count the already-exited legs.
        new_value = round(new_entry * shares_open_after, 2)

        # Leg SL derivation:
        #   avg mode        → leg SL is the user-provided custom_sl if given,
        #                     else derived from old risk% (leg_price × (1-risk%)).
        #                     Blended with old SL weighted by shares.
        #   separate mode   → leg's SL is custom ₹ (user input) OR null when
        #                     user chose trailing_ma only (ma daemon fills it).
        user_sl = None
        if custom_sl is not None and str(custom_sl).strip() != "":
            try:
                user_sl = float(custom_sl)
                if user_sl <= 0:
                    user_sl = None
            except (TypeError, ValueError):
                user_sl = None

        leg_sl = None
        new_sl = old_sl
        risk_pct = float(pos.get("risk_pct") or 4.0)
        if mode == "avg":
            if user_sl is not None:
                leg_sl = round(user_sl, 2)
            else:
                # Fall back to preserving the original risk% for the new leg
                # when the user didn't specify an SL. Keeps legacy callers
                # working and gives a sensible default on the frontend.
                if old_sl and old_entry > 0:
                    effective_risk = (old_entry - old_sl) / old_entry * 100
                else:
                    effective_risk = risk_pct
                leg_sl = round(add_price * (1 - effective_risk / 100), 2)
            if old_sl is not None and shares_open_after > 0:
                # Same denominator correction as entry: blend over shares
                # actually held (pre + new), not over total-bought.
                new_sl = round(((old_sl * shares_open_before) + (leg_sl * add_qty)) / shares_open_after, 2)
            else:
                new_sl = leg_sl
        else:  # separate mode
            leg_sl = user_sl  # may be None if user chose trailing_ma only
            # Main stop_loss stays at old_sl — untouched.

        # risk_pct floats to match the new entry/SL so one_r_value reflects real risk.
        if new_sl and new_sl < new_entry:
            risk_pct = round((new_entry - float(new_sl)) / new_entry * 100, 2)
        one_r_value = round(new_value * (risk_pct / 100.0), 2) if risk_pct else None

        # Re-base bucket_sold_pct on new total qty (sells that happened before
        # pyramiding are now a smaller %). Reuse the parsed sell_history from
        # the weighted-average pre-compute above.
        sell_history = _sh or []
        sold_shares = sold_shares_before
        new_bucket_pct = round((sold_shares / new_qty) * 100, 2) if new_qty else 0
        new_trail_pct = max(0.0, round(100.0 - new_bucket_pct, 2))

        # Append this leg. Schema:
        #   date, price, shares, slot (p1|p2)
        #   mode ('avg'|'separate')
        #   leg_sl (₹) — concrete SL used for this leg (derived in avg mode,
        #                user-provided in separate mode; may be null in
        #                separate mode when user chose trailing_ma only)
        #   trailing_ma ('5ma'|'10ma'|'20ma'|null) — separate mode only
        #   exited_shares (int) — 0 on insert; bumped when a pyramid-aware
        #                          sell retires this leg
        now = datetime.now()
        next_slot = "p1" if len(pyramid_history) == 0 else "p2"
        iso_date = add_date or now.isoformat()[:10]
        pyramid_history.append({
            "date": iso_date,
            "price": add_price,
            "shares": add_qty,
            "slot": next_slot,
            "mode": mode,
            "leg_sl": leg_sl,
            "trailing_ma": trailing_ma if mode == "separate" else None,
            "exited_shares": 0,
        })

        # Silence the sync_positions_to_journal trigger — we're updating quantity
        # and entry_price authoritatively from the new pyramid leg, and don't
        # want the reverse trigger writing this back into journal's initial_qty
        # (which would lose the history of the original entry).
        # Bump the lifetime-cash-deployed counter. Never reduced on sells —
        # closed-trade reports use this to divide total_pnl by the actual
        # cash the user ever put in, which stays honest even after
        # pyramid-aware reversals reduce entry_price×quantity cost basis.
        prior_cost_outlay = float(pos.get("total_cost_outlay") or (float(pos["entry_price"]) * int(pos["quantity"])))
        new_cost_outlay = round(prior_cost_outlay + (add_price * add_qty), 2)

        cur.execute("SET LOCAL app.syncing_from_journal = 'true'")
        cur.execute('''
            UPDATE positions SET
                quantity = %s, entry_price = %s, stop_loss = %s, position_value = %s,
                risk_pct = %s, one_r_value = %s,
                bucket_sold_pct = %s, trail_remaining_pct = %s,
                pyramid_history = %s,
                total_cost_outlay = %s,
                updated_at = %s
            WHERE id = %s AND user_id = %s
        ''', (
            new_qty, new_entry, new_sl, new_value,
            risk_pct, one_r_value,
            new_bucket_pct, new_trail_pct,
            json.dumps(pyramid_history),
            new_cost_outlay,
            now,
            pos_id, g.user_id,
        ))

        # Journal's avg_entry still displays the weighted-avg buy price, so keep
        # it in sync on pyramid. SL also mirrored when we averaged it.
        if jtrade:
            cur.execute(
                'UPDATE journal_trades SET avg_entry = %s, sl = %s, updated_at = %s WHERE id = %s AND user_id = %s',
                (new_entry, new_sl, now, jtrade["id"], g.user_id),
            )

        _validate_position_invariants(cur, pos_id, g.user_id)
        conn.commit()
        print(f"🔺 Pyramid ({mode}): {pos['stock_name']} +{add_qty} @ ₹{add_price} | qty {old_qty}→{new_qty} | avg ₹{old_entry}→₹{new_entry} | SL ₹{old_sl}→₹{new_sl}")

        return jsonify({
            "added": True,
            "new_quantity": new_qty,
            "new_entry_price": new_entry,
            "new_position_value": new_value,
            "risk_pct": risk_pct,
            "_build_tag": "pyramid-v4-trigger-silenced",
        })

    except InvariantError as ie:
        conn.rollback()
        return jsonify({"error": str(ie)}), 400
    except Exception as e:
        conn.rollback()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# DECISION JOURNAL
# ═══════════════════════════════════════════════════

@position_bp.route("/api/positions/<int:pos_id>/journal", methods=["POST"])
@limiter.limit("30 per minute")
def add_journal_entry(pos_id):
    """Add a 'Not Selling Today' or any decision journal entry"""
    data = request.json
    reason = data.get("reason", "")
    action = data.get("action", "not_selling")

    if action == "not_selling" and not reason.strip():
        return jsonify({"error": "Reason is mandatory for 'Not Selling Today'"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        # Decision-journal entries reference hot cols (current_r_multiple,
        # defensive_status, bucket_sold_pct) — JOIN positions_live for fresh
        # values rather than the stale backfill on positions.
        cur.execute('''
            SELECT p.*, pl.current_price, pl.current_r_multiple, pl.defensive_status,
                   pl.last_5ma_price, pl.last_10ma_price, pl.last_20ma_price
            FROM positions p
            LEFT JOIN positions_live pl ON pl.position_id = p.id
            WHERE p.id = %s AND p.user_id = %s
        ''', (pos_id, g.user_id))
        pos = cur.fetchone()
        if not pos:
            return jsonify({"error": "Position not found"}), 404

        entry = {
            "date": datetime.now().isoformat(),
            "action": action,
            "reason": reason,
            "extension": pos.get("entry_extension_pct"),
            "r_multiple": pos.get("current_r_multiple"),
            "bucket_status": f"{pos.get('bucket_sold_pct', 0)}/{pos.get('bucket_cap', 66)}%",
            "defensive_status": pos.get("defensive_status"),
        }

        journal = pos["journal_entries"] or []
        if isinstance(journal, str):
            journal = json.loads(journal)
        journal.append(entry)

        # Soft cap: keep most recent 50 entries to prevent unbounded JSONB growth
        if len(journal) > 50:
            journal = journal[-50:]

        cur.execute('''
            UPDATE positions SET journal_entries = %s, updated_at = %s WHERE id = %s AND user_id = %s
        ''', (json.dumps(journal), datetime.now(), pos_id, g.user_id))
        conn.commit()

        return jsonify({"recorded": True, "entry": entry})

    except Exception as e:
        conn.rollback()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# CLOSE POSITION (5MA exit or manual)
# ═══════════════════════════════════════════════════

@position_bp.route("/api/positions/<int:pos_id>/close", methods=["POST"])
@limiter.limit("30 per minute")
def close_position(pos_id):
    """Close a position — exit remaining, mark as closed, auto-create journal entry"""
    data = request.json
    exit_price = data.get("exit_price")

    if not exit_price:
        return jsonify({"error": "exit_price required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM positions WHERE id = %s AND user_id = %s', (pos_id, g.user_id))
        pos = cur.fetchone()
        if not pos:
            return jsonify({"error": "Position not found"}), 404

        exit_price = float(exit_price)
        entry = pos["entry_price"]
        qty = pos["quantity"]

        # Sum already-realized P&L from any prior partial exits. Without this
        # we'd overcount: qty is TOTAL bought, so (exit_price - entry) * qty
        # would pretend every share is being sold at exit_price, ignoring the
        # fact that some were already booked at earlier partial-sell prices.
        prior_sells = pos.get("sell_history") or []
        if isinstance(prior_sells, str):
            try: prior_sells = json.loads(prior_sells)
            except Exception: prior_sells = []
        prior_realized = 0.0
        prior_shares_sold = 0
        for s in prior_sells:
            prior_realized += float(s.get("profit") or 0)
            prior_shares_sold += int(s.get("shares") or s.get("qty") or s.get("quantity") or 0)
        remaining_shares = max(0, int(qty) - prior_shares_sold)
        final_leg_pnl = (exit_price - entry) * remaining_shares

        # total_pnl = booked from partials + this final exit block
        total_pnl = prior_realized + final_leg_pnl
        # total_pnl_pct = pct return on cost basis of all shares bought
        cost_basis = entry * qty if qty else 0
        total_pnl_pct = (total_pnl / cost_basis * 100) if cost_basis else 0

        # Append the final-exit block to sell_history. Without this the journal
        # only sees the earlier partial exits and thinks the trade is still
        # open — a later inline edit on e1 would then trigger the reopen
        # branch in sync_journal_trade_to_position, which would silently
        # resurrect the closed position. Skip the append when remaining_shares
        # is 0 (position was already fully exited via partials before the user
        # hit Close).
        now = datetime.now()
        sell_history = list(prior_sells)
        if remaining_shares > 0:
            final_block = {
                "date": now.isoformat(),
                "pct": round((remaining_shares / int(qty)) * 100, 2) if qty else 0,
                "price": exit_price,
                "extension": round(((exit_price - entry) / entry * 100), 2) if entry else 0,
                "r_multiple": 0,
                "trigger": "close_position",
                "shares": remaining_shares,
                "profit": round(final_leg_pnl, 2),
            }
            sell_history.append(final_block)

        bucket_sold_pct = 100.0 if qty else 0.0
        cur.execute('''
            UPDATE positions SET
                status = 'closed', exit_price = %s, exit_date = %s,
                total_pnl = %s, total_pnl_pct = %s,
                sell_history = %s,
                bucket_sold_pct = %s, trail_remaining_pct = 0,
                first_sell_done = true, updated_at = %s
            WHERE id = %s AND user_id = %s
        ''', (
            exit_price, now, round(total_pnl, 2),
            round(total_pnl_pct, 2), json.dumps(sell_history),
            bucket_sold_pct, now, pos_id, g.user_id,
        ))
        # Hot sidecar: pin current_price to exit + null current_r_multiple
        cur.execute('''
            UPDATE positions_live SET
                current_price = %s, current_r_multiple = NULL, updated_at = NOW()
            WHERE position_id = %s
        ''', (exit_price, pos_id))

        _validate_position_invariants(cur, pos_id, g.user_id)
        capital_log.rebuild_for_position(cur, g.user_id, pos_id)
        # Behavioral coaching: same transaction so a phantom prompt can't
        # outlive a rolled-back close.
        try:
            from services.rationale_service import maybe_create_prompt
            maybe_create_prompt(cur, g.user_id, pos_id)
        except Exception as _re:
            print(f"[position] rationale hook (close) skipped: {_re}")
        conn.commit()

        # phase 2b: journal no longer auto-synced from close_position. Journal
        # display JOINs positions.sell_history for linked rows.

        return jsonify({
            "closed": True,
            "exit_price": exit_price,
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "journal_synced": True,
        })

    except InvariantError as ie:
        conn.rollback()
        return jsonify({"error": str(ie)}), 400
    except Exception as e:
        conn.rollback()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# UTILITY
# ═══════════════════════════════════════════════════

@position_bp.route("/api/positions/regimes", methods=["GET"])
@limiter.limit("60 per minute")
def get_regimes():
    """Return all regime presets"""
    from database.init_positions_db import REGIME_PARAMS
    return jsonify(REGIME_PARAMS)


# ═══════════════════════════════════════════════════
# MARKET DATA INTEGRATION
# ═══════════════════════════════════════════════════

@position_bp.route("/api/market/health", methods=["GET"])
@limiter.limit("60 per minute")
def market_health():
    """Health check — shows data source freshness."""
    try:
        from services.market_data_service import get_market_health
        return jsonify(get_market_health())
    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@position_bp.route("/api/positions/live-monitor", methods=["GET"])
@limiter.limit("60 per minute")
def live_monitor():
    """ALL active positions refreshed in ONE query — for real-time monitoring."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, stock_name, security_id, entry_price, risk_pct, quantity, stop_loss, trailing_sl, trailing_mode, bucket_sold_pct, market_regime, ma_followed, ma_grade FROM positions WHERE status = 'active' AND user_id = %s", (g.user_id,))
        positions = [dict(r) for r in cur.fetchall()]

        if not positions:
            return jsonify({"positions": [], "source": "supabase_websocket"})

        from services.market_data_service import bulk_refresh_positions
        live_data = bulk_refresh_positions(positions)

        result = []
        for p in positions:
            sid = str(p.get("security_id", ""))
            md = live_data.get(sid, {})
            entry = float(p.get("entry_price") or 0)
            close = md.get("close", 0)
            risk_pct = float(p.get("risk_pct") or 4)
            risk_per_share = entry * (risk_pct / 100) if entry > 0 else 0
            ext = round(((close - entry) / entry * 100), 2) if entry > 0 else 0
            r_mult = round(((close - entry) / risk_per_share), 2) if risk_per_share > 0 else 0

            result.append({
                "id": p["id"],
                "stock_name": p["stock_name"],
                "security_id": sid,
                "entry_price": entry,
                "current_price": close,
                "quantity": p.get("quantity"),
                "stop_loss": p.get("stop_loss"),
                "extension_pct": ext,
                "r_multiple": r_mult,
                "ma5": md.get("ma5"),
                "ma10": md.get("ma10"),
                "ma20": md.get("ma20"),
                "high": md.get("high"),
                "low": md.get("low"),
                "volume": md.get("volume"),
                "bucket_sold_pct": p.get("bucket_sold_pct"),
                "regime": p.get("market_regime"),
                "ma_followed": p.get("ma_followed"),
                "ma_grade": p.get("ma_grade"),
                "unrealised_pnl": round((close - entry) * (p.get("quantity") or 0), 0),
            })

        return jsonify({"positions": result, "source": "supabase_websocket", "count": len(result)})
    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# INTRADAY P&L SNAPSHOTS
# Writer is hit by Cloud Scheduler every ~2 min during market hours.
# Reader returns today's stored series for the intraday P&L modal.
# ═══════════════════════════════════════════════════

def _ist_now_and_date():
    """Return (now_ist, today_ist_date). Used by both snapshot paths so
    the date stored in `date_ist` matches the reader's CURRENT_DATE filter."""
    from datetime import timezone as _tz
    IST = _tz(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    return now_ist, now_ist.date()


def _is_market_hours_ist(now_ist):
    """Skip snapshot writes outside 09:15–15:30 IST on trading weekdays so
    the series doesn't get polluted with pre-/post-market flat rows."""
    if now_ist.weekday() >= 5:
        return False
    minutes = now_ist.hour * 60 + now_ist.minute
    return 555 <= minutes <= 930


def _compute_day_pnl_for_user(cur, user_id):
    """Replicates the frontend day-P&L formula on the server side:
        day_pnl = Σ (ltp - basis) × shares_remaining
        portfolio_value = Σ ltp × shares_remaining
        day_pnl_pct = day_pnl / invested × 100
    where invested = Σ entry_price × shares_remaining.

    For positions opened today (IST), basis = entry_price — the user only
    "owns" the move from where they bought, not any pre-entry gap. From day
    two onwards, basis = previous session close.

    Returns (portfolio_value, day_pnl, day_pnl_pct) — all floats. Returns
    (None, None, None) if there's nothing to snapshot for this user (no
    active positions, or no price data yet)."""
    cur.execute(
        """
        SELECT id, security_id, entry_price, quantity, sell_history, entry_date
        FROM positions
        WHERE user_id = %s AND status = 'active' AND security_id IS NOT NULL
        """,
        (user_id,),
    )
    positions = [dict(r) for r in cur.fetchall()]
    if not positions:
        return None, None, None

    from services.market_data_service import get_ltp
    sec_ids = [{"security_id": p["security_id"], "exchange": "NSE_EQ"} for p in positions]
    ltp_map = get_ltp(sec_ids)

    # Previous-close = last candles_daily.close with date STRICTLY before
    # today IST. candles_daily is updated intraday so today's row would
    # give us the current price, not the prev session close.
    _, today_ist = _ist_now_and_date()
    sec_id_list = [str(p["security_id"]) for p in positions]
    cur.execute(
        """
        SELECT DISTINCT ON (security_id) security_id, close
        FROM candles_daily
        WHERE security_id = ANY(%s::text[]) AND date < %s AND volume > 0
        ORDER BY security_id, date DESC
        """,
        (sec_id_list, today_ist),
    )
    prev_close_map = {str(r["security_id"]): float(r["close"]) for r in cur.fetchall()}

    portfolio_value = 0.0
    day_pnl = 0.0
    invested = 0.0
    matched = 0
    for pos in positions:
        sid = str(pos["security_id"])
        ltp = ltp_map.get(sid)
        prev_close = prev_close_map.get(sid)
        entry = float(pos.get("entry_price") or 0)

        # Entry-day positions get entry_price as basis even when prev_close
        # is missing (a brand-new IPO listed today wouldn't have a prior
        # candle). Otherwise we need prev_close to compute the day move.
        ed = pos.get("entry_date")
        ed_iso = ed.isoformat()[:10] if hasattr(ed, "isoformat") else (str(ed)[:10] if ed else "")
        is_entry_today = bool(ed_iso) and ed_iso == today_ist
        basis = entry if (is_entry_today and entry > 0) else prev_close
        if not ltp or not basis:
            continue

        total_qty = int(pos.get("quantity") or 0)
        sh = pos.get("sell_history") or []
        if isinstance(sh, str):
            try:
                sh = json.loads(sh)
            except Exception:
                sh = []
        shares_sold = 0
        for s in sh:
            if isinstance(s, dict):
                shares_sold += int(s.get("shares") or s.get("qty") or 0)
        shares_remaining = max(0, total_qty - shares_sold)
        if shares_remaining <= 0:
            continue

        day_pnl += (float(ltp) - basis) * shares_remaining
        portfolio_value += float(ltp) * shares_remaining
        invested += entry * shares_remaining
        matched += 1

    if matched == 0:
        return None, None, None

    day_pnl_pct = (day_pnl / invested * 100) if invested > 0 else 0.0
    return round(portfolio_value, 2), round(day_pnl, 2), round(day_pnl_pct, 4)


@position_bp.route("/api/internal/portfolio-snapshot", methods=["POST"])
def write_portfolio_snapshot():
    """Cron-authenticated snapshot writer. Cloud Scheduler hits this every
    ~2 min during market hours; each call writes one row per user with
    active positions into portfolio_snapshots.

    Auth: X-Cron-Secret header must match CRON_SECRET env var (same pattern
    as other internal cron endpoints in this repo)."""
    cron_secret = os.getenv("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret", "")
    if not cron_secret or provided != cron_secret:
        return jsonify({"error": "unauthorized"}), 401

    now_ist, today_ist = _ist_now_and_date()
    if not _is_market_hours_ist(now_ist):
        return jsonify({"skipped": "outside_market_hours", "now_ist": now_ist.isoformat()})

    conn = get_db()
    try:
        cur = conn.cursor()
        # Purge anything older than today. Snapshots are a same-session curve,
        # not a history — we don't keep yesterday's data around. First cron
        # tick after 09:15 IST clears the prior day.
        cur.execute(
            "DELETE FROM portfolio_snapshots WHERE date_ist < %s",
            (today_ist,),
        )
        purged = cur.rowcount

        cur.execute("SELECT DISTINCT user_id FROM positions WHERE status = 'active'")
        user_ids = [r["user_id"] for r in cur.fetchall()]

        written = 0
        skipped = 0
        for uid in user_ids:
            pv, pnl, pnl_pct = _compute_day_pnl_for_user(cur, uid)
            if pv is None:
                skipped += 1
                continue
            cur.execute(
                """
                INSERT INTO portfolio_snapshots
                    (user_id, ts, date_ist, portfolio_value, day_pnl, day_pnl_pct)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, ts) DO NOTHING
                """,
                (uid, now_ist, today_ist, pv, pnl, pnl_pct),
            )
            written += 1
        conn.commit()
        return jsonify({
            "written": written,
            "skipped": skipped,
            "purged": purged,
            "users": len(user_ids),
            "ts": now_ist.isoformat(),
        })
    except Exception as e:
        conn.rollback()
        print(f"[position] snapshot error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/internal/earnings-reminders", methods=["POST"])
def write_earnings_reminders():
    """Daily cron: drop a notification on every active position whose next
    board meeting is exactly 7 days or 1 day out. Two windows so the user
    sees one heads-up a week ahead (time to size down / exit) and a final
    nudge the day before. Dedupe is by (user_id, type) so re-running the
    cron the same day is a no-op.

    Auth: X-Cron-Secret header must match CRON_SECRET env var.

    Schedule (recommended): once daily, ~07:30 IST, before market open.
    """
    cron_secret = os.getenv("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret", "")
    if not cron_secret or provided != cron_secret:
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db()
    try:
        cur = conn.cursor()
        # Find every (user, position) pair where the position is active and
        # the linked stock has a results meeting at exactly +7 or +1 days.
        # Same purpose filter as the rest of the app.
        cur.execute("""
            SELECT
              p.user_id,
              p.id AS position_id,
              p.stock_name,
              p.entry_price,
              fr.meeting_date,
              fr.purpose,
              (fr.meeting_date - CURRENT_DATE)::int AS days_left
            FROM positions p
            JOIN forthcoming_results fr ON fr.security_id = p.security_id
            WHERE p.status = 'active'
              AND fr.meeting_date >= CURRENT_DATE
              AND (fr.meeting_date - CURRENT_DATE) IN (1, 7)
              AND (fr.purpose ILIKE %s OR fr.raw_purpose ILIKE %s
                   OR fr.raw_purpose ILIKE %s OR fr.raw_purpose ILIKE %s)
        """, ("%result%", "%financial result%", "%audited%", "%quarterly result%"))

        rows = cur.fetchall()
        created = 0
        skipped_dupes = 0
        for r in rows:
            days_left = r["days_left"]
            window = "T-1" if days_left == 1 else "T-7"
            ntype = f"earnings_reminder_{window.lower()}_{r['position_id']}"
            # Dedupe: skip if we've already created the same-window reminder
            # for the same position today. (id is per-day because the
            # position_id changes when re-opened; the cron runs once per day.)
            cur.execute(
                "SELECT 1 FROM notifications WHERE user_id = %s AND type = %s "
                "AND created_at::date = CURRENT_DATE LIMIT 1",
                (r["user_id"], ntype),
            )
            if cur.fetchone():
                skipped_dupes += 1
                continue
            md = r["meeting_date"]
            md_str = md.isoformat() if md and hasattr(md, "isoformat") else md
            when_label = "tomorrow" if days_left == 1 else "in 7 days"
            title = f"📅 {r['stock_name']} reports {when_label}"
            body = (
                f"Results scheduled {md_str}. "
                f"Entry ₹{round(float(r['entry_price'] or 0), 2)}. "
                f"{r.get('purpose') or 'Quarterly Results'}."
            )
            cur.execute(
                "INSERT INTO notifications (user_id, type, title, body) "
                "VALUES (%s, %s, %s, %s)",
                (r["user_id"], ntype, title, body),
            )
            created += 1
        conn.commit()
        return jsonify({
            "matched": len(rows),
            "created": created,
            "skipped_duplicates": skipped_dupes,
        })
    except Exception as e:
        conn.rollback()
        print(f"[earnings-reminders] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/internal/post-result-refresh", methods=["POST"])
def post_result_refresh():
    """Daily cron: detect freshly-arrived quarterly results for any active
    position and notify the user with the headline numbers (revenue, EPS,
    net profit) plus YoY/QoQ deltas if comparable rows exist.

    Also reaps the matching forthcoming_results row so the "Next Results"
    card flips off the moment the actual filing lands.

    Tracking: app_config.key='post_result_last_check' stores the last cron
    run timestamp; we look for financials_quarterly.created_at > that. First
    run (no row) defaults to 24 hours ago to avoid notifying on every old
    filing in the table.

    Auth: X-Cron-Secret header must match CRON_SECRET env var.
    """
    cron_secret = os.getenv("CRON_SECRET", "")
    provided = request.headers.get("X-Cron-Secret", "")
    if not cron_secret or provided != cron_secret:
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db()
    try:
        cur = conn.cursor()
        # Last-check timestamp from app_config — defaults to "1 day ago" on
        # first run so we don't backfill notifications for historical filings.
        cur.execute("SELECT value FROM app_config WHERE key = 'post_result_last_check'")
        row = cur.fetchone()
        if row and row.get("value"):
            try:
                from datetime import datetime as _dt
                raw = row["value"] if isinstance(row["value"], str) else row["value"].get("ts")
                last_check = _dt.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                last_check = None
        else:
            last_check = None
        if not last_check:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            last_check = _dt.now(_tz.utc) - _td(days=1)

        # Pull every freshly-arrived quarterly row for any active position.
        # We only care about rows that landed since the last cron tick.
        cur.execute("""
            SELECT
              p.user_id,
              p.id AS position_id,
              p.stock_name,
              p.security_id,
              fq.period,
              fq.period_end_date,
              fq.revenue_cr,
              fq.net_profit_cr,
              fq.eps,
              fq.opm_percent,
              fq.is_consolidated,
              fq.created_at
            FROM positions p
            JOIN financials_quarterly fq ON fq.security_id = p.security_id
            WHERE p.status = 'active'
              AND fq.created_at > %s
              AND fq.revenue_cr IS NOT NULL
            ORDER BY p.id, fq.is_consolidated DESC, fq.period_end_date DESC
        """, (last_check,))
        fresh = [dict(r) for r in cur.fetchall()]

        # Dedupe: keep one row per position (latest period, prefer consolidated).
        per_position = {}
        for r in fresh:
            if r["position_id"] not in per_position:
                per_position[r["position_id"]] = r

        created = 0
        skipped = 0
        for r in per_position.values():
            ntype = f"post_result_{r['position_id']}_{r.get('period') or r.get('period_end_date')}"
            cur.execute(
                "SELECT 1 FROM notifications WHERE user_id = %s AND type = %s LIMIT 1",
                (r["user_id"], ntype),
            )
            if cur.fetchone():
                skipped += 1
                continue

            # Look up the previous quarter for the same security to compute
            # QoQ and YoY deltas — gives the notification a "did they beat?"
            # punchline rather than just raw numbers. Best-effort; skip if
            # comparable data isn't there.
            qoq_pct = None
            yoy_pct = None
            try:
                cur.execute("""
                    SELECT revenue_cr, net_profit_cr, period_end_date
                    FROM financials_quarterly
                    WHERE security_id = %s AND is_consolidated = %s
                      AND period_end_date < %s
                      AND revenue_cr IS NOT NULL
                    ORDER BY period_end_date DESC
                    LIMIT 4
                """, (r["security_id"], r.get("is_consolidated"), r.get("period_end_date")))
                history = cur.fetchall()
                if history:
                    prev_q = history[0]
                    if prev_q.get("revenue_cr") and r.get("revenue_cr"):
                        qoq_pct = round(
                            (float(r["revenue_cr"]) - float(prev_q["revenue_cr"]))
                            / float(prev_q["revenue_cr"]) * 100, 1)
                if len(history) >= 4:
                    prev_y = history[3]  # 4 quarters back
                    if prev_y.get("revenue_cr") and r.get("revenue_cr"):
                        yoy_pct = round(
                            (float(r["revenue_cr"]) - float(prev_y["revenue_cr"]))
                            / float(prev_y["revenue_cr"]) * 100, 1)
            except Exception:
                pass

            rev = round(float(r["revenue_cr"]), 0) if r.get("revenue_cr") else None
            np = round(float(r["net_profit_cr"]), 0) if r.get("net_profit_cr") else None
            eps = round(float(r["eps"]), 2) if r.get("eps") else None

            parts = []
            if rev is not None:
                rev_part = f"Rev ₹{int(rev)}Cr"
                if yoy_pct is not None:
                    rev_part += f" ({yoy_pct:+.1f}% YoY)"
                elif qoq_pct is not None:
                    rev_part += f" ({qoq_pct:+.1f}% QoQ)"
                parts.append(rev_part)
            if np is not None:
                parts.append(f"PAT ₹{int(np)}Cr")
            if eps is not None:
                parts.append(f"EPS ₹{eps}")

            title = f"📈 {r['stock_name']} — Q{r.get('period') or ''} results"
            body = " · ".join(parts) if parts else "Results filed. Open the position for details."
            cur.execute(
                "INSERT INTO notifications (user_id, type, title, body) "
                "VALUES (%s, %s, %s, %s)",
                (r["user_id"], ntype, title, body),
            )
            created += 1

            # Reap any matching forthcoming_results row so the "Next Results"
            # card flips off immediately. Match on security_id where the
            # meeting was on or before the period_end_date of the new filing.
            try:
                cur.execute("""
                    DELETE FROM forthcoming_results
                    WHERE security_id = %s
                      AND meeting_date <= %s
                """, (r["security_id"], r.get("period_end_date")))
            except Exception:
                pass

        # Bump the last-check watermark.
        from datetime import datetime as _dt, timezone as _tz
        now_iso = _dt.now(_tz.utc).isoformat()
        cur.execute(
            "INSERT INTO app_config (key, value, updated_at) VALUES (%s, %s::jsonb, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            ("post_result_last_check", json.dumps(now_iso)),
        )
        conn.commit()
        return jsonify({
            "fresh_rows": len(fresh),
            "positions_with_results": len(per_position),
            "notifications_created": created,
            "skipped_duplicates": skipped,
            "last_check": last_check.isoformat() if hasattr(last_check, "isoformat") else str(last_check),
            "next_check_after": now_iso,
        })
    except Exception as e:
        conn.rollback()
        print(f"[post-result-refresh] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/upcoming-events", methods=["GET"])
@position_bp.route("/api/earnings-calendar", methods=["GET"])  # legacy alias
@limiter.limit("30 per minute")
def upcoming_events():
    """Unified upcoming-events feed — EVERY company (not just user's book)
    that has a board meeting on file (forthcoming_results) or a confirmed
    corporate action (corporate_actions: dividend / split / bonus / rights
    / buyback). Each row is tagged with whether the user currently holds
    it (position_id) or watches it (in_watchlist) so the UI can narrow
    down by those dimensions without a second request.

    Each row carries:
      event_type    'RESULTS' | 'BOARD_MEETING' | 'DIVIDEND' | 'SPLIT' |
                    'BONUS' | 'RIGHTS' | 'BUYBACK' | 'OTHER'
      stock_name, symbol, security_id
      event_date    meeting_date for board meetings, ex_date (or record_date
                    / payment_date) for corp actions
      days_left     (event_date - today)
      details       short human-readable label
      raw           original purpose / details text for tooltips
      position_id   set when the user has an active position; null otherwise
      in_watchlist  true when the user watches this stock
      entry_price, current_price  (positions only)

    Query:
      ?days=30        (default 30, max 180)         days *forward* of today
      ?past_days=0    (default 0,  max 365)         days *backward* of today
      ?scope=all      default — every company with events
      ?scope=mine     restrict to stocks the user holds or watches
    """
    try:
        days_ahead = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days_ahead = 30
    days_ahead = max(1, min(days_ahead, 180))

    try:
        past_days = int(request.args.get("past_days", 0))
    except (TypeError, ValueError):
        past_days = 0
    past_days = max(0, min(past_days, 365))

    scope = (request.args.get("scope") or "all").lower()

    # Pretty-print a corporate-action row into a single short label.
    def _ca_details(action_type, dividend_amount, dividend_type, split_ratio,
                    bonus_ratio, raw_details):
        if action_type == "DIVIDEND":
            amt = f"₹{float(dividend_amount):g}" if dividend_amount is not None else ""
            kind = (dividend_type or "").title()
            label = " ".join(filter(None, [kind, "Dividend", amt])).strip()
            return label or "Dividend"
        if action_type == "SPLIT":
            return f"Split {split_ratio}" if split_ratio else "Stock Split"
        if action_type == "BONUS":
            return f"Bonus {bonus_ratio}" if bonus_ratio else "Bonus Issue"
        if action_type == "RIGHTS":
            return "Rights Issue"
        if action_type == "BUYBACK":
            return "Buyback"
        if action_type == "DEMERGER":
            return "Demerger"
        return (raw_details or action_type or "Corporate Action")[:80]

    conn = get_db()
    try:
        cur = conn.cursor()

        # First, which securities does the user hold / watch? We look these
        # up once so we can annotate every event row without a second query.
        cur.execute(
            "SELECT id, security_id, stock_name, entry_price, current_price "
            "FROM positions WHERE user_id = %s AND status = 'active' "
            "AND security_id IS NOT NULL",
            (g.user_id,),
        )
        positions_by_sid = {}
        for r in cur.fetchall():
            sid = str(r["security_id"])
            positions_by_sid[sid] = dict(r)

        cur.execute(
            "SELECT DISTINCT security_id FROM watchlist_items "
            "WHERE user_id = %s AND security_id IS NOT NULL",
            (g.user_id,),
        )
        watchlist_sids = {str(r["security_id"]) for r in cur.fetchall()}

        # Market-cap classifier: Large = NIFTY or NIFTY NEXT 50,
        # Mid = NIFTY MID100 FREE, Small = NIFTY SMALLCAP 100 /
        # MIDSMALLCAP 400 / NIFTY 500. Stocks not in any of these fall
        # through to null (UI shows them under "no filter").
        cur.execute("""
            SELECT stock_symbol, index_symbol
            FROM index_constituents
            WHERE index_symbol IN (
              'NIFTY', 'NIFTY NEXT 50', 'NIFTY MID100 FREE',
              'NIFTY SMALLCAP 100', 'NIFTY MIDSMALLCAP 400', 'NIFTY 500'
            )
        """)
        mcap_by_symbol = {}
        for r in cur.fetchall():
            sym = r["stock_symbol"]
            idx = r["index_symbol"]
            tier = "large" if idx in ("NIFTY", "NIFTY NEXT 50") \
                else "mid" if idx == "NIFTY MID100 FREE" \
                else "small"
            existing = mcap_by_symbol.get(sym)
            # Promote to larger tier if we see membership in a bigger index.
            rank = {"large": 3, "mid": 2, "small": 1}
            if existing is None or rank[tier] > rank[existing]:
                mcap_by_symbol[sym] = tier

        # ── 1. BOARD MEETINGS (forthcoming_results) ──
        # Collapse duplicate same-day filings via DISTINCT ON, preferring the
        # row that explicitly mentions "Financial Results" when multiple
        # filings land on the same date for the same scrip. INNER JOIN
        # stock_universe on security_id OR symbol so we keep filings whose
        # BSE→universe security_id mapping hasn't landed (forthcoming_results.
        # security_id is nullable) but still drop true orphans like trustwave
        # that never appear in our universe under any key. Also require
        # is_active so the row's click-through to /explore/<symbol> (which
        # filters by is_active = true) actually resolves.
        cur.execute("""
            SELECT DISTINCT ON (fr.security_id, fr.meeting_date)
              fr.security_id,
              fr.symbol,
              COALESCE(fr.long_name, su.company_name, fr.symbol) AS stock_name,
              fr.meeting_date AS event_date,
              CASE
                WHEN fr.purpose ILIKE %s OR fr.raw_purpose ILIKE %s
                  OR fr.raw_purpose ILIKE %s OR fr.raw_purpose ILIKE %s
                THEN 'RESULTS'
                ELSE 'BOARD_MEETING'
              END AS event_type,
              COALESCE(fr.purpose, 'Board Meeting') AS details,
              fr.raw_purpose AS raw,
              (fr.meeting_date - CURRENT_DATE)::int AS days_left
            FROM forthcoming_results fr
            INNER JOIN stock_universe su
              ON (fr.security_id IS NOT NULL AND su.security_id = fr.security_id)
              OR (fr.security_id IS NULL AND fr.symbol IS NOT NULL
                  AND UPPER(su.symbol) = UPPER(fr.symbol))
            WHERE fr.meeting_date >= CURRENT_DATE - %s
              AND fr.meeting_date <= CURRENT_DATE + %s
              AND su.is_active = true
            ORDER BY fr.security_id, fr.meeting_date,
                     (fr.purpose ILIKE %s) DESC,
                     (fr.purpose ILIKE %s) DESC
        """, ("%result%", "%financial result%", "%audited%", "%quarterly result%",
              past_days, days_ahead,
              "%financial result%", "%result%"))
        bm_rows = [dict(r) for r in cur.fetchall()]

        # ── 2. CORPORATE ACTIONS (dividends, splits, bonus, rights, buyback,
        # demerger). Use ex_date primarily, fall back to record_date or
        # payment_date. INNER JOIN stock_universe on security_id OR symbol
        # so renamed/re-keyed rows still resolve. Require is_active so the
        # click-through to /explore/<symbol> works (otherwise the user
        # lands on a "no data found" page).
        cur.execute("""
            SELECT
              ca.security_id,
              ca.symbol,
              COALESCE(su.company_name, ca.symbol) AS stock_name,
              ca.action_type,
              ca.dividend_amount, ca.dividend_type,
              ca.split_ratio, ca.bonus_ratio,
              ca.details AS raw,
              COALESCE(ca.ex_date, ca.record_date, ca.payment_date) AS event_date,
              (COALESCE(ca.ex_date, ca.record_date, ca.payment_date) - CURRENT_DATE)::int AS days_left
            FROM corporate_actions ca
            INNER JOIN stock_universe su
              ON su.security_id = ca.security_id
              OR (ca.symbol IS NOT NULL AND UPPER(su.symbol) = UPPER(ca.symbol))
            WHERE COALESCE(ca.ex_date, ca.record_date, ca.payment_date) >= CURRENT_DATE - %s
              AND COALESCE(ca.ex_date, ca.record_date, ca.payment_date) <= CURRENT_DATE + %s
              AND su.is_active = true
        """, (past_days, days_ahead))
        ca_rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["event_type"] = (d.get("action_type") or "OTHER").upper()
            d["details"] = _ca_details(d.get("action_type"), d.get("dividend_amount"),
                                       d.get("dividend_type"), d.get("split_ratio"),
                                       d.get("bonus_ratio"), d.get("raw"))
            d["stock_name"] = d.get("stock_name") or d.get("symbol")
            for k in ("action_type", "dividend_amount", "dividend_type",
                      "split_ratio", "bonus_ratio"):
                d.pop(k, None)
            ca_rows.append(d)

        # ── 3. Merge, annotate with portfolio flags, serialize ──
        def _annotate(row):
            sid = str(row.get("security_id") or "")
            pos = positions_by_sid.get(sid) if sid else None
            if pos:
                row["position_id"] = pos.get("id")
                # Positions-side stock names are user-entered and more
                # reliable than the generic stock_universe name when available.
                row["stock_name"] = pos.get("stock_name") or row.get("stock_name")
                row["entry_price"] = float(pos["entry_price"]) if pos.get("entry_price") is not None else None
                row["current_price"] = float(pos["current_price"]) if pos.get("current_price") is not None else None
            else:
                row["position_id"] = None
                row["entry_price"] = None
                row["current_price"] = None
            row["in_watchlist"] = sid in watchlist_sids if sid else False
            row["market_cap_tier"] = mcap_by_symbol.get(row.get("symbol")) if row.get("symbol") else None
            # ISO-serialize dates
            ev = row.get("event_date")
            if ev is not None and hasattr(ev, "isoformat"):
                row["event_date"] = ev.isoformat()
            return row

        merged = [_annotate(r) for r in (bm_rows + ca_rows)]

        # Apply scope=mine if requested — keep only events linked to a
        # position or watchlist entry. Default is the full universe.
        if scope == "mine":
            merged = [r for r in merged if r["position_id"] or r["in_watchlist"]]

        merged.sort(key=lambda x: (x.get("event_date") or "",
                                   x.get("event_type") or "",
                                   x.get("symbol") or ""))

        # Backwards-compat: the original earnings-calendar UI reads
        # meeting_date / purpose. Mirror them for any caller that hasn't moved.
        for r in merged:
            r["meeting_date"] = r.get("event_date")
            r["purpose"] = r.get("details")
            # Legacy source tag for clients that predate the flags.
            if r["position_id"]:
                r["source"] = "position"
                r["ref_id"] = r["position_id"]
            elif r["in_watchlist"]:
                r["source"] = "watchlist"
            else:
                r["source"] = "universe"

        return jsonify({
            "events": merged,
            "count": len(merged),
            "days_ahead": days_ahead,
            "past_days": past_days,
            "scope": scope,
        })
    except Exception as e:
        print(f"[upcoming-events] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/positions/intraday-pnl", methods=["GET"])
@limiter.limit("60 per minute")
def get_intraday_pnl():
    """Returns today's stored P&L snapshots for the authenticated user so
    the Position Manager can render the intraday P&L curve. Trivial read
    path — one indexed SELECT, no live compute. The frontend already has
    the live day_pnl value from refresh-all and appends it to the curve
    itself; doing the compute here too would duplicate work."""
    _, today_ist = _ist_now_and_date()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ts, portfolio_value, day_pnl, day_pnl_pct
            FROM portfolio_snapshots
            WHERE user_id = %s AND date_ist = %s
            ORDER BY ts ASC
            """,
            (g.user_id, today_ist),
        )
        rows = cur.fetchall()
        series = [
            {
                "ts": r["ts"].isoformat(),
                "portfolio_value": float(r["portfolio_value"]),
                "day_pnl": float(r["day_pnl"]),
                "day_pnl_pct": float(r["day_pnl_pct"]),
            }
            for r in rows
        ]
        return jsonify({
            "date": today_ist.isoformat(),
            "count": len(series),
            "series": series,
        })
    except Exception as e:
        print(f"[position] intraday-pnl error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/market/search", methods=["GET"])
@limiter.limit("60 per minute")
def market_search():
    """Search the universe by name/symbol.

    Query params:
      q     — required, min 2 chars
      kinds — optional comma-separated: stock,etf,index. Defaults to
              "stock,etf" so legacy callers don't see indices in their
              stock-only dropdowns. The Explore page passes all three.
    """
    query = request.args.get("q", "")
    if len(query) < 2:
        return jsonify({"results": []})

    kinds_param = request.args.get("kinds")
    kinds = None
    if kinds_param:
        allowed = {"stock", "etf", "index"}
        kinds = [k.strip() for k in kinds_param.split(",") if k.strip() in allowed]
        if not kinds:
            kinds = None  # bad input → fall back to default

    try:
        from services.market_data_service import search_stocks
        results = search_stocks(query, kinds=kinds)
        return jsonify({"results": results})
    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error", "results": []}), 500


@position_bp.route("/api/market/ltp", methods=["POST"])
@limiter.limit("30 per minute")
def market_ltp():
    """Fetch live LTP for given security_ids"""
    data = request.json
    sec_ids = data.get("security_ids", [])
    if not sec_ids:
        return jsonify({"prices": {}})

    try:
        from services.market_data_service import get_ltp
        prices = get_ltp(sec_ids)
        return jsonify({"prices": prices})
    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error", "prices": {}}), 500


@position_bp.route("/api/market/stock-data", methods=["POST"])
@limiter.limit("30 per minute")
def market_stock_data():
    """
    FAST endpoint — market data only. No Yahoo Finance.
    Returns: CMP, 5/10/20 MA, ADR%, Liquidity
    Yahoo (market cap, sector) fetched separately via /api/market/yahoo-data
    """
    data = request.json
    security_id = data.get("security_id")
    symbol = data.get("symbol")
    if not security_id:
        return jsonify({"error": "security_id required"}), 400

    result = {
        "security_id": security_id,
        "cmp": None, "five_ma": None, "ten_ma": None, "twenty_ma": None,
        "adr_pct": None, "liquidity_cr": None,
        "market_cap_cr": None, "sector": None, "industry": None,
        "candle_count": 0,
        "high_52w": None, "low_52w": None, "fall_from_52w_pct": None, "recovery_to_52w_pct": None,
    }

    # 1. LTP
    try:
        from services.market_data_service import get_ltp
        ltp_data = get_ltp([{"security_id": security_id, "exchange": "NSE_EQ"}])
        result["cmp"] = ltp_data.get(str(security_id))
    except Exception as e:
        print(f"⚠️ stock-data LTP: {e}")

    # 2. MAs + ADR + Liquidity from historical
    try:
        from services.market_data_service import get_historical_daily
        candles = get_historical_daily(security_id, days=30, verbose=False)
        if candles and len(candles) >= 5:
            closes = [c["close"] for c in candles]
            highs = [c["high"] for c in candles]
            lows = [c["low"] for c in candles]
            volumes = [c.get("volume", 0) for c in candles]
            result["candle_count"] = len(closes)
            if not result["cmp"]:
                result["cmp"] = closes[-1]
            if len(closes) >= 5:
                result["five_ma"] = round(sum(closes[-5:]) / 5, 2)
            if len(closes) >= 10:
                result["ten_ma"] = round(sum(closes[-10:]) / 10, 2)
            if len(closes) >= 20:
                result["twenty_ma"] = round(sum(closes[-20:]) / 20, 2)
            n = min(20, len(candles))
            avg_high = sum(highs[-n:]) / n
            avg_low = sum(lows[-n:]) / n
            if closes[-1] > 0:
                result["adr_pct"] = round(((avg_high - avg_low) / closes[-1]) * 100, 2)
            avg_vol = sum(volumes[-n:]) / n
            avg_close = sum(closes[-n:]) / n
            result["liquidity_cr"] = round((avg_vol * avg_close) / 10000000, 2)
    except Exception as e:
        print(f"⚠️ stock-data historical: {e}")

    # 3. 52-Week High from Supabase candles (252 trading days)
    try:
        from database.database import get_db
        conn52 = get_db()
        if conn52:
            cur52 = conn52.cursor()
            cur52.execute("""
                SELECT MAX(high) as high_52w, MIN(low) as low_52w
                FROM candles_daily
                WHERE security_id = %s AND date > NOW() - INTERVAL '365 days'
            """, (str(security_id),))
            row52 = cur52.fetchone()
            if row52 and row52.get("high_52w"):
                h52 = float(row52["high_52w"])
                l52 = float(row52["low_52w"])
                cmp = result.get("cmp") or 0
                result["high_52w"] = round(h52, 2)
                result["low_52w"] = round(l52, 2)
                if h52 > 0 and cmp > 0:
                    result["fall_from_52w_pct"] = round(((cmp - h52) / h52) * 100, 2)
                    result["recovery_to_52w_pct"] = round(((h52 - cmp) / cmp) * 100, 2) if cmp < h52 else 0
            conn52.close()
    except Exception as e:
        print(f"⚠️ stock-data 52W high: {e}")

    # 4. Market Cap + Sector from Yahoo (cached 24hrs, background fetch if not cached)
    if symbol:
        try:
            from services.market_data_service import get_yahoo_cached, prefetch_yahoo_background
            yahoo = get_yahoo_cached(symbol)
            if yahoo:
                result["market_cap_cr"] = yahoo.get("market_cap_cr")
                result["sector"] = yahoo.get("sector")
                result["industry"] = yahoo.get("industry")
            else:
                # Not cached — trigger background fetch, return without blocking
                prefetch_yahoo_background(symbol)
        except Exception as e:
            print(f"⚠️ stock-data Yahoo: {e}")

    # 5. Next upcoming result / board-meeting date (for event-risk pre-entry
    # warning). Same filter as /api/positions — only results-related meetings,
    # prefer "Financial Results" when multiple rows land on the same day.
    result["next_result_date"] = None
    result["next_result_purpose"] = None
    result["next_result_days_left"] = None
    result["next_result_trading_days_left"] = None
    try:
        from database.database import get_db
        conn_fr = get_db()
        if conn_fr:
            cur_fr = conn_fr.cursor()
            cur_fr.execute("""
                SELECT meeting_date, purpose,
                       (meeting_date - CURRENT_DATE) AS days_left
                FROM forthcoming_results
                WHERE security_id = %s
                  AND meeting_date >= CURRENT_DATE
                  AND (purpose ILIKE '%%result%%'
                       OR raw_purpose ILIKE '%%financial result%%'
                       OR raw_purpose ILIKE '%%audited%%'
                       OR raw_purpose ILIKE '%%quarterly result%%')
                ORDER BY meeting_date ASC,
                         (purpose ILIKE '%%financial result%%') DESC,
                         (purpose ILIKE '%%result%%') DESC
                LIMIT 1
            """, (str(security_id),))
            row_fr = cur_fr.fetchone()
            if row_fr:
                md = row_fr.get("meeting_date")
                result["next_result_date"] = md.isoformat() if md and hasattr(md, "isoformat") else md
                result["next_result_purpose"] = row_fr.get("purpose")
                result["next_result_days_left"] = row_fr.get("days_left")
                # Trading-days countdown (used by event-risk guards + UI badges).
                # Calendar-day diff over a long weekend overstates how much
                # warning the trader really has.
                result["next_result_trading_days_left"] = trading_days_until(md)
            conn_fr.close()
    except Exception as e:
        print(f"⚠️ stock-data forthcoming_results: {e}")

    return jsonify(result)


@position_bp.route("/api/market/yahoo-data", methods=["POST"])
@limiter.limit("30 per minute")
def market_yahoo_data():
    """Fetch market cap + sector from Yahoo. Cached 24hrs. First call ~2s, subsequent instant."""
    data = request.json
    symbol = data.get("symbol")
    if not symbol:
        return jsonify({"market_cap_cr": None, "sector": None, "industry": None})

    try:
        from services.market_data_service import _fetch_yahoo_info
        yahoo = _fetch_yahoo_info(symbol)
        return jsonify(yahoo)
    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"market_cap_cr": None, "sector": None, "industry": None, "error": "Internal error"})


@position_bp.route("/api/positions/<int:pos_id>/refresh", methods=["POST"])
@limiter.limit("30 per minute")
def refresh_position(pos_id):
    """Refresh a single position with live market data"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM positions WHERE id = %s AND user_id = %s', (pos_id, g.user_id))
        pos = cur.fetchone()
        if not pos:
            return jsonify({"error": "Position not found"}), 404

        if not pos.get("security_id"):
            return jsonify({"error": "No security_id set — search and link a stock first"}), 400

        from services.market_data_service import refresh_position_data
        data = refresh_position_data(
            pos["security_id"],
            pos["entry_price"],
            pos.get("risk_pct", 4.0),
            pos.get("leg_base_price"),
            pos.get("valvo_ref_price"),
        )

        if not data:
            return jsonify({"error": "Could not fetch market data"}), 502

        # Update position — cold fields only (extensions). Hot fields go to
        # positions_live (phase-1 hot-cold split).
        cur.execute('''
            UPDATE positions SET
                leg_extension_pct = %s,
                entry_extension_pct = %s, valvo_extension_pct = %s,
                updated_at = %s
            WHERE id = %s AND user_id = %s
        ''', (
            data["leg_extension_pct"],
            data["entry_extension_pct"], data["valvo_extension_pct"],
            datetime.now(), pos_id, g.user_id,
        ))
        cur.execute('''
            UPDATE positions_live SET
                current_price = %s, current_r_multiple = %s,
                defensive_status = %s,
                last_5ma_price = %s, last_10ma_price = %s, last_20ma_price = %s,
                updated_at = NOW()
            WHERE position_id = %s
        ''', (
            data["current_price"], data["r_multiple"], data["defensive_status"],
            data["five_ma"], data.get("ten_ma"), data.get("twenty_ma"),
            pos_id,
        ))
        conn.commit()

        # Re-read full position + sidecar so frontend doesn't need a second call
        cur.execute('''
            SELECT p.*,
                   pl.current_price AS current_price,
                   pl.current_r_multiple AS current_r_multiple,
                   pl.defensive_status AS defensive_status,
                   pl.last_5ma_price AS last_5ma_price,
                   pl.last_10ma_price AS last_10ma_price,
                   pl.last_20ma_price AS last_20ma_price
            FROM positions p
            LEFT JOIN positions_live pl ON pl.position_id = p.id
            WHERE p.id = %s AND p.user_id = %s
        ''', (pos_id, g.user_id))
        full_pos = dict(cur.fetchone())
        for k in ["created_at", "updated_at", "entry_date"]:
            if full_pos.get(k) and hasattr(full_pos[k], "isoformat"):
                full_pos[k] = full_pos[k].isoformat()

        return jsonify({
            "refreshed": True,
            "position_id": pos_id,
            "position": full_pos,
            **data,
        })

    except ValueError as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 400
    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/positions/refresh-all", methods=["POST"])
@limiter.limit("30 per minute")
def refresh_all_positions():
    """Refresh all active positions with live market data (batch)"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM positions WHERE status = 'active' AND security_id IS NOT NULL AND user_id = %s", (g.user_id,))
        positions = cur.fetchall()

        if not positions:
            return jsonify({"refreshed": 0, "message": "No active positions with security_id"})

        from services.market_data_service import get_ltp, bulk_refresh_positions

        # Batch LTP fetch (all at once — 1 query)
        sec_ids = [{"security_id": p["security_id"], "exchange": "NSE_EQ"} for p in positions]
        ltp_map = get_ltp(sec_ids)

        # Batch MA fetch (all at once — 1 query instead of N)
        bulk_ma = bulk_refresh_positions(positions)

        results = []
        for pos in positions:
            sid = str(pos["security_id"])
            current_price = ltp_map.get(sid)

            if not current_price:
                results.append({"id": pos["id"], "stock": pos["stock_name"], "status": "no_price"})
                continue

            # Get MAs from bulk result (already computed for all positions)
            ma_data = bulk_ma.get(sid, {})
            five_ma = ma_data.get("five_ma")
            ten_ma = ma_data.get("ten_ma")
            twenty_ma = ma_data.get("twenty_ma")

            entry = pos["entry_price"]
            leg_base = pos.get("leg_base_price") or entry
            valvo_ref = pos.get("valvo_ref_price") or entry
            risk_pct = pos.get("risk_pct", 4.0)
            risk_per_share = entry * (risk_pct / 100.0)

            leg_ext = round(((current_price - leg_base) / leg_base * 100), 2) if leg_base else 0
            entry_ext = round(((current_price - entry) / entry * 100), 2) if entry else 0
            valvo_ext = round(((current_price - valvo_ref) / valvo_ref * 100), 2) if valvo_ref else 0
            r_mult = round(((current_price - entry) / risk_per_share), 2) if risk_per_share else 0

            defensive = "safe"
            if five_ma:
                diff = ((current_price - five_ma) / five_ma) * 100
                if diff < -1.0:
                    defensive = "break"
                elif diff < 0:
                    defensive = "marginal"

            cur.execute('''
                UPDATE positions SET
                    leg_extension_pct = %s,
                    entry_extension_pct = %s, valvo_extension_pct = %s,
                    updated_at = %s
                WHERE id = %s AND user_id = %s
            ''', (
                leg_ext, entry_ext, valvo_ext,
                datetime.now(), pos["id"], g.user_id,
            ))
            cur.execute('''
                UPDATE positions_live SET
                    current_price = %s, current_r_multiple = %s,
                    defensive_status = %s,
                    last_5ma_price = %s, last_10ma_price = %s, last_20ma_price = %s,
                    updated_at = NOW()
                WHERE position_id = %s
            ''', (
                current_price, r_mult, defensive,
                five_ma, ten_ma, twenty_ma, pos["id"],
            ))

            results.append({
                "id": pos["id"],
                "stock": pos["stock_name"],
                "price": current_price,
                "ext": entry_ext,
                "r": r_mult,
                "defensive": defensive,
                "status": "ok",
            })

        conn.commit()

        # Re-read ALL positions (JOIN sidecar) so frontend doesn't need a second call
        cur.execute('''
            SELECT p.*, pl.current_price, pl.current_r_multiple, pl.defensive_status,
                   pl.last_5ma_price, pl.last_10ma_price, pl.last_20ma_price
            FROM positions p
            LEFT JOIN positions_live pl ON pl.position_id = p.id
            WHERE p.status = 'active' AND p.user_id = %s
            ORDER BY p.created_at DESC
        ''', (g.user_id,))
        all_positions = [dict(r) for r in cur.fetchall()]
        for p in all_positions:
            for k in ["created_at", "updated_at", "entry_date"]:
                if p.get(k) and hasattr(p[k], "isoformat"):
                    p[k] = p[k].isoformat()
            # Same partial-exit derivation list_positions does — without this,
            # the Position page (which loads via refresh-all on mount) never
            # gets has_partial_exits / shares_remaining and the PARTIAL banner
            # never renders.
            _annotate_partial_exit_metadata(p)

        return jsonify({
            "refreshed": len([r for r in results if r["status"] == "ok"]),
            "total": len(positions),
            "results": results,
            "positions": all_positions,
        })

    except ValueError as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 400
    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@position_bp.route("/api/positions/<int:pos_id>/link-security", methods=["POST"])
@limiter.limit("30 per minute")
def link_security(pos_id):
    """Link a security_id to a position"""
    data = request.json
    security_id = data.get("security_id")
    trading_symbol = data.get("trading_symbol")

    if not security_id:
        return jsonify({"error": "security_id required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('''
            UPDATE positions SET security_id = %s, updated_at = %s WHERE id = %s AND user_id = %s RETURNING id
        ''', (str(security_id), datetime.now(), pos_id, g.user_id))
        updated = cur.fetchone()
        conn.commit()

        if not updated:
            return jsonify({"error": "Position not found"}), 404

        return jsonify({"linked": True, "security_id": security_id, "trading_symbol": trading_symbol})

    except Exception as e:
        conn.rollback()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# CHART DATA CACHE + ENDPOINTS
# ═══════════════════════════════════════════════════

_chart_cache = {}  # {"{security_id}_{days}": {"data": [...], "ts": datetime}}
_CHART_CACHE_TTL_OFFHOURS = 86400  # 24 hours — after market close, candles don't change
_CHART_CACHE_TTL_MARKET = 120      # 2 minutes — during market hours, keep it fresh

def _get_chart_cache_ttl():
    """Return cache TTL — short during market hours, long after close."""
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    market_mins = now_ist.hour * 60 + now_ist.minute
    # Market hours: 9:15 AM (555) to 3:45 PM (945) — extra 15min buffer after close
    if 555 <= market_mins <= 945 and now_ist.weekday() < 5:
        return _CHART_CACHE_TTL_MARKET
    return _CHART_CACHE_TTL_OFFHOURS


def _convert_candles(candles, IST):
    """Convert candles to lightweight-charts format. Handles both timestamp and date formats."""
    chart_data = []
    for c in candles:
        time_str = None

        # Method 1: Direct date string (from Supabase market_data_service)
        date_val = c.get("date")
        if date_val:
            if isinstance(date_val, str) and len(date_val) >= 10:
                time_str = date_val[:10]
            elif hasattr(date_val, "strftime"):
                time_str = date_val.strftime("%Y-%m-%d")

        # Method 2: Timestamp fallback
        if not time_str:
            ts = c.get("timestamp", 0)
            if isinstance(ts, (int, float)) and ts > 0:
                if ts > 1e11:
                    ts = ts / 1000
                try:
                    dt = datetime.fromtimestamp(ts, tz=IST)
                    time_str = dt.strftime("%Y-%m-%d")
                except (ValueError, OSError, TypeError, OverflowError):
                    continue
            elif isinstance(ts, str) and len(ts) >= 10:
                time_str = ts[:10]

        if not time_str:
            continue

        chart_data.append({
            "time": time_str,
            "open": round(c["open"], 2),
            "high": round(c["high"], 2),
            "low": round(c["low"], 2),
            "close": round(c["close"], 2),
            "volume": c.get("volume", 0),
        })

    # Sort ascending by date (charts need oldest first)
    chart_data.sort(key=lambda x: x["time"])
    return chart_data


@position_bp.route("/api/charts/precache", methods=["POST"])
# 30/min was tight when a user clicks through 3M / 6M / 1Y on Bird's
# Eye for several indices in quick succession. 60/min still bounds
# abuse but covers normal exploration patterns.
@limiter.limit("60 per minute")
def precache_charts():
    """
    Bulk pre-cache chart data for screener/sectoral stocks.
    Returns chart data so frontend can cache locally too.
    """
    data = request.json or {}
    security_ids = data.get("security_ids", [])
    days = data.get("days", 365)  # Must match ValvoChart default (365)
    if not security_ids:
        return jsonify({"error": "security_ids required", "cached": 0}), 400

    try:
        from services.market_data_service import bulk_get_chart_data
        from datetime import timezone

        IST = timezone(timedelta(hours=5, minutes=30))
        bulk_data = bulk_get_chart_data(security_ids, days=days)

        cached_count = 0
        now = datetime.now()
        # Pre-populate every requested sid with an empty list. Without this,
        # the response only contained stocks that *had* candles — leaving
        # the frontend unable to distinguish "still loading" (sid not in
        # response yet) from "definitively no data" (sid has no rows in
        # candles_daily). With explicit empty entries, the frontend can
        # render a stable "No data" placeholder immediately for missing
        # stocks instead of waiting 800ms then doing a wasted individual
        # fetch.
        charts = {str(sid): [] for sid in security_ids}
        missing = []
        for sid, candles in bulk_data.items():
            if candles:
                chart_data = _convert_candles(candles, IST)
                if chart_data:
                    _chart_cache[f"{sid}_{days}"] = {"data": chart_data, "ts": now}
                    charts[str(sid)] = chart_data
                    cached_count += 1
                    continue
            missing.append(str(sid))

        # Track which stocks had no data so we can surface the gap rate
        # in monitoring without scraping logs.
        if missing:
            print(f"[charts/precache] {len(missing)}/{len(security_ids)} stocks had no candles in last {days}d: "
                  f"{', '.join(missing[:10])}{'…' if len(missing) > 10 else ''}")

        return jsonify({
            "cached": cached_count,
            "total": len(security_ids),
            "missing_count": len(missing),
            "charts": charts,
        })

    except Exception as e:
        print(f"❌ Chart precache error: {e}")
        return jsonify({"error": "Internal error", "cached": 0}), 500


@position_bp.route("/api/market/chart-data", methods=["POST"])
# 30/min was too tight: Bird's Eye renders up to 100 charts and any
# stock the bulk precache misses falls back to this endpoint. One user
# opening one sectoral view could blow past the limit, leaving the rest
# of the tiles 429'ing. 240/min gives ~4 charts/second of headroom for
# fallback fetches without inviting abuse on a single read endpoint.
@limiter.limit("240 per minute")
def market_chart_data():
    """
    Fetch OHLCV for charting — powered by Supabase websocket data.
    Supports timeframe: daily (default) or weekly (server-side aggregation).
    """
    data = request.json
    security_id = data.get("security_id")
    days = data.get("days", 365)
    timeframe = data.get("timeframe", "daily")  # "daily" | "weekly"
    from_date = data.get("from_date")  # Optional: YYYY-MM-DD
    to_date = data.get("to_date")      # Optional: YYYY-MM-DD

    if not security_id:
        return jsonify({"error": "security_id required"}), 400

    # Check cache — short TTL during market hours, long after close
    cache_key = f"{security_id}_{from_date or days}_{to_date or ''}_{timeframe}"
    now = datetime.now()
    if cache_key in _chart_cache:
        cached = _chart_cache[cache_key]
        age = (now - cached["ts"]).total_seconds()
        if age < _get_chart_cache_ttl():
            return jsonify({"candles": cached["data"], "count": len(cached["data"]), "cached": True})

    try:
        if timeframe in ("weekly", "monthly"):
            # Try pre-computed table first, fall back to SQL aggregation
            table = "candles_weekly" if timeframe == "weekly" else "candles_monthly"
            from database.database import get_db, close_db
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SET LOCAL statement_timeout = '15000'")  # 15s max per query

            # Build date filter — use from_date/to_date if provided (Past Winners, custom ranges)
            if from_date and to_date:
                date_clause = "AND date >= %s AND date <= %s"
                date_params = (str(security_id), str(from_date), str(to_date))
            else:
                date_clause = "AND date >= (CURRENT_DATE - make_interval(days => %s))"
                date_params = (str(security_id), int(days))

            # Try pre-computed table (includes current incomplete period)
            try:
                cur.execute(f"""
                    SELECT date, open, high, low, close, volume
                    FROM {table}
                    WHERE security_id = %s
                      {date_clause}
                    ORDER BY date ASC
                """, date_params)
                rows = cur.fetchall()
            except Exception as e:
                print(f"[chart-data] Pre-computed {table} read failed: {e}")
                conn.rollback()
                rows = []
            # Fallback: SQL aggregation from candles_daily
            if not rows:
                if timeframe == "weekly":
                    cur.execute(f"""
                        SELECT MIN(date)::date as date,
                            (array_agg(open ORDER BY date ASC))[1] as open,
                            MAX(high) as high, MIN(low) as low,
                            (array_agg(close ORDER BY date DESC))[1] as close,
                            SUM(volume) as volume
                        FROM candles_daily
                        WHERE security_id = %s AND volume > 0
                          AND EXTRACT(dow FROM date) BETWEEN 1 AND 5
                          {date_clause}
                        GROUP BY EXTRACT(isoyear FROM date), EXTRACT(week FROM date)
                        ORDER BY date ASC
                    """, date_params)
                else:  # monthly
                    cur.execute(f"""
                        SELECT MIN(date)::date as date,
                            (array_agg(open ORDER BY date ASC))[1] as open,
                            MAX(high) as high, MIN(low) as low,
                            (array_agg(close ORDER BY date DESC))[1] as close,
                            SUM(volume) as volume
                        FROM candles_daily
                        WHERE security_id = %s AND volume > 0
                          AND EXTRACT(dow FROM date) BETWEEN 1 AND 5
                          {date_clause}
                        GROUP BY EXTRACT(year FROM date), EXTRACT(month FROM date)
                        ORDER BY date ASC
                    """, date_params)
                rows = cur.fetchall()
            close_db(conn)
            chart_data = [{"time": str(r["date"]), "open": round(float(r["open"]), 2),
                           "high": round(float(r["high"]), 2), "low": round(float(r["low"]), 2),
                           "close": round(float(r["close"]), 2), "volume": int(r["volume"] or 0)} for r in rows]
        else:
            # Daily candles (existing path)
            from services.market_data_service import get_chart_data
            candles = get_chart_data(security_id, days=days, from_date=from_date, to_date=to_date)
            if not candles:
                return jsonify({"error": f"No chart data for {security_id}", "candles": [], "count": 0})
            from datetime import timezone
            IST = timezone(timedelta(hours=5, minutes=30))
            chart_data = _convert_candles(candles, IST)

        if chart_data:
            _chart_cache[cache_key] = {"data": list(chart_data), "ts": now}
            if len(_chart_cache) > 2000:
                oldest = min(_chart_cache, key=lambda k: _chart_cache[k]["ts"])
                del _chart_cache[oldest]

        return jsonify({"security_id": security_id, "candles": chart_data, "count": len(chart_data)})

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error", "candles": [], "count": 0}), 500


@position_bp.route("/api/market/live-candle", methods=["POST"])
@limiter.limit("30 per minute")
def market_live_candle():
    """Fetch today's live OHLC for 10-second chart updates."""
    data = request.json
    security_id = data.get("security_id")
    if not security_id:
        return jsonify({"error": "security_id required"}), 400
    try:
        from services.market_data_service import get_live_candle
        candle = get_live_candle(security_id)
        if not candle:
            return jsonify({"error": "No live data", "candle": None})
        return jsonify({"candle": candle})
    except Exception as e:
        print(f"[position] error: {e}")
        return jsonify({"error": "Internal error", "candle": None}), 500

