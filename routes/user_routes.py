"""
User Routes — Profile management, FY base capital configuration, FY list.
Auth handled globally by app.before_request middleware (g.user_id always available).
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from database.database import get_db, close_db
from services.admin_service import fetch_pricing_payload

user_bp = Blueprint("user", __name__)


@user_bp.route("/api/user/profile", methods=["GET"])
@limiter.limit("60 per minute")
def get_profile():
    """Get current user's profile (role, display_name)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, display_name, created_at FROM user_profiles WHERE user_id = %s",
            (g.user_id,)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"role": "user", "display_name": None, "needs_setup": True})
        return jsonify({
            "role": row["role"],
            "display_name": row["display_name"],
            "needs_setup": False,
        })
    except Exception as e:
        print(f"[user] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@user_bp.route("/api/user/profile", methods=["PUT"])
@limiter.limit("30 per minute")
def update_profile():
    """Update display_name. Role cannot be self-assigned."""
    data = request.get_json() or {}
    display_name = data.get("display_name", "").strip()
    if not display_name:
        return jsonify({"error": "display_name required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_profiles (user_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name
            RETURNING role, display_name
        """, (g.user_id, display_name))
        conn.commit()
        row = cur.fetchone()
        return jsonify({"role": row["role"], "display_name": row["display_name"]})
    except Exception as e:
        conn.rollback()
        print(f"[user] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@user_bp.route("/api/user/fy-list", methods=["GET"])
@limiter.limit("60 per minute")
def fy_list():
    """Returns list of FYs available to the current user."""
    conn = get_db()
    try:
        cur = conn.cursor()
        from services.user_analytics_service import get_user_fy_list, get_user_role
        fys = get_user_fy_list(cur, g.user_id)
        role = get_user_role(cur, g.user_id)

        # Also return base capitals for each FY
        cur.execute(
            "SELECT fy, base_capital FROM user_fy_config WHERE user_id = %s ORDER BY fy",
            (g.user_id,)
        )
        fy_capitals = {r["fy"]: float(r["base_capital"]) for r in cur.fetchall()}

        return jsonify({
            "fys": fys,
            "role": role,
            "fy_capitals": fy_capitals,
            "needs_setup": len(fys) == 0,
        })
    except Exception as e:
        print(f"[user] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@user_bp.route("/api/user/fy-config", methods=["POST"])
@limiter.limit("30 per minute")
def set_fy_config():
    """Set base capital for a specific FY. Creates or updates."""
    data = request.get_json() or {}
    fy = (data.get("fy") or "").strip()
    base_capital = data.get("base_capital")

    if not fy:
        return jsonify({"error": "fy required (e.g. '2026-27')"}), 400
    if base_capital is None or float(base_capital) <= 0:
        return jsonify({"error": "base_capital must be a positive number"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()

        # Ensure user has a profile row
        cur.execute("""
            INSERT INTO user_profiles (user_id) VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
        """, (g.user_id,))

        cur.execute("""
            INSERT INTO user_fy_config (user_id, fy, base_capital)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, fy) DO UPDATE SET base_capital = EXCLUDED.base_capital
            RETURNING fy, base_capital
        """, (g.user_id, fy, float(base_capital)))
        conn.commit()
        row = cur.fetchone()
        return jsonify({
            "fy": row["fy"],
            "base_capital": float(row["base_capital"]),
            "message": f"Base capital for FY {fy} set to ₹{float(base_capital):,.0f}",
        })
    except Exception as e:
        conn.rollback()
        print(f"[user] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@user_bp.route("/api/user/subscription", methods=["GET"])
@limiter.limit("60 per minute")
def get_subscription():
    """Return current user's subscription info."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE user_subscriptions
            SET plan = 'free',
                status = 'expired',
                billing_cycle = 'monthly',
                amount = 0,
                updated_at = NOW()
            WHERE user_id = %s
              AND status = 'trial'
              AND end_date IS NOT NULL
              AND end_date < NOW()
        """, (g.user_id,))
        conn.commit()
        cur.execute("""
            SELECT plan, status, billing_cycle, amount, start_date, end_date, payment_ref, trial_used_at
            FROM user_subscriptions WHERE user_id = %s
        """, (g.user_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"plan": "free", "status": "active"})
        return jsonify(dict(row))
    except Exception:
        return jsonify({"plan": "free", "status": "active"})
    finally:
        close_db(conn)


@user_bp.route("/api/pricing", methods=["GET"])
@limiter.limit("60 per minute")
def get_pricing():
    """Return active pricing plans (public, for the pricing page).

    The endpoint is in AUTH_EXEMPT_ENDPOINTS so it can render before the user
    finishes onboarding — but if a Bearer token IS present we still want to
    show the right trial-eligibility for that signed-in user. We decode the
    JWT opportunistically here; failures fall back to user_id=None.

    Plans + paywall + trial-eligibility are fetched via fetch_pricing_payload
    which (a) serves plans/paywall from a 60s in-process cache so warm
    workers do zero DB work on the hot path, and (b) when a query is
    needed, reuses a single DB connection for all three reads instead of
    opening four. This was the main source of pool exhaustion behind
    'Pricing temporarily unavailable'.
    """
    user_id = getattr(g, "user_id", None)
    if not user_id:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                from routes.auth_routes import _verify_supabase_jwt
                payload = _verify_supabase_jwt(auth_header.split(" ")[1])
                user_id = payload.get("sub")
            except Exception:
                user_id = None

    plans, paywall, trial_offer, failures = fetch_pricing_payload(user_id)

    for plan in plans:
        plan["billing_options"] = [opt for opt in (plan.get("billing_options") or []) if opt.get("enabled")]

    if trial_offer is None:
        trial_offer = {"enabled": False, "eligible": False}

    # If plans are gone there's nothing to show — surface a real 500 so the
    # page can show its error state. Otherwise return what we have.
    if not plans and "plans" in failures:
        return jsonify({"plans": [], "error": "Pricing temporarily unavailable"}), 500

    return jsonify({
        "plans": plans,
        "paywall": paywall,
        "trial_offer": trial_offer,
        "_partial": failures or None,
    })


@user_bp.route("/api/user/valvo-magic", methods=["GET"])
@limiter.limit("120 per minute")
def get_valvo_magic():
    """Return whether the user's Valvo Magic (sector grouping) is currently
    active and when it expires. Hydrated by Screener + WatchlistPage on mount
    so the toggle survives reloads, tab switches, and watchlist switches."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT valvo_magic_until FROM user_settings WHERE user_id = %s", (g.user_id,))
        row = cur.fetchone()
        until = row["valvo_magic_until"] if row else None
        if until is None:
            return jsonify({"enabled": False, "expires_at": None})
        cur.execute("SELECT %s > NOW() AS active", (until,))
        active = bool(cur.fetchone()["active"])
        return jsonify({
            "enabled": active,
            "expires_at": until.isoformat() if active else None,
        })
    except Exception as e:
        print(f"[user] valvo-magic GET error: {e}")
        return jsonify({"enabled": False, "expires_at": None})
    finally:
        close_db(conn)


@user_bp.route("/api/user/valvo-magic", methods=["POST"])
@limiter.limit("60 per minute")
def set_valvo_magic():
    """Turn Valvo Magic on/off for the current user. TTL is decided server-side
    from the user's plan: 7 days for paid, 1 day for free. Body: {on: bool}."""
    data = request.get_json(silent=True) or {}
    turn_on = bool(data.get("on"))
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (g.user_id,))
        if not turn_on:
            cur.execute("UPDATE user_settings SET valvo_magic_until = NULL WHERE user_id = %s", (g.user_id,))
            conn.commit()
            return jsonify({"enabled": False, "expires_at": None})
        cur.execute("SELECT plan, status FROM user_subscriptions WHERE user_id = %s", (g.user_id,))
        sub = cur.fetchone()
        is_paid = bool(sub) and (sub.get("plan") not in (None, "free")) and (sub.get("status") in ("active", "trial"))
        ttl_days = 7 if is_paid else 1
        cur.execute(
            "UPDATE user_settings SET valvo_magic_until = NOW() + (%s || ' days')::interval WHERE user_id = %s "
            "RETURNING valvo_magic_until",
            (ttl_days, g.user_id),
        )
        row = cur.fetchone()
        conn.commit()
        until = row["valvo_magic_until"] if row else None
        return jsonify({
            "enabled": True,
            "expires_at": until.isoformat() if until else None,
            "ttl_days": ttl_days,
        })
    except Exception as e:
        conn.rollback()
        print(f"[user] valvo-magic POST error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@user_bp.route("/api/user/visible-sections", methods=["GET"])
@limiter.limit("60 per minute")
def visible_sections():
    """Return which sections are enabled for regular users."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_config WHERE key = 'user_visible_sections'")
        row = cur.fetchone()
        sections = row["value"] if row else ["screener", "ipo-lab", "watchlist", "alerts", "market-breadth", "sectoral"]
        return jsonify({"sections": sections})
    except Exception:
        return jsonify({"sections": ["screener", "ipo-lab", "watchlist", "alerts", "market-breadth", "sectoral"]})
    finally:
        close_db(conn)
