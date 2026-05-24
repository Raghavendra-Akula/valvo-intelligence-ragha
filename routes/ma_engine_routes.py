"""
5MA Tracking Engine — Auto-calculates moving averages for all active positions
every 10 seconds during market hours.

Data source: candles_daily (Supabase, updated every ~10s via WebSocket VM)
Output: Updates positions table + saves to position_daily_updates

Rules (from Rohit's system):
- ONLY candle CLOSE matters — wicks irrelevant
- Close sitting ON the MA or barely below = tolerance, NOT shakeout
- Only clear, meaningful closes below count as shakeouts
- Safe: close >= 5MA
- Marginal: close is 0-1% below 5MA (1-day grace)
- Break: close is >1% below 5MA → EXIT signal
"""
from flask import Blueprint, jsonify, request, g
from extensions import limiter
from datetime import datetime, timedelta, timezone
import threading, os, json

ma_engine_bp = Blueprint("ma_engine", __name__)
IST = timezone(timedelta(hours=5, minutes=30))

def _get_db():
    from database.database import get_db
    return get_db()

def _close_db(conn):
    from database.database import close_db
    close_db(conn)


@ma_engine_bp.route("/api/positions/5ma-engine", methods=["POST"])
@limiter.limit("30 per minute")
def run_5ma_engine():
    """Calculate 5MA/10MA/20MA for ALL active positions using candles_daily.
    Updates positions table with latest values and saves daily snapshot."""
    conn = _get_db()
    try:
        cur = conn.cursor()

        # Get all active positions with security_id (scoped to current user)
        cur.execute("""
            SELECT id, stock_name, security_id, entry_price, risk_pct,
                   created_at, leg_base_price, valvo_ref_price,
                   grace_day_used, stop_loss, quantity, pyramid_history
            FROM positions WHERE status = 'active' AND user_id = %s AND security_id IS NOT NULL
        """, (g.user_id,))
        positions = cur.fetchall()

        if not positions:
            return jsonify({"ok": True, "updated": 0, "message": "No active positions"})

        results = []
        updated = 0

        # Bulk fetch MAs for ALL positions in ONE query (instead of N queries)
        all_sids = [str(pos["security_id"]) for pos in positions]
        cur.execute("""
            SELECT DISTINCT ON (security_id)
                security_id, date, close,
                AVG(close) OVER (PARTITION BY security_id ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
                AVG(close) OVER (PARTITION BY security_id ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as ma10,
                AVG(close) OVER (PARTITION BY security_id ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as ma20
            FROM candles_daily
            WHERE security_id = ANY(%s) AND volume > 0
            ORDER BY security_id, date DESC
        """, (all_sids,))
        ma_lookup = {}
        for row in cur.fetchall():
            ma_lookup[str(row["security_id"])] = row

        for pos in positions:
            sid = str(pos["security_id"])
            entry = float(pos["entry_price"] or 0)
            risk_pct = float(pos["risk_pct"] or 4)
            entry_date = pos.get("entry_date") or pos["created_at"]
            lb = float(pos["leg_base_price"] or 0) or entry
            vr = float(pos["valvo_ref_price"] or 0) or entry
            sl = float(pos["stop_loss"] or 0)
            qty = int(pos["quantity"] or 0)
            rps = entry * (risk_pct / 100.0) if risk_pct and entry else entry * 0.04

            latest = ma_lookup.get(sid)

            if not latest or not latest["close"] or latest["close"] <= 0:
                results.append({"id": pos["id"], "name": pos["stock_name"], "error": "no_candle_data"})
                continue

            cp = float(latest["close"])
            ma5 = round(float(latest["ma5"]), 2) if latest["ma5"] else None
            ma10 = round(float(latest["ma10"]), 2) if latest["ma10"] else None
            ma20 = round(float(latest["ma20"]), 2) if latest["ma20"] else None
            candle_date = str(latest["date"])

            # Calculate defensive status
            defensive = "safe"
            ma5_dist_pct = 0
            if ma5 and ma5 > 0:
                ma5_dist_pct = round(((cp - ma5) / ma5) * 100, 2)
                if ma5_dist_pct < -1.0:
                    defensive = "break"
                elif ma5_dist_pct < 0:
                    defensive = "marginal"

            # Extension calculations
            entry_ext = round(((cp - entry) / entry * 100), 2) if entry else 0
            leg_ext = round(((cp - lb) / lb * 100), 2) if lb else 0
            valvo_ext = round(((cp - vr) / vr * 100), 2) if vr else 0
            r_multiple = round(((cp - entry) / rps), 2) if rps else 0

            # P&L
            pnl = round((cp - entry) * qty, 2) if qty else 0
            pnl_pct = round(((cp - entry) / entry * 100), 2) if entry else 0

            # SL distance
            sl_dist_pct = round(((cp - sl) / cp * 100), 2) if sl and cp else 0

            # ═══ PER-LEG TRAILING MA UPDATE ═══
            # If any separate-mode pyramid leg is set to trail an MA, raise its
            # leg_sl to the current MA value (MA-based stops only ratchet UP —
            # never lower). Record the old/new values so the caller can see
            # what moved, and write back the mutated pyramid_history.
            pyr_raw = pos.get("pyramid_history") or []
            if isinstance(pyr_raw, str):
                try: pyr_raw = json.loads(pyr_raw)
                except Exception: pyr_raw = []
            pyr_changed = False
            pyr_leg_updates = []
            if isinstance(pyr_raw, list):
                ma_map = {"5ma": ma5, "10ma": ma10, "20ma": ma20}
                for i, leg in enumerate(pyr_raw):
                    if not isinstance(leg, dict):
                        continue
                    if leg.get("mode") != "separate":
                        continue
                    tag = leg.get("trailing_ma")
                    if not tag or tag not in ma_map:
                        continue
                    target = ma_map[tag]
                    if not target or target <= 0:
                        continue
                    current_leg_sl = leg.get("leg_sl")
                    try:
                        current_leg_sl_f = float(current_leg_sl) if current_leg_sl is not None else None
                    except (TypeError, ValueError):
                        current_leg_sl_f = None
                    new_leg_sl = round(float(target), 2)
                    if current_leg_sl_f is None or new_leg_sl > current_leg_sl_f:
                        pyr_raw[i]["leg_sl"] = new_leg_sl
                        pyr_changed = True
                        pyr_leg_updates.append({
                            "slot": leg.get("slot"), "trailing_ma": tag,
                            "old_sl": current_leg_sl_f, "new_sl": new_leg_sl,
                        })

            # Update positions table — cold fields only (extensions are
            # per-position derived metrics that only change when price changes,
            # but they live on positions because they're per-leg). The hot
            # columns (current_price, MAs, defensive_status, current_r_multiple)
            # moved to positions_live in phase-1 hot-cold split to keep the
            # 10s-cadence writes on a narrow sidecar row.
            if pyr_changed:
                cur.execute("""
                    UPDATE positions SET
                        entry_extension_pct = %s, leg_extension_pct = %s, valvo_extension_pct = %s,
                        pyramid_history = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (entry_ext, leg_ext, valvo_ext, json.dumps(pyr_raw), pos["id"]))
            else:
                cur.execute("""
                    UPDATE positions SET
                        entry_extension_pct = %s, leg_extension_pct = %s, valvo_extension_pct = %s,
                        updated_at = NOW()
                    WHERE id = %s
                """, (entry_ext, leg_ext, valvo_ext, pos["id"]))
            cur.execute("""
                UPDATE positions_live SET
                    current_price = %s,
                    last_5ma_price = %s, last_10ma_price = %s, last_20ma_price = %s,
                    defensive_status = %s,
                    current_r_multiple = %s,
                    updated_at = NOW()
                WHERE position_id = %s
            """, (cp, ma5, ma10, ma20, defensive, r_multiple, pos["id"]))

            # Save daily snapshot (upsert — one row per position per date)
            cur.execute("""
                INSERT INTO position_daily_updates
                    (user_id, position_id, date, close_price, five_ma_price, ten_ma_price, twenty_ma_price,
                     defensive_result, entry_extension_pct, leg_extension_pct, valvo_extension_pct, r_multiple)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (position_id, date) DO UPDATE SET
                    close_price = EXCLUDED.close_price,
                    five_ma_price = EXCLUDED.five_ma_price,
                    ten_ma_price = EXCLUDED.ten_ma_price,
                    twenty_ma_price = EXCLUDED.twenty_ma_price,
                    defensive_result = EXCLUDED.defensive_result,
                    entry_extension_pct = EXCLUDED.entry_extension_pct,
                    leg_extension_pct = EXCLUDED.leg_extension_pct,
                    valvo_extension_pct = EXCLUDED.valvo_extension_pct,
                    r_multiple = EXCLUDED.r_multiple
            """, (g.user_id, pos["id"], candle_date, cp, ma5, ma10, ma20, defensive,
                  entry_ext, leg_ext, valvo_ext, r_multiple))

            updated += 1
            results.append({
                "id": pos["id"],
                "name": pos["stock_name"],
                "cmp": cp,
                "ma5": ma5, "ma10": ma10, "ma20": ma20,
                "ma5_dist_pct": ma5_dist_pct,
                "defensive": defensive,
                "entry_ext": entry_ext,
                "r_multiple": r_multiple,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "sl_dist_pct": sl_dist_pct,
                "date": candle_date,
            })

        # ═══ SL/TSL BREACH DETECTION ═══
        # Check all active positions for SL or TSL breach (only notify once).
        # Also check each separate-mode pyramid leg's leg_sl — those don't
        # go through the main stop_loss column but are stops nonetheless.
        sl_breaches = []
        cur.execute("""
            SELECT id, stock_name, entry_price, current_price, stop_loss, trailing_sl,
                   sl_breach_notified, pyramid_history
            FROM positions
            WHERE status = 'active' AND user_id = %s
              AND sl_breach_notified = false
              AND current_price IS NOT NULL
        """, (g.user_id,))
        sl_positions = cur.fetchall()

        now = datetime.now(IST)
        for sp in sl_positions:
            cp = float(sp["current_price"] or 0)
            sl = float(sp["stop_loss"] or 0)
            tsl = float(sp["trailing_sl"] or 0)
            if cp <= 0:
                continue

            breach_type = None
            # TSL takes priority (it's the tighter stop)
            if tsl > 0 and cp <= tsl:
                breach_type = "tsl"
            elif sl > 0 and cp <= sl:
                breach_type = "sl"

            if breach_type:
                # Mark as notified to avoid spam
                cur.execute("""
                    UPDATE positions SET sl_breach_notified = true, sl_breach_notified_at = %s
                    WHERE id = %s
                """, (now, sp["id"]))

                breach_data = {
                    "id": sp["id"],
                    "stock_name": sp["stock_name"],
                    "entry_price": float(sp["entry_price"] or 0),
                    "current_price": cp,
                    "stop_loss": sl,
                    "trailing_sl": tsl,
                    "breach_type": breach_type,
                }
                sl_breaches.append(breach_data)

                # Create in-app notification
                sl_label = "Trailing SL" if breach_type == "tsl" else "Stop Loss"
                sl_price = tsl if breach_type == "tsl" else sl
                cur.execute("""
                    INSERT INTO notifications (user_id, type, title, body)
                    VALUES (%s, 'sl_breach', %s, %s)
                """, (g.user_id,
                      f"⚠️ {sp['stock_name']} hit {sl_label}!",
                      f"CMP: ₹{round(cp, 2)} | {sl_label}: ₹{round(sl_price, 2)} | Entry: ₹{round(float(sp['entry_price'] or 0), 2)}"))

            # ═══ PYRAMID-LEG BREACH DETECTION ═══
            # A separate-mode leg has its own leg_sl (custom ₹ or trailing MA).
            # Surface a breach when CMP <= leg_sl AND the leg still has shares
            # left. We don't touch sl_breach_notified here — that column guards
            # the main SL/TSL; per-leg breaches track their own notified flag
            # on the leg dict so each leg fires at most once.
            pyr = sp.get("pyramid_history") or []
            if isinstance(pyr, str):
                try: pyr = json.loads(pyr)
                except Exception: pyr = []
            pyr_mutated = False
            if isinstance(pyr, list):
                for i, leg in enumerate(pyr):
                    if not isinstance(leg, dict):
                        continue
                    if leg.get("mode") != "separate":
                        continue
                    leg_sl_raw = leg.get("leg_sl")
                    try:
                        leg_sl = float(leg_sl_raw) if leg_sl_raw is not None else None
                    except (TypeError, ValueError):
                        leg_sl = None
                    if leg_sl is None or leg_sl <= 0:
                        continue
                    leg_shares = int(leg.get("shares") or 0) - int(leg.get("exited_shares") or 0)
                    if leg_shares <= 0 or leg.get("leg_sl_breach_notified"):
                        continue
                    if cp <= leg_sl:
                        leg["leg_sl_breach_notified"] = True
                        leg["leg_sl_breach_at"] = now.isoformat()
                        pyr_mutated = True
                        slot_label = (leg.get("slot") or f"p{i+1}").upper()
                        trailing_tag = leg.get("trailing_ma")
                        stop_kind = f"{trailing_tag.upper()} trail" if trailing_tag else "custom SL"
                        sl_breaches.append({
                            "id": sp["id"],
                            "stock_name": sp["stock_name"],
                            "entry_price": float(sp["entry_price"] or 0),
                            "current_price": cp,
                            "stop_loss": leg_sl,
                            "trailing_sl": 0,
                            "breach_type": "pyramid_leg",
                            "pyramid_slot": slot_label,
                            "pyramid_shares": leg_shares,
                        })
                        cur.execute("""
                            INSERT INTO notifications (user_id, type, title, body)
                            VALUES (%s, 'sl_breach', %s, %s)
                        """, (g.user_id,
                              f"⚠️ {sp['stock_name']} {slot_label} ({leg_shares} sh) hit {stop_kind}",
                              f"CMP: ₹{round(cp, 2)} | Leg SL: ₹{round(leg_sl, 2)} | Buy: ₹{round(float(leg.get('price') or 0), 2)}"))
            if pyr_mutated:
                cur.execute("UPDATE positions SET pyramid_history = %s WHERE id = %s",
                            (json.dumps(pyr), sp["id"]))

        conn.commit()

        # Send SL breach email in background (skip if client says skip_email=true)
        skip_email = request.args.get("skip_email", "").lower() == "true"
        if sl_breaches and not skip_email:
            from routes.alerts_routes import send_sl_breach_email
            user_email = getattr(g, "user_email", None) or os.getenv("ALERT_EMAIL")
            if user_email:
                threading.Thread(target=send_sl_breach_email, args=(user_email, sl_breaches), daemon=True).start()

        return jsonify({
            "ok": True,
            "updated": updated,
            "total_active": len(positions),
            "positions": results,
            "sl_breaches": sl_breaches,
            "timestamp": datetime.utcnow().isoformat(),
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[ma_engine] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@ma_engine_bp.route("/api/positions/check-sl-breaches", methods=["GET"])
@limiter.limit("60 per minute")
def check_sl_breaches():
    """Check for any SL/TSL breaches that haven't been acknowledged by the frontend yet.
    Called by AppLayout polling (alongside price alert checks)."""
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, stock_name, entry_price, current_price, stop_loss, trailing_sl,
                   sl_breach_notified_at
            FROM positions
            WHERE status = 'active' AND user_id = %s
              AND sl_breach_notified = true
              AND sl_breach_notified_at > NOW() - INTERVAL '5 minutes'
        """, (g.user_id,))
        recent_breaches = []
        for sp in cur.fetchall():
            tsl = float(sp["trailing_sl"] or 0)
            sl = float(sp["stop_loss"] or 0)
            cp = float(sp["current_price"] or 0)
            breach_type = "tsl" if tsl > 0 and cp <= tsl else "sl"
            recent_breaches.append({
                "id": sp["id"],
                "stock_name": sp["stock_name"],
                "entry_price": float(sp["entry_price"] or 0),
                "current_price": cp,
                "stop_loss": sl,
                "trailing_sl": tsl,
                "breach_type": breach_type,
                "notified_at": str(sp["sl_breach_notified_at"]),
            })
        return jsonify({"breaches": recent_breaches, "count": len(recent_breaches)})
    except Exception as e:
        print(f"[ma_engine] error: {e}")
        return jsonify({"error": "Internal error", "breaches": []}), 500
    finally:
        _close_db(conn)


