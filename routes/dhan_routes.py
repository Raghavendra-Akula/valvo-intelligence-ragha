"""
Dhan integration routes — admin only.

Consent flow:
  POST /api/dhan/consent/start      → returns {login_url} for the user to visit
  GET  /api/dhan/consent/callback   → Dhan redirects here with ?tokenId=...
  POST /api/dhan/disconnect          → drop stored token

Status:
  GET  /api/dhan/status              → {connected, dhan_client_id, expires_at}

Trading & portfolio (all admin-gated, all require completed consent flow):
  POST   /api/dhan/orders                place new order              (via VM proxy)
  PUT    /api/dhan/orders/<id>           modify pending order         (via VM proxy)
  DELETE /api/dhan/orders/<id>           cancel pending order         (via VM proxy)
  GET    /api/dhan/orders                order book
  GET    /api/dhan/orders/<id>           order status
  GET    /api/dhan/trades                trade book

  GET    /api/dhan/holdings              portfolio holdings (T1 + delivered)
  GET    /api/dhan/positions             open intraday + F&O carryforward positions
  GET    /api/dhan/funds                 fund limits
"""
from __future__ import annotations

import os

from flask import Blueprint, g, jsonify, redirect, request

from extensions import limiter
from services.admin_service import is_admin
from services import dhan_service, dhan_sync, dhan_sl_service

dhan_bp = Blueprint("dhan", __name__)


# Only this email may use the Dhan integration. Set via env so we don't hardcode.
# Empty value = fall back to admin-only (any admin allowed).
DHAN_TRADER_EMAIL = os.getenv("DHAN_TRADER_EMAIL", "").strip().lower()

# Shared secret for the pg_cron poller. If unset, the endpoint stays open
# (matching the existing /api/project-hub/run-reminders convention).
DHAN_SYNC_CRON_SECRET = os.getenv("DHAN_SYNC_CRON_SECRET", "").strip()


# ── Helpers ────────────────────────────────────────────────────────────────


def _require_admin():
    user_id = getattr(g, "user_id", None)
    if not user_id or not is_admin(user_id):
        return jsonify({"error": "Admin only"}), 403
    if DHAN_TRADER_EMAIL:
        user_email = (getattr(g, "user_email", "") or "").strip().lower()
        if user_email != DHAN_TRADER_EMAIL:
            return jsonify({"error": "Dhan trading is restricted to a different account"}), 403
    return None


def _safe_call(*args, **kwargs):
    """Wrap dhan_service.call to translate exceptions into HTTP responses."""
    try:
        return jsonify(dhan_service.call(*args, **kwargs) or {}), 200
    except dhan_service.DhanNotConnected as exc:
        return jsonify({"error": "Dhan not connected", "detail": str(exc)}), 412
    except dhan_service.DhanApiError as exc:
        return jsonify({"error": "Dhan API error", "status": exc.status, "detail": exc.body}), 502
    except Exception as exc:
        return jsonify({"error": "Internal error", "detail": str(exc)}), 500


# ── Consent flow ──────────────────────────────────────────────────────────────


@dhan_bp.route("/api/dhan/consent/start", methods=["POST"])
@limiter.limit("10 per hour")
def consent_start():
    err = _require_admin()
    if err:
        return err
    try:
        result = dhan_service.generate_consent(g.user_id)
        return jsonify(result), 200
    except Exception as exc:
        return jsonify({"error": "Consent generation failed", "detail": str(exc)}), 500


@dhan_bp.route("/api/dhan/consent/callback", methods=["POST"])
@limiter.limit("60 per hour")
def consent_callback():
    """
    Called by the frontend's Dhan-callback page after Dhan redirects the user
    back with ?tokenId=... in the URL. Frontend POSTs that tokenId here with
    the Supabase JWT so we know which admin user this consent belongs to.
    """
    err = _require_admin()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    token_id = body.get("tokenId") or body.get("token_id") or request.args.get("tokenId")
    consent_id_hint = (
        body.get("consentId") or body.get("consent_id")
        or request.args.get("consentId") or request.args.get("consent_id")
    )
    if not token_id:
        return jsonify({"error": "tokenId missing in callback"}), 400
    try:
        result = dhan_service.consume_consent(token_id, consent_id_hint=consent_id_hint)
        if result["user_id"] != g.user_id:
            return jsonify({"error": "Consent belongs to a different user"}), 403
        return jsonify({"connected": True, **result}), 200
    except Exception as exc:
        return jsonify({"error": "Consent consume failed", "detail": str(exc)}), 500


