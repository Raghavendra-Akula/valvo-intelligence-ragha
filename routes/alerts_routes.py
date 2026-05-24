"""
alerts_routes.py — Price Alerts + Notifications API
CRUD alerts, auto-check via pg_cron, in-app notifications, email on trigger
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from datetime import datetime, timezone, timedelta
from database.database import get_db, close_db
import os, threading

alerts_bp = Blueprint("alerts", __name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _send_valvo_email(user_email, subject, html_body):
    """Send an email via SMTP (runs in background thread). Shared by price alerts + SL/TSL."""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        smtp_email = os.getenv("SMTP_EMAIL")
        smtp_password = os.getenv("SMTP_PASSWORD")
        if not smtp_email or not smtp_password or not user_email:
            return
        msg = MIMEMultipart("alternative")
        msg["From"] = smtp_email
        msg["To"] = user_email
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", 587))) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
            server.send_message(msg)
        print(f"📧 Email sent to {user_email}: {subject}")
    except Exception as e:
        print(f"❌ Email failed: {e}")


def _send_alert_email(user_email, alerts_list):
    """Send email notification for triggered price alerts."""
    subject = f"Valvo Alert: {', '.join(a['symbol'] for a in alerts_list)}"
    lines = ["<h2 style='color:#0A84FF;font-family:sans-serif'>Valvo Price Alerts Triggered</h2>"]
    for a in alerts_list:
        direction = "crossed above" if "above" in a["condition"] else "crossed below"
        color = "#30D158" if "above" in a["condition"] else "#FF453A"
        lines.append(f"<div style='padding:12px;margin:8px 0;border-radius:8px;border:1px solid #333;font-family:sans-serif'>")
        lines.append(f"<strong style='font-size:16px'>{a['symbol']}</strong> {direction} "
                     f"<strong style='color:{color}'>₹{a['threshold']}</strong><br>")
        lines.append(f"<span style='color:#888'>Current: ₹{a['current_price']}</span>")
        if a.get("notes"):
            lines.append(f"<br><span style='color:#666;font-size:12px'>{a['notes']}</span>")
        lines.append("</div>")
    lines.append("<p style='color:#666;font-size:11px;font-family:sans-serif'>— Valvo Intelligence</p>")
    _send_valvo_email(user_email, subject, "\n".join(lines))


def send_sl_breach_email(user_email, breaches):
    """Send urgent email for SL/TSL breaches on open positions."""
    subject = f"⚠️ Valvo SL Breach: {', '.join(b['stock_name'] for b in breaches)}"
    lines = ["<h2 style='color:#FF453A;font-family:sans-serif'>⚠️ Stop Loss Breach Alert</h2>"]
    for b in breaches:
        sl_type = "Trailing SL" if b["breach_type"] == "tsl" else "Stop Loss"
        sl_price = b.get("trailing_sl") if b["breach_type"] == "tsl" else b.get("stop_loss")
        lines.append(f"<div style='padding:12px;margin:8px 0;border-radius:8px;border:1px solid #FF453A;font-family:sans-serif;background:rgba(255,69,58,0.05)'>")
        lines.append(f"<strong style='font-size:16px;color:#FF453A'>{b['stock_name']}</strong> hit {sl_type}<br>")
        lines.append(f"<span style='color:#888'>CMP: ₹{round(b['current_price'], 2)} | {sl_type}: ₹{round(sl_price, 2)} | Entry: ₹{round(b['entry_price'], 2)}</span>")
        lines.append("</div>")
    lines.append("<p style='color:#FF453A;font-weight:bold;font-size:12px;font-family:sans-serif'>Action required — review your positions immediately.</p>")
    lines.append("<p style='color:#666;font-size:11px;font-family:sans-serif'>— Valvo Intelligence</p>")
    _send_valvo_email(user_email, subject, "\n".join(lines))


@alerts_bp.route("/api/alerts", methods=["GET"])
@limiter.limit("60 per minute")
def list_alerts():
    """List all alerts for the user (active first, then triggered)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM price_alerts
            WHERE user_id = %s
            ORDER BY active DESC, created_at DESC
        """, (g.user_id,))
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("created_at"): r["created_at"] = str(r["created_at"])
            if r.get("triggered_at"): r["triggered_at"] = str(r["triggered_at"])
        return jsonify({"alerts": rows, "total": len(rows)})
    except Exception as e:
        print(f"[alerts] error: {e}")
        return jsonify({"error": "Internal error", "alerts": []}), 500
    finally:
        close_db(conn)


@alerts_bp.route("/api/alerts", methods=["POST"])
@limiter.limit("30 per minute")
def create_alert():
    """Create a new price alert."""
    data = request.get_json(force=True)
    symbol = (data.get("symbol") or "").strip().upper()
    condition = data.get("condition", "")
    threshold = data.get("threshold")

    if not symbol or not condition or threshold is None:
        return jsonify({"error": "symbol, condition, and threshold required"}), 400

    valid_conditions = {"crosses_above", "crosses_below", "change_pct_above", "change_pct_below"}
    if condition not in valid_conditions:
        return jsonify({"error": f"Invalid condition. Use: {', '.join(valid_conditions)}"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO price_alerts (user_id, symbol, security_id, condition, threshold, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (g.user_id, symbol, data.get("security_id"), condition, float(threshold), data.get("notes")))
        row = cur.fetchone()
        conn.commit()
        return jsonify({"created": True, "id": row["id"], "symbol": symbol, "condition": condition, "threshold": threshold}), 201
    except Exception as e:
        conn.rollback()
        print(f"[alerts] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@alerts_bp.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
@limiter.limit("15 per minute")
def delete_alert(alert_id):
    """Delete an alert."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM price_alerts WHERE id = %s AND user_id = %s RETURNING id", (alert_id, g.user_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Alert not found"}), 404
        return jsonify({"deleted": True, "id": alert_id})
    except Exception as e:
        conn.rollback()
        print(f"[alerts] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@alerts_bp.route("/api/alerts/check", methods=["GET"])
@limiter.limit("60 per minute")
def check_alerts():
    """Evaluate all active alerts against current prices. Returns newly triggered alerts."""
    conn = get_db()
    try:
        cur = conn.cursor()

        # Step 1: Get all active alerts
        cur.execute("SELECT * FROM price_alerts WHERE user_id = %s AND active = true", (g.user_id,))
        alerts = [dict(r) for r in cur.fetchall()]
        if not alerts:
            return jsonify({"checked": 0, "newly_triggered": [], "triggered_count": 0})

        # Step 2: Get current prices for all alert security_ids.
        # Reads from stock_daily_summary (precomputed, indexed) instead of
        # DISTINCT ON over candles_daily (6.56M rows) so the 60s alert
        # poll stops competing with watchlist/screener traffic for the
        # 5-conn DB pool. live_close is kept fresh every ~10s during
        # market hours by the websocket worker.
        sids = list(set(a["security_id"] for a in alerts if a.get("security_id")))
        prices = {}
        if sids:
            cur.execute("""
                SELECT security_id, COALESCE(live_close, prev_close) AS px
                FROM stock_daily_summary
                WHERE security_id = ANY(%s)
                  AND COALESCE(live_close, prev_close) > 0
            """, (sids,))
            for r in cur.fetchall():
                prices[r["security_id"]] = float(r["px"])

        # Step 3: Evaluate each alert
        newly_triggered = []
        now = datetime.now(IST)

        for a in alerts:
            price = prices.get(a["security_id"])
            if not price:
                continue

            prev = float(a["last_price"]) if a.get("last_price") else None
            threshold = float(a["threshold"])
            condition = a["condition"]
            triggered = False

            if condition == "crosses_above":
                triggered = price >= threshold and (prev is None or prev < threshold)
            elif condition == "crosses_below":
                triggered = price <= threshold and (prev is None or prev > threshold)

            # Update last_price. The alerts loop iterates the user-scoped
            # SELECT above, so id is pre-filtered, but belt-and-suspenders:
            # also gate by user_id so a future refactor can't leak writes.
            cur.execute(
                "UPDATE price_alerts SET last_price = %s WHERE id = %s AND user_id = %s",
                (price, a["id"], g.user_id),
            )

            if triggered:
                cur.execute("""
                    UPDATE price_alerts SET triggered = true, triggered_at = %s, active = false,
                        last_notified_at = %s, trigger_count = COALESCE(trigger_count, 0) + 1
                    WHERE id = %s AND user_id = %s
                """, (now, now, a["id"], g.user_id))
                alert_data = {
                    "id": a["id"],
                    "symbol": a["symbol"],
                    "condition": condition,
                    "threshold": threshold,
                    "current_price": price,
                    "triggered_at": str(now),
                    "notes": a.get("notes"),
                }
                newly_triggered.append(alert_data)
                # Create in-app notification
                direction = "crossed above" if "above" in condition else "crossed below"
                cur.execute("""
                    INSERT INTO notifications (user_id, alert_id, type, title, body)
                    VALUES (%s, %s, 'alert_triggered', %s, %s)
                """, (g.user_id, a["id"],
                      f"{a['symbol']} {direction} ₹{threshold}",
                      f"{a['symbol']} is now at ₹{round(price, 2)}" + (f" — {a['notes']}" if a.get("notes") else "")))

        conn.commit()

        # Send email in background if any alerts triggered (skip if client says skip_email=true)
        skip_email = request.args.get("skip_email", "").lower() == "true"
        if newly_triggered and not skip_email:
            user_email = getattr(g, "user_email", None) or os.getenv("ALERT_EMAIL")
            if user_email:
                threading.Thread(target=_send_alert_email, args=(user_email, newly_triggered), daemon=True).start()

        return jsonify({
            "checked": len(alerts),
            "newly_triggered": newly_triggered,
            "triggered_count": len(newly_triggered),
        })
    except Exception as e:
        conn.rollback()
        print(f"[alerts] error: {e}")
        return jsonify({"error": "Internal error", "newly_triggered": []}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# NOTIFICATIONS — In-app notification bell
# ═══════════════════════════════════════════════════

@alerts_bp.route("/api/notifications", methods=["GET"])
@limiter.limit("60 per minute")
def list_notifications():
    """Get recent notifications (unread first, then recent read)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        limit = request.args.get("limit", 20, type=int)
        cur.execute("""
            SELECT * FROM notifications
            WHERE user_id = %s
            ORDER BY read ASC, created_at DESC
            LIMIT %s
        """, (g.user_id, limit))
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("created_at"): r["created_at"] = str(r["created_at"])
        return jsonify({"notifications": rows, "total": len(rows)})
    except Exception as e:
        print(f"[alerts] error: {e}")
        return jsonify({"error": "Internal error", "notifications": []}), 500
    finally:
        close_db(conn)


@alerts_bp.route("/api/notifications/unread-count", methods=["GET"])
@limiter.limit("60 per minute")
def unread_count():
    """Get count of unread notifications (for badge)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as count FROM notifications WHERE user_id = %s AND read = false", (g.user_id,))
        row = cur.fetchone()
        return jsonify({"count": row["count"] if row else 0})
    except Exception as e:
        print(f"[alerts] error: {e}")
        return jsonify({"error": "Internal error", "count": 0}), 500
    finally:
        close_db(conn)


@alerts_bp.route("/api/notifications/mark-read", methods=["POST"])
@limiter.limit("30 per minute")
def mark_read():
    """Mark notifications as read. Body: { ids: [1,2,3] } or { all: true }."""
    data = request.get_json(force=True) or {}
    conn = get_db()
    try:
        cur = conn.cursor()
        if data.get("all"):
            cur.execute("UPDATE notifications SET read = true WHERE user_id = %s AND read = false", (g.user_id,))
        else:
            ids = data.get("ids", [])
            if ids:
                cur.execute("UPDATE notifications SET read = true WHERE user_id = %s AND id = ANY(%s)", (g.user_id, ids))
        conn.commit()
        return jsonify({"marked": True})
    except Exception as e:
        conn.rollback()
        print(f"[alerts] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@alerts_bp.route("/api/alerts/<int:alert_id>/rearm", methods=["POST"])
@limiter.limit("30 per minute")
def rearm_alert(alert_id):
    """Re-arm a triggered alert (make it active again)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE price_alerts SET active = true, triggered = false, triggered_at = NULL
            WHERE id = %s AND user_id = %s RETURNING id
        """, (alert_id, g.user_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Alert not found"}), 404
        return jsonify({"rearmed": True, "id": alert_id})
    except Exception as e:
        conn.rollback()
        print(f"[alerts] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)