@ma_engine_bp.route("/api/positions/5ma-status", methods=["GET"])
@limiter.limit("60 per minute")
def get_5ma_status():
    """Quick read of current 5MA status for all active positions (no recalc)."""
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, stock_name, security_id, entry_price, current_price,
                   last_5ma_price, last_10ma_price, last_20ma_price,
                   defensive_status, entry_extension_pct, current_r_multiple,
                   stop_loss, quantity, bucket_sold_pct, trail_remaining_pct,
                   updated_at
            FROM positions WHERE status = 'active' AND user_id = %s
            ORDER BY current_r_multiple DESC NULLS LAST
        """, (g.user_id,))
        positions = cur.fetchall()

        result = []
        for p in positions:
            cp = float(p["current_price"] or 0)
            ma5 = float(p["last_5ma_price"] or 0)
            entry = float(p["entry_price"] or 0)
            sl = float(p["stop_loss"] or 0)
            qty = int(p["quantity"] or 0)

            ma5_dist = round(((cp - ma5) / ma5 * 100), 2) if ma5 > 0 else 0
            pnl = round((cp - entry) * qty) if entry and qty else 0

            result.append({
                "id": p["id"],
                "name": p["stock_name"],
                "cmp": cp,
                "entry": entry,
                "ma5": ma5,
                "ma10": float(p["last_10ma_price"] or 0),
                "ma20": float(p["last_20ma_price"] or 0),
                "ma5_dist_pct": ma5_dist,
                "defensive": p["defensive_status"] or "unknown",
                "ext_pct": float(p["entry_extension_pct"] or 0),
                "r_mult": float(p["current_r_multiple"] or 0),
                "sl": sl,
                "sl_dist_pct": round(((cp - sl) / cp * 100), 2) if sl and cp else 0,
                "pnl": pnl,
                "pnl_pct": round(((cp - entry) / entry * 100), 2) if entry else 0,
                "bucket_sold": float(p["bucket_sold_pct"] or 0),
                "trail_remaining": float(p["trail_remaining_pct"] or 0),
                "updated": p["updated_at"].isoformat() if p["updated_at"] else None,
            })

        return jsonify({"positions": result, "count": len(result)})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[ma_engine] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)