@dhan_bp.route("/api/dhan/disconnect", methods=["POST"])
def disconnect():
    err = _require_admin()
    if err:
        return err
    removed = dhan_service.disconnect(g.user_id)
    return jsonify({"disconnected": bool(removed)}), 200


@dhan_bp.route("/api/dhan/status", methods=["GET"])
def status():
    err = _require_admin()
    if err:
        return err
    tok = dhan_service.get_token(g.user_id)
    if not tok:
        return jsonify({"connected": False}), 200
    return jsonify({
        "connected": True,
        "dhan_client_id": tok["dhan_client_id"],
        "expires_at": tok["expires_at"].isoformat() if tok.get("expires_at") else None,
    }), 200


# ── Symbol resolver (cheap, public-ish read of stock_universe) ───────────────


@dhan_bp.route("/api/dhan/resolve", methods=["GET"])
def resolve():
    """GET /api/dhan/resolve?symbol=RELIANCE&exchange=NSE → {security_id, ...}"""
    err = _require_admin()
    if err:
        return err
    symbol = (request.args.get("symbol") or "").strip()
    exchange = (request.args.get("exchange") or "NSE").strip()
    if not symbol:
        return jsonify({"error": "symbol query param required"}), 400
    resolved = dhan_service.resolve_symbol(symbol, exchange)
    if not resolved:
        return jsonify({"error": f"No active security_id for {symbol} on {exchange}"}), 404
    return jsonify(resolved), 200


# ── Orders (write paths go via VM proxy; reads hit Dhan directly) ─────────────


@dhan_bp.route("/api/dhan/orders", methods=["POST"])
@limiter.limit("60 per minute")
def place_order():
    """
    Two payload styles, picked by whether `symbol` is present:

      Valvo-friendly (recommended for the UI):
        { symbol, exchange, side, quantity, order_type?, product?, price?,
          trigger_price?, validity?, disclosed_quantity?, after_market? }

      Raw Dhan body (for power users / direct API):
        { dhanClientId, securityId, transactionType, exchangeSegment, ... }
    """
    err = _require_admin()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    if body.get("symbol"):
        try:
            result = dhan_sync.place_order_with_position(
                g.user_id,
                symbol=body.get("symbol"),
                exchange=body.get("exchange", "NSE"),
                side=body.get("side"),
                quantity=body.get("quantity"),
                order_type=body.get("order_type", "MARKET"),
                product=body.get("product", "CNC"),
                price=body.get("price"),
                trigger_price=body.get("trigger_price"),
                stop_loss=body.get("stop_loss"),
                validity=body.get("validity", "DAY"),
                disclosed_quantity=body.get("disclosed_quantity", 0),
                after_market=body.get("after_market", False),
                amo_time=body.get("amo_time", "OPEN"),
            )
            # Flatten the Dhan response so existing UI code keeps working,
            # while exposing position_id for the trade card.
            dhan_payload = result.get("dhan") or {}
            response = dict(dhan_payload) if isinstance(dhan_payload, dict) else {}
            response["position_id"] = result.get("position_id")
            if result.get("rejected"):
                response["rejected"] = True
            return jsonify(response), 200
        except dhan_service.DhanNotConnected as exc:
            return jsonify({"error": "Dhan not connected", "detail": str(exc)}), 412
        except dhan_service.DhanApiError as exc:
            return jsonify({"error": "Dhan API error", "status": exc.status, "detail": exc.body}), 502
        except ValueError as exc:
            return jsonify({"error": "Invalid order", "detail": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "Internal error", "detail": str(exc)}), 500
    return _safe_call(g.user_id, "POST", "/orders", json=body, via_proxy=True)


@dhan_bp.route("/api/dhan/orders/<order_id>", methods=["PUT"])
@limiter.limit("60 per minute")
def modify_order(order_id):
    err = _require_admin()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    return _safe_call(g.user_id, "PUT", f"/orders/{order_id}", json=body, via_proxy=True)


