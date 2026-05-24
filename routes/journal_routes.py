"""
journal_routes.py — Trading Journal API
CRUD operations with server-side calculated fields
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter
import psycopg2, psycopg2.extras, os, json
from datetime import datetime, date
from decimal import Decimal

from database.database import get_db, close_db
from services.entry_metrics import compute_entry_metrics
from services import portfolio_capital_log as capital_log
# phase 2b: sync_journal_trade_to_position no longer called from this module.

journal_bp = Blueprint("journal", __name__)

PORTFOLIO_CAPITAL = 50_000_000  # Default 5 Crore — will be configurable

def _get_db():
    return get_db()

def _close_db(conn):
    close_db(conn)


def _calc_fields(t, portfolio_capital=None):
    """Compute all derived fields from stored data.

    Phase 2a: pyramid legs come from positions.pyramid_history JSONB (joined in
    by list_trades). Expected shape: [{date, price, shares, slot}]. The journal
    no longer carries p1_*/p2_* columns. Pyramid shares count as BOUGHT qty,
    not exited qty — they contribute to cost basis, not realised P/L.
    """
    pc = portfolio_capital or PORTFOLIO_CAPITAL
    entry = t.get("entry_price") or 0
    avg_entry = t.get("avg_entry") or entry
    sl = t.get("sl") or 0
    initial_qty = t.get("initial_qty") or 0

    # Parse pyramid_history JSONB (joined from positions)
    pyr_raw = t.get("pyramid_history") or []
    if isinstance(pyr_raw, str):
        try:
            pyr_raw = json.loads(pyr_raw)
        except Exception:
            pyr_raw = []

    # Mirror p1_*/p2_* onto the row so the grid's EditCell renders the leg
    # values. journal_trades has no p1_*/p2_* columns, so pyramid_history
    # (joined from positions) is the only source — without this mirror, the
    # cells display "—" even when the position has pyramid legs.
    if isinstance(pyr_raw, list) and pyr_raw:
        def _pyslot(i, key):
            if i < len(pyr_raw) and isinstance(pyr_raw[i], dict):
                return pyr_raw[i].get(key)
            return None
        t["p1_price"] = _pyslot(0, "price")
        t["p1_qty"]   = _pyslot(0, "shares")
        t["p1_date"]  = _pyslot(0, "date")
        t["p1_sl"]    = _pyslot(0, "leg_sl")
        t["p2_price"] = _pyslot(1, "price")
        t["p2_qty"]   = _pyslot(1, "shares")
        t["p2_date"]  = _pyslot(1, "date")
        t["p2_sl"]    = _pyslot(1, "leg_sl")

    pyramid_qty = sum((leg.get("shares") or 0) for leg in pyr_raw)

    # Total bought = initial + pyramids
    total_bought_qty = initial_qty + pyramid_qty

    # Phase 2b: For linked trades, e1/e2/e3 slot values are derived from
    # positions.sell_history JSONB (joined in). For orphan trades (no linked
    # position) we fall back to the journal's own e_* columns.
    sh_raw = t.get("sell_history")
    if isinstance(sh_raw, str):
        try:
            sh_raw = json.loads(sh_raw)
        except Exception:
            sh_raw = None
    if isinstance(sh_raw, list) and sh_raw:
        def _slot(i, key):
            if i < len(sh_raw):
                leg = sh_raw[i]
                if key == "shares":
                    return leg.get("shares") or leg.get("qty") or leg.get("quantity") or 0
                return leg.get(key)
            return None
        t["e1_price"] = _slot(0, "price")
        t["e1_qty"]   = _slot(0, "shares")
        t["e1_date"]  = _slot(0, "date")
        t["e2_price"] = _slot(1, "price")
        t["e2_qty"]   = _slot(1, "shares")
        t["e2_date"]  = _slot(1, "date")
        t["e3_price"] = _slot(2, "price")
        t["e3_qty"]   = _slot(2, "shares")
        t["e3_date"]  = _slot(2, "date")

    # Pyramid-aware exits live on leg.exits[] (not in sell_history / e-slots).
    # Fold them into the journal's exit totals so Open Qty, Gross P/L, and
    # Position Status reflect pyramid sells too — otherwise a fully-closed
    # pyramid looks "still open" in the journal.
    pyramid_exited_qty = 0
    pyramid_exit_value = 0.0
    pyramid_cost_of_exited = 0.0
    pyramid_realised_pnl = 0.0
    for leg in pyr_raw:
        if not isinstance(leg, dict):
            continue
        leg_price = float(leg.get("price") or 0)
        for ex in (leg.get("exits") or []):
            if not isinstance(ex, dict):
                continue
            esh = int(ex.get("shares") or 0)
            if esh <= 0:
                continue
            eprice = float(ex.get("price") or 0)
            pyramid_exited_qty += esh
            pyramid_exit_value += eprice * esh
            pyramid_cost_of_exited += leg_price * esh
            pyramid_realised_pnl += float(ex.get("profit") or 0)

    # Exit quantities — E slots + pyramid-aware exits
    e1_qty = t.get("e1_qty") or 0
    e2_qty = t.get("e2_qty") or 0
    e3_qty = t.get("e3_qty") or 0
    e_only_qty = e1_qty + e2_qty + e3_qty
    total_exited_qty = e_only_qty + pyramid_exited_qty
    open_qty = max(0, total_bought_qty - total_exited_qty)

    # Position status
    position_status = "Open" if open_qty > 0 else "Closed"

    # SL %
    sl_pct = abs(entry - sl) / entry * 100 if entry > 0 and sl > 0 else 0

    # Exit prices
    e1_p = t.get("e1_price") or 0
    e2_p = t.get("e2_price") or 0
    e3_p = t.get("e3_price") or 0

    exit_value = (e1_p * e1_qty) + (e2_p * e2_qty) + (e3_p * e3_qty) + pyramid_exit_value
    avg_exit = exit_value / total_exited_qty if total_exited_qty > 0 else 0

    # Position size = cost basis of all shares bought (initial + pyramids)
    pyramid_cost = sum((leg.get("shares") or 0) * (leg.get("price") or 0) for leg in pyr_raw)
    initial_cost = entry * initial_qty if entry > 0 else 0
    position_size = initial_cost + pyramid_cost

    # Allocation %
    allocation_pct = (position_size / pc * 100) if pc > 0 else 0

    # Realised amount & Gross P/L
    # Regular E-slot exits book against avg_entry (blended book convention);
    # pyramid-aware exits book against the leg's own buy price (pyramid leg
    # cost removed when the sell fired — see record_sell pyramid-aware path).
    realised_amount = exit_value
    cost_of_exited = avg_entry * e_only_qty + pyramid_cost_of_exited
    gross_pl = realised_amount - cost_of_exited

    # PF Impact %
    pf_impact = gross_pl / pc * 100 if pc > 0 else 0

    # Stock move %
    if position_status == "Closed" and avg_exit > 0 and avg_entry > 0:
        stock_move = (avg_exit - avg_entry) / avg_entry * 100
    else:
        stock_move = 0

    # Reward:Risk (R-multiple)
    reward_risk = stock_move / sl_pct if sl_pct > 0 else 0

    # Holding days — last exit date wins; pyramid dates are irrelevant
    trade_date = t.get("trade_date")
    last_exit = t.get("e3_date") or t.get("e2_date") or t.get("e1_date")
    holding_days = 0
    if trade_date and last_exit:
        try:
            d1 = trade_date if isinstance(trade_date, date) else datetime.strptime(str(trade_date), "%Y-%m-%d").date()
            d2 = last_exit if isinstance(last_exit, date) else datetime.strptime(str(last_exit), "%Y-%m-%d").date()
            holding_days = (d2 - d1).days
        except (ValueError, TypeError, AttributeError):
            pass

    # Capital at risk %
    # Matches PositionList's riskPct: the active stop is max(sl, tsl), and a
    # stop sitting AT or ABOVE cost basis is locked profit, not risk.
    capital_at_risk = 0
    if open_qty > 0 and avg_entry > 0:
        tsl = float(t.get("tsl") or 0)
        active_sl = max(float(sl or 0), tsl)
        if active_sl > 0 and active_sl < avg_entry:
            capital_at_risk = (avg_entry - active_sl) * open_qty / pc * 100

    t["sl_pct"] = round(sl_pct, 2)
    t["open_qty"] = open_qty
    t["exited_qty"] = total_exited_qty
    t["avg_exit_price"] = round(avg_exit, 2)
    t["position_size"] = round(position_size, 2)
    t["allocation_pct"] = round(allocation_pct, 2)
    t["realised_amount"] = round(realised_amount, 2)
    t["gross_pl"] = round(gross_pl, 2)
    t["pf_impact_pct"] = round(pf_impact, 4)
    t["stock_move_pct"] = round(stock_move, 2)
    t["reward_risk"] = round(reward_risk, 2)
    t["holding_days"] = holding_days
    t["position_status"] = position_status
    t["capital_at_risk_pct"] = round(capital_at_risk, 2)
    return t


def _serialize(row):
    """Convert DB row to JSON-safe dict."""
    r = dict(row)
    for k, v in r.items():
        if isinstance(v, Decimal):
            r[k] = float(v)
        elif isinstance(v, (date, datetime)):
            r[k] = v.isoformat()
    # Parse setup JSONB
    if isinstance(r.get("setup"), str):
        try:
            r["setup"] = json.loads(r["setup"])
        except (json.JSONDecodeError, ValueError, TypeError):
            r["setup"] = []
    if r.get("setup") is None:
        r["setup"] = []
    return r


# ═══════════════════════════════════════════════════════════
# GET /api/journal/trades — List all trades with calculated fields
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/trades", methods=["GET"])
@limiter.limit("60 per minute")
def list_trades():
    status = request.args.get("status", "all")
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Base capital: single source of truth is user_fy_config.base_capital
        # (per-FY, written by the Settings page). journal_settings.portfolio_capital
        # was the stale duplicate — reading it meant editing capital in Settings
        # had no effect on journal math until the legacy column was also touched.
        pc = PORTFOLIO_CAPITAL
        try:
            from services.user_analytics_service import get_user_base_capital
            from routes.settings_routes import _current_fy_label
            bc = get_user_base_capital(cur, g.user_id, _current_fy_label())
            if bc and bc > 0:
                pc = float(bc)
        except Exception:
            try:
                conn.rollback()
            except Exception as rb_err:
                print(f"[journal] rollback failed: {rb_err}")

        # Phase 2b: JOIN positions and PREFER its trading data (entry, SL,
        # quantity, sell_history, pyramid_history) over the journal columns.
        # For linked trades, positions is the single source of truth. For
        # orphans (no linked position) the journal columns are the only data
        # we have — COALESCE falls back to them. RealDictCursor's last-key-
        # wins semantics mean we list positions' columns AFTER j.* so the
        # duplicate names resolve to the position side.
        cur.execute("""
            SELECT j.*,
                   COALESCE(p.entry_price, j.entry_price) AS entry_price,
                   COALESCE(p.stop_loss, j.sl)            AS sl,
                   COALESCE(p.trailing_sl, j.tsl)         AS tsl,
                   COALESCE(p.quantity, j.initial_qty)    AS initial_qty,
                   COALESCE(p.entry_price, j.avg_entry)   AS avg_entry,
                   p.sell_history                         AS sell_history,
                   p.pyramid_history                      AS pyramid_history,
                   p.status                               AS position_live_status
            FROM journal_trades j
            LEFT JOIN positions p ON p.id = j.position_id
            WHERE j.user_id = %s
            ORDER BY j.trade_no DESC
        """, (g.user_id,))
        raw_rows = cur.fetchall()

        # Serialize and calculate
        from routes.position_routes import _detect_position_health_issue
        rows = []
        for r in raw_rows:
            try:
                serialized = _serialize(r)
                calculated = _calc_fields(serialized, portfolio_capital=pc)
                # Flag trades whose linked position is in a corrupt state so
                # the grid can quarantine them until fixed. Orphan trades
                # (no linked position) also go through the same detector —
                # it runs on the joined row shape, so whether fields came
                # from positions or from the journal's own columns, the
                # validation semantics are identical.
                issue = _detect_position_health_issue(calculated)
                if issue:
                    calculated["health_issue"] = issue
                rows.append(calculated)
            except Exception as row_err:
                # Skip broken rows but include error info
                rows.append({"id": r.get("id"), "symbol": str(r.get("symbol", "?")), "error": str(row_err)})

        # Apply status filter on calculated position_status
        if status == "open":
            rows = [r for r in rows if r.get("position_status") == "Open"]
        elif status == "closed":
            rows = [r for r in rows if r.get("position_status") == "Closed"]

        # Compute cumulative PF impact (ordered by date ASC)
        sorted_closed = sorted([r for r in rows if r.get("position_status") == "Closed"], key=lambda x: x.get("trade_date") or "")
        cumm = 0
        cumm_map = {}
        for r in sorted_closed:
            cumm += r.get("pf_impact_pct", 0)
            cumm_map[r["id"]] = round(cumm, 4)

        for r in rows:
            r["cumm_pf_pct"] = cumm_map.get(r.get("id"), 0)

        return jsonify({"trades": rows, "count": len(rows)})
    except Exception as e:
        import traceback
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# GET /api/journal/debug — Quick test that route is alive
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/debug", methods=["GET"])
@limiter.limit("60 per minute")
def journal_debug():
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT COUNT(*) as cnt FROM journal_trades WHERE user_id = %s", (g.user_id,))
        cnt = cur.fetchone()["cnt"]
        # Try fetching one trade raw
        cur.execute("SELECT id, trade_no, symbol, entry_price FROM journal_trades WHERE user_id = %s LIMIT 1", (g.user_id,))
        sample = cur.fetchone()
        return jsonify({"status": "ok", "trade_count": cnt, "sample": dict(sample) if sample else None})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "error": "Internal error"})
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# POST /api/journal/trades/renumber — Re-number all trades sequentially by date
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/trades/renumber", methods=["POST"])
@limiter.limit("10 per minute")
def renumber_trades():
    """Re-assign trade_no 1,2,3... in chronological order (trade_date, then created_at)."""
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id FROM journal_trades WHERE user_id = %s ORDER BY trade_date ASC NULLS LAST, created_at ASC",
            (g.user_id,)
        )
        rows = cur.fetchall()
        for idx, row in enumerate(rows, start=1):
            cur.execute("UPDATE journal_trades SET trade_no = %s WHERE id = %s AND user_id = %s",
                        (idx, row["id"], g.user_id))
        conn.commit()
        return jsonify({"ok": True, "renumbered": len(rows)})
    except Exception as e:
        conn.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Failed to renumber trades"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/trades", methods=["POST"])
@limiter.limit("30 per minute")
def create_trade():
    data = request.get_json(force=True)
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Auto trade_no: max + 1 for this user
        cur.execute("SELECT COALESCE(MAX(trade_no), 0) + 1 as next_no FROM journal_trades WHERE user_id = %s", (g.user_id,))
        next_no = cur.fetchone()["next_no"]

        # ═══ AUTO-LINK or AUTO-CREATE position in PM ═══
        # First try to attach to an existing active position — matches on
        # stock_name (case-insensitive) or security_id. If no match exists,
        # create a new one so every journal trade also shows up in the
        # Position Manager (phase 2b: positions is the source of truth).
        position_id = data.get("position_id")
        symbol_in = data.get("symbol", "") or ""
        security_in = data.get("security_id", "") or ""
        name_in = data.get("name", "") or ""

        # Fallback-resolve security_id from stock_universe if the client
        # didn't send one. Without it the created position stores NULL,
        # which breaks chart / live-CMP / MA pipeline downstream.
        # Tries exact-symbol first, then fuzzy company_name match.
        if not security_in and (symbol_in or name_in):
            search_token = symbol_in or name_in
            try:
                cur.execute("""
                    SELECT security_id, symbol, company_name FROM stock_universe
                    WHERE is_active = true
                      AND (
                        UPPER(symbol) = UPPER(%s)
                        OR (LENGTH(%s) >= 3 AND company_name ILIKE %s)
                      )
                    ORDER BY CASE WHEN UPPER(symbol) = UPPER(%s) THEN 0 ELSE 1 END
                    LIMIT 1
                """, (search_token, search_token, f"%{search_token}%", search_token))
                uni = cur.fetchone()
                if uni and uni.get("security_id"):
                    security_in = uni["security_id"]
                    if not name_in and uni.get("company_name"):
                        name_in = uni["company_name"]
            except Exception as resolve_err:
                print(f"[journal] security_id resolve failed for {search_token}: {resolve_err}")

        # FY26-27 architectural lock: refuse the write at API layer with a
        # readable error before the DB CHECK constraint trips. Pre-FY26-27
        # rows bypass (back-dated/legacy entries are still allowed).
        trade_date_in = data.get("trade_date") or date.today().isoformat()
        if trade_date_in >= "2026-04-01" and not security_in:
            return jsonify({
                "error": "Stock not found in catalog. Pick from the suggestions or use the exact NSE symbol (e.g. RELIANCE, TCS).",
                "error_code": "unresolvable_security",
                "stock_query": symbol_in or name_in,
            }), 400

        if not position_id and (symbol_in or security_in):
            cur.execute("""
                SELECT id FROM positions
                WHERE status = 'active'
                AND user_id = %s
                AND (LOWER(stock_name) = LOWER(%s) OR (security_id IS NOT NULL AND security_id = %s))
                LIMIT 1
            """, (g.user_id, symbol_in, security_in))
            match = cur.fetchone()
            if match:
                position_id = match["id"]

        # Snapshot 20-day liquidity + market cap at the entry date so the AI /
        # analytics layer can later answer "do I do better in mid-cap entries"
        # questions. Computed once per request and reused for both the
        # positions and journal_trades INSERTs below — both are 'at entry'
        # snapshots that should never diverge.
        liq_at_entry, mcap_at_entry = compute_entry_metrics(
            security_in or None, data.get("trade_date"), conn=conn
        )

        if not position_id:
            try:
                entry_price_num = float(data.get("entry_price") or 0)
                qty_num = int(data.get("initial_qty") or 0)
            except (TypeError, ValueError):
                entry_price_num, qty_num = 0.0, 0
            if entry_price_num > 0 and qty_num > 0:
                sl_raw = data.get("sl")
                try:
                    sl_num = float(sl_raw) if sl_raw not in (None, "") else None
                except (TypeError, ValueError):
                    sl_num = None
                # Default SL to 4% below entry if the user didn't give one —
                # keeps risk_pct / one_r_value sane downstream.
                if sl_num is None or sl_num <= 0 or sl_num >= entry_price_num:
                    sl_num = round(entry_price_num * 0.96, 2)
                risk_pct = round((entry_price_num - sl_num) / entry_price_num * 100, 2)
                stock_name_for_pos = name_in or symbol_in or "UNKNOWN"
                cur.execute(
                    """
                    INSERT INTO positions
                        (stock_name, entry_price, initial_entry_price, stop_loss, initial_sl, quantity,
                         position_value, one_r_value, risk_pct,
                         source, market_regime, regime_source,
                         leg_base_price, valvo_ref_price, current_price,
                         security_id, entry_date, status, total_cost_outlay, user_id,
                         liquidity_at_entry, mcap_at_entry,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'journal', 'bull', 'manual',
                            %s, %s, %s,
                            %s, %s, 'active', %s, %s,
                            %s, %s,
                            NOW(), NOW())
                    RETURNING id
                    """,
                    (
                        stock_name_for_pos, entry_price_num, entry_price_num, sl_num, sl_num, qty_num,
                        entry_price_num * qty_num,
                        entry_price_num * qty_num * risk_pct / 100,
                        risk_pct,
                        entry_price_num, entry_price_num, entry_price_num,
                        security_in or None,
                        data.get("trade_date"),
                        round(entry_price_num * qty_num, 2),
                        g.user_id,
                        liq_at_entry, mcap_at_entry,
                    ),
                )
                created = cur.fetchone()
                if created:
                    position_id = created["id"]

        cur.execute("""
            INSERT INTO journal_trades (
                user_id, trade_no, trade_date, symbol, name, setup, entry_type,
                self_rating, buy_sell, entry_price, avg_entry, sl,
                initial_qty, sector, security_id, position_id,
                liquidity_at_entry, mcap_at_entry
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s
            ) RETURNING *
        """, (
            g.user_id,
            data.get("trade_no", next_no),
            data.get("trade_date"),
            symbol_in or data.get("symbol", ""),
            name_in or symbol_in or "",
            json.dumps(data.get("setup", [])),
            data.get("entry_type", "BREAKOUT"),
            data.get("self_rating", 0),
            data.get("buy_sell", "Buy"),
            data.get("entry_price"),
            data.get("avg_entry") or data.get("entry_price"),
            data.get("sl"),
            data.get("initial_qty", 0),
            data.get("sector", ""),
            security_in or "",
            position_id,
            liq_at_entry, mcap_at_entry,
        ))
        row = _calc_fields(_serialize(cur.fetchone()))
        row["position_linked"] = position_id is not None
        conn.commit()
        return jsonify(row), 201
    except Exception as e:
        conn.rollback()
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


def _apply_trading_writes_to_position(cur, pos_id, user_id, data):
    """Phase 2b-bis: when a LINKED journal trade is edited, trading-field
    writes get redirected to positions (the single source of truth).

    Field mapping:
      entry_price -> positions.entry_price
      avg_entry   -> ignored (derived from pyramids, not user-editable)
      sl          -> positions.stop_loss
      tsl         -> positions.trailing_sl
      initial_qty -> positions.quantity
      e{1,2,3}_*  -> mutate positions.sell_history[idx] + recompute

    Silently skips writes to non-existent e-slots (user must record the sell
    via PM's Partial Sell first — can't inline-create from the journal grid).
    Journal's own stored columns are left alone for linked rows; they're
    shadowed by the JOIN in list_trades.
    """
    # Simple field renames: entry_price, sl, tsl, initial_qty
    pos_sets = []
    pos_vals = []
    if "entry_price" in data and data["entry_price"] is not None:
        pos_sets.append("entry_price = %s")
        pos_vals.append(data["entry_price"])
    if "sl" in data and data["sl"] is not None:
        pos_sets.append("stop_loss = %s")
        pos_vals.append(data["sl"])
    if "tsl" in data:
        # tsl can be null (clearing a trailing SL); pass through
        pos_sets.append("trailing_sl = %s")
        pos_vals.append(data["tsl"])
    if "initial_qty" in data and data["initial_qty"] is not None:
        pos_sets.append("quantity = %s")
        pos_vals.append(data["initial_qty"])

    if pos_sets:
        pos_sets.append("updated_at = NOW()")
        pos_vals.extend([pos_id, user_id])
        cur.execute(
            f"UPDATE positions SET {', '.join(pos_sets)} WHERE id = %s AND user_id = %s",
            pos_vals,
        )
        # Keep position_value + one_r_value in step if entry/qty moved
        cur.execute(
            """
            UPDATE positions p SET
              position_value = ROUND((p.entry_price * p.quantity)::numeric, 2)::double precision,
              one_r_value = CASE
                WHEN p.stop_loss IS NOT NULL AND p.stop_loss > 0
                 AND p.entry_price > p.stop_loss
                THEN ROUND((ABS(p.entry_price::numeric - p.stop_loss::numeric) * p.quantity)::numeric)::double precision
                ELSE p.one_r_value END,
              risk_pct = CASE
                WHEN p.stop_loss IS NOT NULL AND p.stop_loss > 0
                 AND p.entry_price > p.stop_loss
                THEN ROUND((ABS(p.entry_price::numeric - p.stop_loss::numeric) / p.entry_price::numeric * 100), 2)::double precision
                ELSE p.risk_pct END
            WHERE p.id = %s AND p.user_id = %s
            """,
            (pos_id, user_id),
        )

    # Exit slot edits: e1_* -> slot 0, e2_* -> 1, e3_* -> 2
    slot_edits = {}  # {slot_idx: {price, shares, date}}
    for key in ("e1_price", "e1_qty", "e1_date",
                "e2_price", "e2_qty", "e2_date",
                "e3_price", "e3_qty", "e3_date"):
        if key not in data:
            continue
        idx = int(key[1]) - 1
        field = key.split("_", 1)[1]  # 'price' | 'qty' | 'date'
        if field == "qty":
            field = "shares"
        slot_edits.setdefault(idx, {})[field] = data[key]

    # Pyramid slot edits: p1_* -> leg 0, p2_* -> leg 1 (parsed up here so the
    # no-exit-edits early-return still lets pyramid edits through).
    pyr_edits = {}  # {leg_idx: {price|shares|date|leg_sl: value}}
    _PYR_FIELD = {"price": "price", "qty": "shares", "date": "date", "sl": "leg_sl"}
    for key in ("p1_price", "p1_qty", "p1_date", "p1_sl",
                "p2_price", "p2_qty", "p2_date", "p2_sl"):
        if key not in data:
            continue
        idx = int(key[1]) - 1
        raw_field = key.split("_", 1)[1]
        pyr_edits.setdefault(idx, {})[_PYR_FIELD[raw_field]] = data[key]

    # If neither exit nor pyramid slot edits, skip the rebuilds but STILL
    # validate post-write state — simple-field edits (e.g. initial_qty) can
    # also violate invariants (dropping qty below already-sold shares).
    if not slot_edits and not pyr_edits:
        from routes.position_routes import _validate_position_invariants
        _validate_position_invariants(cur, pos_id, user_id)
        return

    # Pull the current sell_history + entry_price in one shot
    cur.execute(
        "SELECT entry_price, sell_history FROM positions WHERE id = %s AND user_id = %s",
        (pos_id, user_id),
    )
    prow = cur.fetchone()
    if not prow:
        return
    sh = prow["sell_history"] or []
    if isinstance(sh, str):
        try:
            sh = json.loads(sh)
        except Exception:
            sh = []
    entry_price = float(prow["entry_price"] or 0)

    # Process slot edits in ascending order so array indices stay valid.
    # Detect "delete" intent (all three fields nulled for a slot) so users
    # can clear a partial exit by blanking its row in the journal grid.
    dirty = False
    for idx in sorted(slot_edits.keys()):
        if idx >= len(sh):
            # Can't create a new slot from journal — user has to go to PM's
            # Partial Sell flow. Skip silently.
            continue
        edits = slot_edits[idx]
        all_null = all(v is None for v in edits.values()) and set(edits.keys()) >= {"price", "shares", "date"}
        if all_null:
            # User nulled the whole slot -> treat as delete
            sh.pop(idx)
            dirty = True
            continue
        entry_existing = sh[idx]
        if "price" in edits and edits["price"] is not None:
            entry_existing["price"] = float(edits["price"])
        if "shares" in edits and edits["shares"] is not None:
            entry_existing["shares"] = int(edits["shares"])
        if "date" in edits and edits["date"] is not None:
            entry_existing["date"] = edits["date"]
        # Re-derive profit + extension from updated price/shares
        new_price = float(entry_existing.get("price") or 0)
        new_shares = int(entry_existing.get("shares") or 0)
        if entry_price > 0 and new_price > 0:
            entry_existing["profit"] = round((new_price - entry_price) * new_shares, 2)
            entry_existing["extension"] = round(((new_price - entry_price) / entry_price) * 100, 2)
        sh[idx] = entry_existing
        dirty = True

    if dirty:
        # Re-use the position-side recompute helper (bucket_%, status flip, etc.)
        from routes.position_routes import _recompute_after_sellhistory_change
        _recompute_after_sellhistory_change(cur, pos_id, user_id, sh)

    # Process pyramid slot edits (pyr_edits parsed up top). Silently skip
    # legs that don't exist — user must add the pyramid via PM's Pyramid
    # button first; can't inline-create from the journal grid (same
    # constraint as exits).
    if pyr_edits:
        cur.execute(
            "SELECT pyramid_history FROM positions WHERE id = %s AND user_id = %s",
            (pos_id, user_id),
        )
        prow2 = cur.fetchone()
        pyr = (prow2 and prow2["pyramid_history"]) or []
        if isinstance(pyr, str):
            try: pyr = json.loads(pyr)
            except Exception: pyr = []

        pyr_dirty = False
        # Process highest-index slots first so a P1 delete doesn't shift P2
        # before P2's edits apply.
        for idx in sorted(pyr_edits.keys(), reverse=True):
            if idx >= len(pyr):
                continue
            edits = pyr_edits[idx]
            # Null in ANY pyramid field = "remove the typed-by-mistake entry"
            # — drop the whole leg (mirrors exits' delete-on-null behavior).
            # Safety: refuse delete if leg has pyramid-aware exits, since
            # those carry realised P&L that would silently vanish.
            if any(edits.get(k) is None for k in ("price", "shares", "date", "leg_sl")):
                leg = pyr[idx] if isinstance(pyr[idx], dict) else {}
                if int(leg.get("exited_shares") or 0) > 0:
                    from routes.position_routes import InvariantError
                    raise InvariantError(
                        f"Cannot delete P{idx+1} — leg has {leg['exited_shares']} "
                        "pyramid-aware exits. Unwind exits in Position Manager first."
                    )
                pyr.pop(idx)
                pyr_dirty = True
                continue
            leg = pyr[idx] if isinstance(pyr[idx], dict) else {}
            for f, v in edits.items():
                if f == "shares":
                    leg[f] = int(v)
                elif f in ("price", "leg_sl"):
                    leg[f] = float(v)
                else:
                    leg[f] = v
            pyr[idx] = leg
            pyr_dirty = True

        if pyr_dirty:
            from routes.position_routes import _recompute_after_pyramid_history_change
            _recompute_after_pyramid_history_change(cur, pos_id, user_id, pyr)

    # Validate the new state AFTER all writes (simple fields + exit slots).
    # Journal editors can easily violate invariants — e.g. drop initial_qty
    # from 100 to 10 while 50 shares are already sold — so the check is
    # done here with the post-write state loaded fresh from DB. Caller
    # (update_trade) lets InvariantError bubble and converts to 400.
    from routes.position_routes import _validate_position_invariants
    _validate_position_invariants(cur, pos_id, user_id)


# ═══════════════════════════════════════════════════════════
# PUT /api/journal/trades/<id> — Update a trade (inline edit)
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/trades/<int:trade_id>", methods=["PUT"])
@limiter.limit("30 per minute")
def update_trade(trade_id):
    data = request.get_json(force=True)
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Phase 2b-bis: trading fields on LINKED journal trades are redirected
        # to the authoritative position instead of stored on journal. This
        # keeps the journal grid editable (SL, entry, qty, exits) without
        # re-introducing bidirectional sync — the write goes straight to the
        # one source of truth.
        #
        # Determine if this trade is linked BEFORE building the UPDATE so we
        # can strip trading fields out of the journal payload in that case.
        cur.execute("SELECT position_id FROM journal_trades WHERE id = %s AND user_id = %s", (trade_id, g.user_id))
        link_row = cur.fetchone()
        if not link_row:
            return jsonify({"error": "Trade not found"}), 404
        linked_pos_id = link_row.get("position_id")

        # Extract + apply trading writes to positions for linked trades
        if linked_pos_id:
            _apply_trading_writes_to_position(cur, linked_pos_id, g.user_id, data)

        # Build SET clause from provided fields. Pyramid p1_*/p2_* edits are
        # routed (for LINKED trades) to positions.pyramid_history via
        # _apply_trading_writes_to_position — journal_trades has no p1_*/p2_*
        # columns, so they are intentionally NOT in ALLOWED. Orphan trades
        # silently drop these edits (no pyramid storage for orphans).
        ALLOWED = {
            "trade_no", "trade_date", "symbol", "name", "entry_type",
            "self_rating", "buy_sell", "entry_price", "avg_entry", "sl",
            "initial_qty", "tsl",
            "e1_price", "e1_qty", "e1_date",
            "e2_price", "e2_qty", "e2_date",
            "e3_price", "e3_qty", "e3_date",
            "plan_followed", "exit_trigger", "growth_areas",
            "base_duration", "notes", "chart_image", "sector", "security_id",
        }

        # For linked trades, strip the trading fields from the journal UPDATE
        # so they don't also get written to the duplicate journal column
        # (would be a stale mirror). Orphan trades keep the full ALLOWED
        # behaviour — their only storage is journal_trades.
        _POS_ROUTED = {
            "entry_price", "avg_entry", "sl", "tsl", "initial_qty",
            "p1_price", "p1_qty", "p1_date", "p1_sl",
            "p2_price", "p2_qty", "p2_date", "p2_sl",
            "e1_price", "e1_qty", "e1_date",
            "e2_price", "e2_qty", "e2_date",
            "e3_price", "e3_qty", "e3_date",
        }
        effective_allowed = ALLOWED - _POS_ROUTED if linked_pos_id else ALLOWED

        sets = []
        vals = []
        for k, v in data.items():
            if k == "setup":
                sets.append("setup = %s")
                vals.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
            elif k in effective_allowed:
                sets.append(f"{k} = %s")
                vals.append(v)

        if sets:
            sets.append("updated_at = CURRENT_TIMESTAMP")
            vals.extend([trade_id, g.user_id])
            cur.execute(f"UPDATE journal_trades SET {', '.join(sets)} WHERE id = %s AND user_id = %s RETURNING *", vals)
            row = cur.fetchone()
        else:
            # Nothing left to write to journal (all fields were routed to
            # positions). Re-fetch the row so we can return the updated state.
            cur.execute("SELECT * FROM journal_trades WHERE id = %s AND user_id = %s", (trade_id, g.user_id))
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "Trade not found"}), 404

        row = _serialize(row)
        result = _calc_fields(row)
        conn.commit()
        return jsonify(result)
    except Exception as e:
        # Surface position-invariant violations as 400 with a user-readable
        # message. Import guarded so a circular import between journal and
        # position routes doesn't cascade here.
        try:
            from routes.position_routes import InvariantError
            if isinstance(e, InvariantError):
                conn.rollback()
                return jsonify({"error": str(e)}), 400
        except Exception:
            pass
        conn.rollback()
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# DELETE /api/journal/trades/<id> — Delete a trade
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/trades/<int:trade_id>", methods=["DELETE"])
@limiter.limit("15 per minute")
def delete_trade(trade_id):
    """Delete a journal trade.

    Behavior:
      - position_id NULL              -> delete journal row.
      - position_id points to NOTHING -> dangling FK, auto-clear and delete
        (this used to trap users — Data Patterns symptom).
      - position_id points to ACTIVE position -> 409 unless ?cascade=true.
        Cascade also closes/deletes the position to keep the system
        in lockstep.
      - position_id points to CLOSED position -> delete both rows together
        (the round-trip is being undone; PM showed the closed row but
        the user wants both gone).
    """
    cascade = request.args.get("cascade", "").lower() in ("1", "true", "yes")
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT jt.position_id, p.id AS pos_exists, p.status AS pos_status
            FROM journal_trades jt
            LEFT JOIN positions p ON p.id = jt.position_id AND p.user_id = jt.user_id
            WHERE jt.id = %s AND jt.user_id = %s
            """,
            (trade_id, g.user_id),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Trade not found"}), 404

        pos_id = row["position_id"]
        pos_exists = row["pos_exists"]
        pos_status = (row["pos_status"] or "").lower() if row["pos_status"] else None

        # Dangling FK — position deleted out from under the journal row. Always safe
        # to clear and proceed; refusing here is the trap the user just hit.
        if pos_id and not pos_exists:
            cur.execute(
                "UPDATE journal_trades SET position_id = NULL, updated_at = NOW() WHERE id = %s AND user_id = %s",
                (trade_id, g.user_id),
            )
            pos_id = None

        # Active link without cascade — refuse, but offer the cascade hint.
        if pos_id and pos_status == "active" and not cascade:
            return jsonify({
                "error": "This trade is linked to an active position. Pass ?cascade=true to delete both, or close the position first.",
                "position_id": pos_id,
                "linked": True,
                "cascade_hint": f"DELETE /api/journal/trades/{trade_id}?cascade=true",
            }), 409

        # Cascade or closed-position case: delete the position first to silence
        # the bidirectional sync trigger, then delete the journal row.
        if pos_id and (cascade or pos_status == "closed"):
            # Drop equity-curve log rows so realized P&L from this deleted
            # trade stops inflating Portfolio Capital. FK CASCADE on
            # portfolio_capital_log.position_id covers this too, but the
            # explicit call mirrors position_routes.delete_position.
            capital_log.delete_for_position(cur, g.user_id, pos_id)
            cur.execute("DELETE FROM positions WHERE id = %s AND user_id = %s", (pos_id, g.user_id))

        cur.execute("DELETE FROM journal_trades WHERE id = %s AND user_id = %s", (trade_id, g.user_id))
        if cur.rowcount == 0:
            conn.rollback()
            return jsonify({"error": "Trade not found"}), 404
        conn.commit()
        return jsonify({"deleted": True, "id": trade_id, "position_id_cleared": pos_id})
    except Exception as e:
        conn.rollback()
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# POST /api/journal/backfill-positions — Create journal entries for existing positions
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/backfill-positions", methods=["POST"])
@limiter.limit("30 per minute")
def backfill_positions():
    """One-time: create journal_trades entries for active positions that don't have one yet."""
    conn = get_db()
    try:
        cur = conn.cursor()
        # Find active positions with no linked journal entry
        cur.execute('''
            SELECT p.id, p.stock_name, p.entry_price, p.stop_loss, p.quantity, p.security_id,
                   p.created_at, p.entry_date, p.liquidity_at_entry, p.mcap_at_entry
            FROM positions p
            LEFT JOIN journal_trades j ON j.position_id = p.id AND j.user_id = %s
            WHERE p.user_id = %s AND p.status = 'active' AND j.id IS NULL
        ''', (g.user_id, g.user_id))
        missing = cur.fetchall()
        if not missing:
            return jsonify({"backfilled": 0, "message": "All positions already have journal entries"})

        cur.execute('SELECT COALESCE(MAX(trade_no), 0) as max_no FROM journal_trades WHERE user_id = %s', (g.user_id,))
        next_no = cur.fetchone()["max_no"] + 1

        now = datetime.now()
        for p in missing:
            liq = p.get("liquidity_at_entry")
            mcap = p.get("mcap_at_entry")
            if (liq is None and mcap is None) and p.get("security_id"):
                liq, mcap = compute_entry_metrics(
                    p["security_id"], p.get("entry_date") or p.get("created_at"), conn=conn
                )
            cur.execute('''
                INSERT INTO journal_trades (trade_no, trade_date, symbol, name, buy_sell,
                    security_id, entry_price, avg_entry, sl, initial_qty,
                    position_id, notes, created_at, updated_at, user_id,
                    liquidity_at_entry, mcap_at_entry)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                next_no, p["created_at"] or now, p["stock_name"], p["stock_name"],
                'Buy', p.get("security_id"),
                p["entry_price"], p["entry_price"], p.get("stop_loss"),
                int(p["quantity"]),
                p["id"], "Backfilled from existing position",
                now, now, g.user_id,
                liq, mcap,
            ))
            next_no += 1

        conn.commit()
        return jsonify({"backfilled": len(missing), "message": f"Created {len(missing)} journal entries"})
    except Exception as e:
        conn.rollback()
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# GET /api/journal/settings — Get journal settings
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/settings", methods=["GET"])
@limiter.limit("60 per minute")
def get_journal_settings():
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM journal_settings WHERE user_id = %s", (g.user_id,))
        row = cur.fetchone()
        if not row:
            # portfolio_capital column is deprecated — don't seed it. Base
            # capital lives on user_fy_config per FY (overlaid below).
            cur.execute("""
                INSERT INTO journal_settings (user_id) VALUES (%s)
                ON CONFLICT (user_id) DO NOTHING
            """, (g.user_id,))
            conn.commit()
            cur.execute("SELECT * FROM journal_settings WHERE user_id = %s", (g.user_id,))
            row = cur.fetchone()

        # Overlay current-FY base capital so legacy clients reading
        # `portfolio_capital` off this endpoint see the live value. The
        # source of truth is user_fy_config; new code should use /api/settings.
        pc_from_fy = None
        try:
            from services.user_analytics_service import get_user_base_capital
            from routes.settings_routes import _current_fy_label
            pc_from_fy = get_user_base_capital(cur, g.user_id, _current_fy_label())
        except Exception:
            pass

        if row:
            out = {k: float(v) if isinstance(v, (int, float)) or (hasattr(v, '__float__')) else str(v) if hasattr(v, 'isoformat') else v for k, v in dict(row).items()}
            if pc_from_fy and pc_from_fy > 0:
                out["portfolio_capital"] = float(pc_from_fy)
            return jsonify(out)
        return jsonify({"portfolio_capital": float(pc_from_fy) if pc_from_fy else 50000000, "tax_rate_stcg": 20, "tax_rate_ltcg": 12.5, "ltcg_exemption": 125000})
    except Exception as e:
        return jsonify({"portfolio_capital": 50000000, "tax_rate_stcg": 20, "tax_rate_ltcg": 12.5, "ltcg_exemption": 125000})
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# PUT /api/journal/settings — Update journal settings
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/settings", methods=["PUT"])
@limiter.limit("30 per minute")
def update_journal_settings():
    data = request.get_json(force=True)
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # portfolio_capital is DEPRECATED — base capital is per-FY now and
        # lives in user_fy_config (source of truth). If the caller sends
        # portfolio_capital, redirect it to user_fy_config for the current FY
        # and don't touch the legacy column. Remaining fields still go to
        # journal_settings (tax rates + currency are genuinely journal-scoped).
        if "portfolio_capital" in data:
            try:
                amount = float(data["portfolio_capital"])
                if amount >= 0:
                    from services.user_analytics_service import set_user_base_capital
                    from datetime import date
                    today = date.today()
                    year = today.year if today.month >= 4 else today.year - 1
                    end = str((year + 1) % 100).zfill(2)
                    set_user_base_capital(cur, g.user_id, f"{year}-{end}", amount)
                    conn.commit()
            except (TypeError, ValueError):
                pass

        allowed = {"tax_rate_stcg", "tax_rate_ltcg", "ltcg_exemption", "currency"}
        sets, vals = [], []
        for k, v in data.items():
            if k in allowed:
                sets.append(f"{k} = %s")
                vals.append(v)
        if not sets:
            # portfolio_capital redirected above — return success so the UI
            # doesn't complain about "no valid fields" on a pure-capital save.
            if "portfolio_capital" in data:
                return jsonify({"ok": True})
            return jsonify({"error": "No valid fields"}), 400
        sets.append("updated_at = CURRENT_TIMESTAMP")
        vals.append(g.user_id)
        cur.execute(f"UPDATE journal_settings SET {', '.join(sets)} WHERE user_id = %s RETURNING *", vals)
        row = cur.fetchone()
        conn.commit()
        return jsonify({k: float(v) if hasattr(v, '__float__') else str(v) if hasattr(v, 'isoformat') else v for k, v in dict(row).items()})
    except Exception as e:
        conn.rollback()
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# GET /api/journal/fund-months?year=2026 — Get fund months
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/fund-months", methods=["GET"])
@limiter.limit("60 per minute")
def get_fund_months():
    year = request.args.get("year", datetime.now().year, type=int)
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Auto-seed months if they don't exist for this user
        for m in range(1, 13):
            cur.execute("""
                INSERT INTO journal_fund_months (user_id, year, month, added)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, year, month) DO NOTHING
            """, (g.user_id, year, m, 50000000 if m == 1 and year == 2026 else 0))
        conn.commit()
        cur.execute("SELECT * FROM journal_fund_months WHERE user_id = %s AND year = %s ORDER BY month", (g.user_id, year))
        rows = cur.fetchall()
        return jsonify({"months": [dict(r) for r in rows], "year": year})
    except Exception as e:
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error", "months": []}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# PUT /api/journal/fund-months/:id — Update a fund month
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/fund-months/<int:month_id>", methods=["PUT"])
@limiter.limit("30 per minute")
def update_fund_month(month_id):
    data = request.get_json(force=True)
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        allowed = {"added", "withdrawn", "notes"}
        sets, vals = [], []
        for k, v in data.items():
            if k in allowed:
                sets.append(f"{k} = %s")
                vals.append(v)
        if not sets:
            return jsonify({"error": "No valid fields"}), 400
        sets.append("updated_at = CURRENT_TIMESTAMP")
        vals.extend([month_id, g.user_id])
        cur.execute(f"UPDATE journal_fund_months SET {', '.join(sets)} WHERE id = %s AND user_id = %s RETURNING *", vals)
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Month not found"}), 404
        return jsonify(dict(row))
    except Exception as e:
        conn.rollback()
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════
# JOURNAL COMPLETION — Incomplete entries for nudge system
# ═══════════════════════════════════════════════════

def _journal_completion_pct(t):
    """Compute how 'complete' a journal trade entry is (0-100%).
    Open trades: core(40) + setup(15) + entry_type(10) + rating(10) + not-closed(25) = 100
    Closed trades: core(40) + setup(15) + entry_type(10) + rating(10) + plan(10) + exit(10) + growth(5) = 100
    """
    score = 0
    # Core fields (40%) — auto-filled from position
    if t.get("symbol"): score += 10
    if t.get("entry_price"): score += 10
    if t.get("sl"): score += 10
    if t.get("initial_qty") and t["initial_qty"] > 0: score += 10
    # Setup, type & rating (35%)
    setup = t.get("setup")
    if setup and setup != '[]' and setup != []: score += 15
    if t.get("entry_type"): score += 10
    if t.get("self_rating") and t["self_rating"] > 0: score += 10
    # Post-close fields (25%) — only count if trade is closed
    is_closed = t.get("e1_price") is not None or t.get("position_status") == "Closed"
    if is_closed:
        if t.get("plan_followed"): score += 10
        if t.get("exit_trigger"): score += 10
        if t.get("growth_areas"): score += 5
    else:
        score += 25  # Not closed yet — don't penalize for post-close fields
    return min(score, 100)


@journal_bp.route("/api/journal/incomplete", methods=["GET"])
@limiter.limit("60 per minute")
def journal_incomplete():
    """Return incomplete journal entries that need user attention (nudge system).
    Only returns trades from the current financial year (FY starts April 1)."""
    conn = get_db()
    try:
        cur = conn.cursor()

        # Current financial year starts April 1
        today = date.today()
        fy_start = date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)

        cur.execute('''
            SELECT id, trade_no, symbol, name, entry_price, sl, initial_qty,
                   setup, entry_type, self_rating,
                   e1_price, plan_followed, exit_trigger, growth_areas,
                   position_id, created_at
            FROM journal_trades
            WHERE user_id = %s AND created_at >= %s
            ORDER BY created_at DESC
        ''', (g.user_id, fy_start))
        rows = [dict(r) for r in cur.fetchall()]

        incomplete = []
        for r in rows:
            pct = _journal_completion_pct(r)
            if pct < 100:
                r["completion_pct"] = pct
                r["created_at"] = str(r["created_at"]) if r.get("created_at") else None
                # Determine which fields are missing
                missing = []
                setup = r.get("setup")
                if not setup or setup == '[]' or setup == []: missing.append("setup")
                if not r.get("entry_type"): missing.append("entry_type")
                if not r.get("self_rating") or r["self_rating"] == 0: missing.append("rating")
                is_closed = r.get("e1_price") is not None
                if is_closed:
                    if not r.get("plan_followed"): missing.append("plan_followed")
                    if not r.get("exit_trigger"): missing.append("exit_trigger")
                    if not r.get("growth_areas"): missing.append("growth_areas")
                r["missing_fields"] = missing
                incomplete.append(r)

        return jsonify({
            "total": len(rows),
            "incomplete_count": len(incomplete),
            "incomplete": incomplete[:20],  # Top 20
        })
    except Exception as e:
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@journal_bp.route("/api/journal/trades/<int:trade_id>/quick-fill", methods=["PUT"])
@limiter.limit("30 per minute")
def journal_quick_fill(trade_id):
    """Quick-fill journal-specific fields (setup, rating, post-close reflections)."""
    data = request.get_json(force=True)
    conn = get_db()
    try:
        cur = conn.cursor()
        # Build dynamic SET clause from allowed fields only
        allowed = {"setup", "entry_type", "self_rating", "plan_followed", "exit_trigger", "growth_areas", "notes", "base_duration", "chart_image"}
        sets = []
        vals = []
        for key, val in data.items():
            if key in allowed:
                if key == "setup" and isinstance(val, list):
                    sets.append(f"{key} = %s")
                    vals.append(json.dumps(val))
                else:
                    sets.append(f"{key} = %s")
                    vals.append(val)

        if not sets:
            return jsonify({"error": "No valid fields provided"}), 400

        sets.append("updated_at = %s")
        vals.append(datetime.now())
        vals.extend([trade_id, g.user_id])

        cur.execute(f'''
            UPDATE journal_trades SET {", ".join(sets)}
            WHERE id = %s AND user_id = %s
            RETURNING id, symbol
        ''', vals)
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Trade not found"}), 404
        return jsonify({"updated": True, "id": row["id"], "symbol": row["symbol"]})
    except Exception as e:
        conn.rollback()
        print(f"[journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# GET /api/journal/daily-pnl — Daily realized P&L buckets
# ═══════════════════════════════════════════════════════════
@journal_bp.route("/api/journal/daily-pnl", methods=["GET"])
@limiter.limit("60 per minute")
def daily_pnl():
    """Realized P&L aggregated by exit date. Each exit (E-slot or
    pyramid leg exit) is bucketed on its own date — so a trade with
    multiple partial exits contributes to multiple days. Legacy FY
    tables are excluded because they have no per-day granularity.
    """
    fy = request.args.get("fy", "all")
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT j.id,
                   j.symbol,
                   j.trade_date,
                   j.e1_price, j.e1_qty, j.e1_date,
                   j.e2_price, j.e2_qty, j.e2_date,
                   j.e3_price, j.e3_qty, j.e3_date,
                   COALESCE(p.entry_price, j.entry_price) AS entry_price,
                   COALESCE(p.entry_price, j.avg_entry)   AS avg_entry,
                   COALESCE(p.quantity, j.initial_qty)    AS initial_qty,
                   p.sell_history                         AS sell_history,
                   p.pyramid_history                      AS pyramid_history
            FROM journal_trades j
            LEFT JOIN positions p ON p.id = j.position_id
            WHERE j.user_id = %s
        """, (g.user_id,))
        rows = cur.fetchall()

        buckets = {}

        def _add(dt, trade_id, symbol, pl):
            if not dt:
                return
            if isinstance(dt, (date, datetime)):
                dt_iso = dt.isoformat() if isinstance(dt, date) and not isinstance(dt, datetime) else dt.date().isoformat()
            else:
                dt_iso = str(dt)[:10]
            b = buckets.setdefault(dt_iso, {
                "date": dt_iso,
                "realized_pl": 0.0,
                "trade_ids": set(),
                "exit_count": 0,
                "wins": 0,
                "losses": 0,
                "symbols": set(),
            })
            b["realized_pl"] += pl
            b["trade_ids"].add(trade_id)
            b["exit_count"] += 1
            b["symbols"].add(symbol)
            if pl > 0:
                b["wins"] += 1
            elif pl < 0:
                b["losses"] += 1

        for r in rows:
            avg_entry = float(r.get("avg_entry") or r.get("entry_price") or 0)
            symbol = r.get("symbol") or ""
            trade_id = r.get("id")

            # Prefer positions.sell_history when present — same override
            # semantics as list_trades (_calc_fields sh_raw path).
            sh_raw = r.get("sell_history")
            if isinstance(sh_raw, str):
                try:
                    sh_raw = json.loads(sh_raw)
                except Exception:
                    sh_raw = None

            if isinstance(sh_raw, list) and sh_raw:
                for leg in sh_raw:
                    if not isinstance(leg, dict):
                        continue
                    qty = leg.get("shares") or leg.get("qty") or leg.get("quantity") or 0
                    price = leg.get("price") or 0
                    dt = leg.get("date")
                    if qty and price and avg_entry > 0:
                        pl = (float(price) - avg_entry) * int(qty)
                        _add(dt, trade_id, symbol, pl)
            else:
                # Orphan trade — read from E-slot columns
                for (px, qty, d) in [
                    (r.get("e1_price"), r.get("e1_qty"), r.get("e1_date")),
                    (r.get("e2_price"), r.get("e2_qty"), r.get("e2_date")),
                    (r.get("e3_price"), r.get("e3_qty"), r.get("e3_date")),
                ]:
                    if px and qty and avg_entry > 0:
                        pl = (float(px) - avg_entry) * int(qty)
                        _add(d, trade_id, symbol, pl)

            # Pyramid leg exits — profit is pre-computed on each exit.
            pyr_raw = r.get("pyramid_history") or []
            if isinstance(pyr_raw, str):
                try:
                    pyr_raw = json.loads(pyr_raw)
                except Exception:
                    pyr_raw = []
            for leg in pyr_raw:
                if not isinstance(leg, dict):
                    continue
                for ex in (leg.get("exits") or []):
                    if not isinstance(ex, dict):
                        continue
                    dt = ex.get("date")
                    profit = ex.get("profit")
                    if profit is None:
                        lp = float(leg.get("price") or 0)
                        ep = float(ex.get("price") or 0)
                        es = int(ex.get("shares") or 0)
                        profit = (ep - lp) * es if lp and ep and es else 0
                    if dt:
                        _add(dt, trade_id, symbol, float(profit or 0))

        days = []
        for _, b in sorted(buckets.items()):
            days.append({
                "date": b["date"],
                "realized_pl": round(b["realized_pl"], 2),
                "trade_count": len(b["trade_ids"]),
                "exit_count": b["exit_count"],
                "wins": b["wins"],
                "losses": b["losses"],
                "symbols": sorted(b["symbols"]),
            })

        # Indian FY filter (Apr→Mar)
        if fy and fy != "all":
            try:
                start_year = int(fy.split("-")[0])
                start = date(start_year, 4, 1).isoformat()
                end = date(start_year + 1, 3, 31).isoformat()
                days = [d for d in days if start <= d["date"] <= end]
            except (ValueError, IndexError):
                pass

        return jsonify({"days": days, "count": len(days), "fy": fy})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[journal] daily-pnl error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# EQUITY CURVE — running portfolio capital from realized events
# ═══════════════════════════════════════════════════════════
# Reads portfolio_capital_log (rows are appended on every position close /
# partial exit / pyramid unwind via services.portfolio_capital_log). Running
# capital = base_capital(fy) + Σ realized_pnl ordered by event_ts.
#
# Response shape:
#   {
#     "fy": "2026-27",
#     "base_capital": 50000000,
#     "current_capital": 51750000,
#     "realized_pnl_total": 1750000,
#     "series": [
#         {"date": "2026-04-01", "capital": 50000000, "cumulative_pnl": 0,
#          "day_pnl": 0, "event_count": 0},
#         {"date": "2026-04-15", "capital": 50250000, "cumulative_pnl": 250000,
#          "day_pnl": 250000, "event_count": 1},
#         ...
#     ],
#     "events": [
#         {"date": "2026-04-15", "ts": "...", "type": "final_close",
#          "stock": "FOO", "shares": 100, "price": 250.5, "pnl": 250000,
#          "trigger": "close_position", "capital_after": 50250000},
#         ...
#     ]
#   }
@journal_bp.route("/api/journal/equity-curve", methods=["GET"])
@limiter.limit("60 per minute")
def equity_curve():
    fy = request.args.get("fy")
    if not fy:
        try:
            from routes.settings_routes import _current_fy_label
            fy = _current_fy_label()
        except Exception:
            today = date.today()
            fy = f"{today.year}-{str(today.year + 1)[-2:]}" if today.month >= 4 else f"{today.year - 1}-{str(today.year)[-2:]}"

    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        from services.user_analytics_service import get_user_base_capital
        base = get_user_base_capital(cur, g.user_id, fy)
        if base is None:
            return jsonify({
                "fy": fy,
                "base_capital": None,
                "needs_setup": True,
                "series": [],
                "events": [],
            })
        base = float(base)

        cur.execute(
            """
            SELECT id, position_id, event_date, event_ts, event_type,
                   stock_name, shares, price, realized_pnl, trigger
            FROM portfolio_capital_log
            WHERE user_id = %s AND fy = %s
            ORDER BY event_ts ASC, id ASC
            """,
            (g.user_id, fy),
        )
        rows = cur.fetchall()

        # FY anchor — Apr 1 of starting year
        try:
            fy_start_year = int(fy.split("-")[0])
            fy_anchor = date(fy_start_year, 4, 1)
        except (ValueError, IndexError):
            fy_anchor = None

        running = base
        events = []
        day_buckets: dict[str, dict] = {}
        if fy_anchor is not None:
            day_buckets[fy_anchor.isoformat()] = {
                "date": fy_anchor.isoformat(),
                "day_pnl": 0.0,
                "event_count": 0,
            }

        for r in rows:
            pnl = float(r["realized_pnl"] or 0)
            running += pnl
            d_iso = r["event_date"].isoformat()
            ts = r["event_ts"]
            events.append({
                "id": r["id"],
                "position_id": r["position_id"],
                "date": d_iso,
                "ts": ts.isoformat() if ts else None,
                "type": r["event_type"],
                "stock": r["stock_name"],
                "shares": int(r["shares"]) if r["shares"] is not None else None,
                "price": float(r["price"]) if r["price"] is not None else None,
                "pnl": round(pnl, 2),
                "trigger": r["trigger"],
                "capital_after": round(running, 2),
            })
            bucket = day_buckets.setdefault(d_iso, {
                "date": d_iso,
                "day_pnl": 0.0,
                "event_count": 0,
            })
            bucket["day_pnl"] += pnl
            bucket["event_count"] += 1

        # Build the series with running capital per day
        series = []
        cum = 0.0
        for d_iso in sorted(day_buckets.keys()):
            b = day_buckets[d_iso]
            cum += b["day_pnl"]
            series.append({
                "date": d_iso,
                "capital": round(base + cum, 2),
                "cumulative_pnl": round(cum, 2),
                "day_pnl": round(b["day_pnl"], 2),
                "event_count": b["event_count"],
            })

        return jsonify({
            "fy": fy,
            "base_capital": base,
            "current_capital": round(running, 2),
            "realized_pnl_total": round(running - base, 2),
            "series": series,
            "events": events,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[journal] equity-curve error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ═══════════════════════════════════════════════════════════
# ONE-SHOT: backfill portfolio_capital_log from positions
# ═══════════════════════════════════════════════════════════
# Run once after the migration ships — rebuilds the entire log for the calling
# user from positions.sell_history + pyramid_history. Idempotent (deletes-then-
# inserts), so safe to re-run if anything looks off.
@journal_bp.route("/api/journal/equity-curve/backfill", methods=["POST"])
@limiter.limit("5 per minute")
def equity_curve_backfill():
    conn = _get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        from services import portfolio_capital_log as capital_log
        result = capital_log.backfill_user(cur, g.user_id)
        conn.commit()
        return jsonify({"backfilled": True, **result})
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        import traceback; traceback.print_exc()
        print(f"[journal] equity-curve backfill error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)