@dhan_bp.route("/api/dhan/orders/<order_id>", methods=["DELETE"])
@limiter.limit("60 per minute")
def cancel_order(order_id):
    err = _require_admin()
    if err:
        return err
    return _safe_call(g.user_id, "DELETE", f"/orders/{order_id}", via_proxy=True)


@dhan_bp.route("/api/dhan/orders", methods=["GET"])
def list_orders():
    err = _require_admin()
    if err:
        return err
    return _safe_call(g.user_id, "GET", "/orders")


@dhan_bp.route("/api/dhan/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    err = _require_admin()
    if err:
        return err
    return _safe_call(g.user_id, "GET", f"/orders/{order_id}")


@dhan_bp.route("/api/dhan/trades", methods=["GET"])
def list_trades():
    err = _require_admin()
    if err:
        return err
    return _safe_call(g.user_id, "GET", "/trades")


# ── Portfolio / funds (read-only, no proxy needed) ───────────────────────


@dhan_bp.route("/api/dhan/holdings", methods=["GET"])
def holdings():
    err = _require_admin()
    if err:
        return err
    return _safe_call(g.user_id, "GET", "/holdings")


@dhan_bp.route("/api/dhan/positions", methods=["GET"])
def positions():
    err = _require_admin()
    if err:
        return err
    return _safe_call(g.user_id, "GET", "/positions")


@dhan_bp.route("/api/dhan/funds", methods=["GET"])
def funds():
    err = _require_admin()
    if err:
        return err
    return _safe_call(g.user_id, "GET", "/fundlimit")


# ── Trade-fill auto-sync (broker fills → positions / journal_trades) ─────────


@dhan_bp.route("/api/dhan/sync-fills", methods=["POST"])
@limiter.exempt
def sync_fills_cron():
    """
    Batch sync for every connected Dhan user. Hit by pg_cron every 5 min during
    market hours. Validates X-Cron-Secret if DHAN_SYNC_CRON_SECRET is set.
    """
    if DHAN_SYNC_CRON_SECRET:
        provided = request.headers.get("X-Cron-Secret", "").strip()
        if provided != DHAN_SYNC_CRON_SECRET:
            return jsonify({"error": "unauthorized"}), 401

    from database.database import get_db, close_db
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM dhan_tokens WHERE expires_at > NOW()")
        user_ids = [str(row["user_id"]) for row in cur.fetchall()]
    finally:
        close_db(conn)

    summaries = []
    for uid in user_ids:
        try:
            summaries.append({"user_id": uid, **dhan_sync.sync_user_fills(uid)})
        except Exception as exc:
            summaries.append({"user_id": uid, "error": str(exc)})

    total_new = sum(s.get("new") or 0 for s in summaries)
    return jsonify({"users": len(user_ids), "total_new": total_new, "summaries": summaries}), 200


@dhan_bp.route("/api/dhan/sync-mine", methods=["POST"])
def sync_mine():
    """Manual sync trigger for the connected admin user."""
    err = _require_admin()
    if err:
        return err
    return jsonify(dhan_sync.sync_user_fills(g.user_id)), 200


# ── Broker-side SL: morning rollover + manual sync ──────────────────────────


@dhan_bp.route("/api/dhan/place-morning-sl-orders", methods=["POST"])
@limiter.exempt
def place_morning_sl_orders_cron():
    """
    Hit by pg_cron at 09:14 IST Mon–Fri. For each connected user, places a
    fresh DAY STOP_LOSS_MARKET order for every active broker position so the
    broker actually exits if price hits stop_loss during the day. Yesterday's
    DAY orders have already auto-expired; we wipe their stale order IDs and
    let dhan_sl_service.sync_sl() place new ones.
    """
    if DHAN_SYNC_CRON_SECRET:
        provided = request.headers.get("X-Cron-Secret", "").strip()
        if provided != DHAN_SYNC_CRON_SECRET:
            return jsonify({"error": "unauthorized"}), 401
    return jsonify(dhan_sl_service.place_morning_sl_orders()), 200


@dhan_bp.route("/api/dhan/sync-sl-mine", methods=["POST"])
def sync_sl_mine():
    """Manual broker-SL reconciliation for the connected admin user."""
    err = _require_admin()
    if err:
        return err
    return jsonify(dhan_sl_service.reconcile_user_sl(g.user_id)), 200
