"""
Admin Dashboard Routes — User management, stats, subscriptions, activity log.
All endpoints require admin role (checked via user_profiles).
"""

from flask import Blueprint, request, jsonify, g
from extensions import limiter
from services.admin_service import (
    is_admin, get_dashboard_stats, get_all_users, create_user_full,
    delete_user_full, update_user, update_subscription, reset_supabase_password,
    ban_supabase_user, unban_supabase_user,
    get_active_sessions, get_user_detail, log_admin_action, get_activity_log,
    get_revenue_stats, get_pricing_plans, upsert_pricing_plan, delete_pricing_plan,
    get_paywall_config, save_paywall_config, start_promotional_trial,
    create_coupon, get_coupons, update_coupon, deactivate_coupon, validate_coupon,
)
from services.razorpay_service import (
    is_configured as razorpay_configured,
    get_config_status as razorpay_config_status,
    save_config as razorpay_save_config,
    sync_plans_to_razorpay,
    create_subscription as razorpay_create_subscription,
    verify_subscription_payment as razorpay_verify_subscription,
    handle_webhook as razorpay_webhook,
    invalidate_cache as razorpay_invalidate,
)
from services.backend_access_service import (
    BackendAccessExpired,
    BackendAccessInvalid,
    BackendPasswordInvalid,
    issue_backend_access_token,
    verify_backend_access_token,
    verify_backend_password,
)
from services.session_security_service import (
    force_logout_all_sessions,
    force_logout_user_sessions,
    get_session_security_overview,
    save_session_security_config,
)

admin_dashboard_bp = Blueprint("admin_dashboard", __name__)
BACKEND_ACCESS_HEADER = "X-Backend-Access"


def _backend_step_up_response(message="Backend access confirmation required"):
    return jsonify({"error": message, "code": "backend_step_up_required"}), 401


def _require_admin_role():
    """Check admin role only. Returns error response or None if OK."""
    if not is_admin(g.user_id):
        return jsonify({"error": "Backend privileges required"}), 403
    return None


def _require_admin():
    """Check admin role + backend step-up token. Returns error response or None if OK."""
    denied = _require_admin_role()
    if denied:
        return denied

    token = (request.headers.get(BACKEND_ACCESS_HEADER) or "").strip()
    if not token:
        return _backend_step_up_response()

    try:
        g.backend_access = verify_backend_access_token(token, expected_user_id=str(g.user_id))
    except BackendAccessExpired:
        return _backend_step_up_response("Backend access expired. Confirm your password again.")
    except BackendAccessInvalid:
        return _backend_step_up_response()

    return None


def _normalize_cycle_id(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    raw = raw.replace("-", "_").replace(" ", "_")
    aliases = {
        "annual": "yearly",
        "annually": "yearly",
        "semiannual": "half_yearly",
        "semi_yearly": "half_yearly",
        "halfyearly": "half_yearly",
        "halfyear": "half_yearly",
        "6_months": "half_yearly",
        "3_months": "quarterly",
        "12_months": "yearly",
        "1_month": "monthly",
    }
    return aliases.get(raw, raw)


def _plan_catalog():
    catalog = {
        "free": {
            "name": "free",
            "display_name": "Free",
            "is_active": True,
            "billing_options": [{"id": "monthly", "label": "Monthly", "months": 1, "price": 0, "enabled": True}],
        }
    }
    for plan in get_pricing_plans(active_only=False):
        catalog[plan["name"]] = plan
    return catalog


def _validate_plan_selection(plan_name, billing_cycle=None, *, allow_inactive=True):
    catalog = _plan_catalog()
    normalized_plan = str(plan_name or "free").strip().lower()
    plan = catalog.get(normalized_plan)
    if not plan:
        raise ValueError("Selected plan is not available in Plans & Paywall")
    if normalized_plan != "free" and not allow_inactive and not plan.get("is_active"):
        raise ValueError("Selected plan is currently inactive in Plans & Paywall")

    normalized_cycle = None
    if billing_cycle is not None:
        normalized_cycle = _normalize_cycle_id(billing_cycle) or "monthly"
        options = [option for option in (plan.get("billing_options") or []) if option.get("enabled")]
        if not options:
            options = plan.get("billing_options") or [{"id": "monthly", "enabled": True}]
        allowed_cycles = {_normalize_cycle_id(option.get("id")) for option in options}
        if normalized_cycle not in allowed_cycles:
            raise ValueError(f"{plan.get('display_name', normalized_plan)} does not offer {normalized_cycle.replace('_', ' ')} billing")

    return normalized_plan, normalized_cycle


# ── Dashboard Stats ───────────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/stats", methods=["GET"])
@limiter.limit("60 per minute")
def stats():
    denied = _require_admin()
    if denied:
        return denied
    try:
        return jsonify(get_dashboard_stats())
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── User Management ──────────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/users", methods=["GET"])
@limiter.limit("60 per minute")
def list_users():
    denied = _require_admin()
    if denied:
        return denied
    try:
        return jsonify({"users": get_all_users()})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/users/<user_id>", methods=["GET"])
@limiter.limit("60 per minute")
def get_user(user_id):
    denied = _require_admin()
    if denied:
        return denied
    try:
        detail = get_user_detail(user_id)
        if not detail:
            return jsonify({"error": "User not found"}), 404
        return jsonify(detail)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/users", methods=["POST"])
@limiter.limit("60 per minute")
def create_user():
    denied = _require_admin()
    if denied:
        return denied

    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    display_name = (data.get("display_name") or "").strip()
    role = data.get("role", "user")
    plan = data.get("plan", "free")

    try:
        plan, _ = _validate_plan_selection(plan, allow_inactive=False)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not email or not password:
        return jsonify({"error": "email and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    try:
        result = create_user_full(email, password, display_name, role, plan)
        log_admin_action(g.user_id, "user_created", result["id"], {"email": email, "role": role, "plan": plan})
        return jsonify({"message": f"User {email} created", "user": result}), 201
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 400


@admin_dashboard_bp.route("/api/admin-dashboard/users/<user_id>", methods=["PUT"])
@limiter.limit("60 per minute")
def update_user_route(user_id):
    denied = _require_admin()
    if denied:
        return denied

    data = request.get_json() or {}
    if data.get("plan") is not None:
        try:
            data["plan"], _ = _validate_plan_selection(data.get("plan"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    try:
        update_user(
            user_id,
            role=data.get("role"),
            display_name=data.get("display_name"),
            plan=data.get("plan"),
            sub_status=data.get("sub_status"),
        )
        log_admin_action(g.user_id, "user_updated", user_id, data)
        return jsonify({"message": "User updated"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/users/<user_id>", methods=["DELETE"])
@limiter.limit("60 per minute")
def delete_user_route(user_id):
    denied = _require_admin()
    if denied:
        return denied

    # Prevent self-deletion
    if user_id == str(g.user_id):
        return jsonify({"error": "Cannot delete your own account"}), 400

    try:
        delete_user_full(user_id)
        log_admin_action(g.user_id, "user_deleted", user_id)
        return jsonify({"message": "User deleted"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/users/<user_id>/reset-password", methods=["POST"])
@limiter.limit("60 per minute")
def reset_password(user_id):
    denied = _require_admin()
    if denied:
        return denied

    data = request.get_json() or {}
    new_password = data.get("password", "")
    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    try:
        reset_supabase_password(user_id, new_password)
        log_admin_action(g.user_id, "password_reset", user_id)
        return jsonify({"message": "Password reset successfully"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── Ban / Unban ──────────────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/users/<user_id>/ban", methods=["POST"])
@limiter.limit("60 per minute")
def ban_user(user_id):
    denied = _require_admin()
    if denied:
        return denied
    if user_id == str(g.user_id):
        return jsonify({"error": "Cannot ban your own account"}), 400
    try:
        ban_supabase_user(user_id)
        log_admin_action(g.user_id, "user_banned", user_id)
        return jsonify({"message": "User banned"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/users/<user_id>/unban", methods=["POST"])
@limiter.limit("60 per minute")
def unban_user(user_id):
    denied = _require_admin()
    if denied:
        return denied
    try:
        unban_supabase_user(user_id)
        log_admin_action(g.user_id, "user_unbanned", user_id)
        return jsonify({"message": "User unbanned"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── Sessions ─────────────────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/sessions", methods=["GET"])
@limiter.limit("60 per minute")
def sessions():
    denied = _require_admin()
    if denied:
        return denied
    try:
        return jsonify({"sessions": get_active_sessions()})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/security", methods=["GET"])
@limiter.limit("60 per minute")
def session_security():
    denied = _require_admin()
    if denied:
        return denied
    try:
        return jsonify(get_session_security_overview())
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/security/config", methods=["PUT"])
@limiter.limit("60 per minute")
def update_session_security():
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json() or {}
    try:
        config = save_session_security_config(data)
        log_admin_action(g.user_id, "session_security_updated", details=config)
        return jsonify(config)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 400


@admin_dashboard_bp.route("/api/admin-dashboard/users/<user_id>/force-logout", methods=["POST"])
@limiter.limit("60 per minute")
def force_logout_user(user_id):
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "forced_logout").strip() or "forced_logout"
    message = str(data.get("message") or "").strip() or None
    try:
        notice = force_logout_user_sessions(user_id, reason=reason, message=message)
        log_admin_action(g.user_id, "user_force_logged_out", user_id, notice)
        return jsonify({"message": notice["message"], "code": notice["code"]})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/security/force-logout-all", methods=["POST"])
@limiter.limit("60 per minute")
def force_logout_everyone():
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "security_logout_all").strip() or "security_logout_all"
    message = str(data.get("message") or "").strip() or None
    try:
        notice = force_logout_all_sessions(reason=reason, message=message)
        log_admin_action(g.user_id, "all_users_force_logged_out", details=notice)
        return jsonify({"message": notice["message"], "code": notice["code"]})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── Subscriptions ────────────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/subscriptions/<user_id>", methods=["PUT"])
@limiter.limit("60 per minute")
def update_sub(user_id):
    denied = _require_admin()
    if denied:
        return denied

    data = request.get_json() or {}
    if data.get("plan") is not None:
        try:
            data["plan"], normalized_cycle = _validate_plan_selection(data.get("plan"), data.get("billing_cycle"))
            if normalized_cycle:
                data["billing_cycle"] = normalized_cycle
            if data["plan"] == "free":
                data["amount"] = 0
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    try:
        update_subscription(user_id, **data)
        log_admin_action(g.user_id, "subscription_updated", user_id, data)
        return jsonify({"message": "Subscription updated"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── Activity Log ─────────────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/activity", methods=["GET"])
@limiter.limit("60 per minute")
def activity():
    denied = _require_admin()
    if denied:
        return denied
    try:
        limit = request.args.get("limit", 50, type=int)
        return jsonify({"entries": get_activity_log(limit)})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── Revenue Analytics ────────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/revenue", methods=["GET"])
@limiter.limit("60 per minute")
def revenue():
    denied = _require_admin()
    if denied:
        return denied
    try:
        stats = get_revenue_stats()
        stats["razorpay_configured"] = razorpay_configured()
        return jsonify(stats)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── Backend Access Step-Up ───────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/access/verify", methods=["POST"])
@limiter.limit("60 per minute")
def verify_backend_access():
    denied = _require_admin_role()
    if denied:
        return denied

    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    if not str(password).strip():
        return jsonify({"error": "Password is required", "code": "backend_password_required"}), 400

    try:
        verify_backend_password(g.user_email or "", password)
        token_data = issue_backend_access_token(g.user_id, g.user_email or "")
        return jsonify(
            {
                **token_data,
                "user": {"id": g.user_id, "email": g.user_email or ""},
            }
        )
    except BackendPasswordInvalid as exc:
        return jsonify({"error": str(exc), "code": "invalid_backend_password"}), 401
    except Exception:
        return jsonify({"error": "Cannot verify backend access right now"}), 500


# ── Pricing Plans ────────────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/plans", methods=["GET"])
@limiter.limit("60 per minute")
def list_plans():
    denied = _require_admin()
    if denied:
        return denied
    try:
        plans = get_pricing_plans()
        # Serialize dates
        for p in plans:
            for k in ("created_at", "updated_at"):
                if p.get(k):
                    p[k] = p[k].isoformat()
            p["price_monthly"] = float(p["price_monthly"]) if p["price_monthly"] else 0
            p["price_yearly"] = float(p["price_yearly"]) if p["price_yearly"] else 0
        return jsonify({"plans": plans})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/paywall-config", methods=["GET"])
@limiter.limit("60 per minute")
def get_paywall_config_route():
    denied = _require_admin()
    if denied:
        return denied
    try:
        return jsonify(get_paywall_config())
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/paywall-config", methods=["PUT"])
@limiter.limit("60 per minute")
def update_paywall_config_route():
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json() or {}
    try:
        result = save_paywall_config(data)
        log_admin_action(g.user_id, "paywall_config_updated", details=result)
        return jsonify(result)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 400


@admin_dashboard_bp.route("/api/admin-dashboard/plans", methods=["POST"])
@limiter.limit("60 per minute")
def create_plan():
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json() or {}
    try:
        upsert_pricing_plan(**data)
        log_admin_action(g.user_id, "plan_created", details=data)
        return jsonify({"message": "Plan created"}), 201
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 400


@admin_dashboard_bp.route("/api/admin-dashboard/plans/<int:plan_id>", methods=["PUT"])
@limiter.limit("60 per minute")
def update_plan(plan_id):
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json() or {}
    try:
        upsert_pricing_plan(plan_id=plan_id, **data)
        log_admin_action(g.user_id, "plan_updated", details={"plan_id": plan_id, **data})
        return jsonify({"message": "Plan updated"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/plans/<int:plan_id>", methods=["DELETE"])
@limiter.limit("60 per minute")
def remove_plan(plan_id):
    denied = _require_admin()
    if denied:
        return denied
    try:
        delete_pricing_plan(plan_id)
        log_admin_action(g.user_id, "plan_deactivated", details={"plan_id": plan_id})
        return jsonify({"message": "Plan deactivated"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── Coupons ───────────────────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/coupons", methods=["GET"])
@limiter.limit("60 per minute")
def list_coupons():
    denied = _require_admin()
    if denied:
        return denied
    try:
        return jsonify({"coupons": get_coupons()})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/coupons", methods=["POST"])
@limiter.limit("60 per minute")
def create_coupon_route():
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json() or {}
    try:
        cid = create_coupon(
            code=data.get("code", ""),
            name=data.get("name"),
            discount_type=data.get("discount_type", "percent"),
            discount_value=data.get("discount_value", 0),
            max_uses=data.get("max_uses"),
            max_uses_per_user=data.get("max_uses_per_user", 1),
            applies_to=data.get("applies_to"),
            billing_cycles=data.get("billing_cycles"),
            valid_from=data.get("valid_from"),
            valid_until=data.get("valid_until"),
            created_by=g.user_id,
        )
        log_admin_action(g.user_id, "coupon_created", details={"code": data.get("code"), "id": cid})
        return jsonify({"id": cid, "message": "Coupon created"}), 201
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 400


@admin_dashboard_bp.route("/api/admin-dashboard/coupons/<int:coupon_id>", methods=["PUT"])
@limiter.limit("60 per minute")
def update_coupon_route(coupon_id):
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json() or {}
    try:
        update_coupon(coupon_id, **data)
        log_admin_action(g.user_id, "coupon_updated", details={"id": coupon_id, **data})
        return jsonify({"message": "Coupon updated"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/coupons/<int:coupon_id>", methods=["DELETE"])
@limiter.limit("60 per minute")
def deactivate_coupon_route(coupon_id):
    denied = _require_admin()
    if denied:
        return denied
    try:
        deactivate_coupon(coupon_id)
        log_admin_action(g.user_id, "coupon_deactivated", details={"id": coupon_id})
        return jsonify({"message": "Coupon deactivated"})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/coupons/validate", methods=["POST"])
@limiter.limit("60 per minute")
def validate_coupon_route():
    """User-facing: validate a coupon code and get discount preview."""
    data = request.get_json() or {}
    try:
        result = validate_coupon(
            data.get("code", ""),
            plan=data.get("plan"),
            billing_cycle=data.get("billing_cycle"),
        )
        return jsonify(result)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"valid": False, "error": "Internal error"}), 400


# ── Section Visibility ──────────────────────────────────────


# All available sections (source of truth for the toggle UI)
ALL_SECTIONS = [
    {"id": "scoring", "label": "Scoring", "desc": "Score live setups"},
    {"id": "screener", "label": "Screener", "desc": "Advanced stock scanner"},
    {"id": "watchlist", "label": "Watchlists", "desc": "Track & score watchlists"},
    {"id": "explore", "label": "Explore Stock", "desc": "Deep-dive any stock"},
    {"id": "sectoral", "label": "Sectoral", "desc": "Index & sector X-Ray"},
    {"id": "position", "label": "Position Manager", "desc": "Track & trail positions"},
    {"id": "ipo-lab", "label": "IPO Zone", "desc": "Live IPO tracker & analysis"},
    {"id": "journal", "label": "Valvo Journal", "desc": "Institutional trade journal"},
    {"id": "trade-analytics", "label": "Trade Analytics", "desc": "Trade analytics & history"},
    {"id": "alerts", "label": "Price Alerts", "desc": "Price alert notifications"},
    {"id": "market-breadth", "label": "Market Breadth", "desc": "Market timing & breadth"},
    {"id": "fundamentals", "label": "Fundamentals", "desc": "Company financials & ratios"},
    {"id": "past-winners", "label": "Past Winners", "desc": "Biggest gainers by date range"},
    {"id": "valvo-ai", "label": "Valvo AI", "desc": "AI-powered cockpit"},
    {"id": "project-hub", "label": "Project Hub", "desc": "Team tasks & collaboration"},
]


@admin_dashboard_bp.route("/api/admin-dashboard/section-visibility", methods=["GET"])
@limiter.limit("60 per minute")
def get_section_visibility():
    denied = _require_admin()
    if denied:
        return denied
    try:
        from database.database import get_db, close_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_config WHERE key = 'user_visible_sections'")
        row = cur.fetchone()
        close_db(conn)
        enabled = row["value"] if row else []
        return jsonify({"sections": ALL_SECTIONS, "enabled": enabled})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/section-visibility", methods=["PUT"])
@limiter.limit("60 per minute")
def update_section_visibility():
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json() or {}
    enabled = data.get("enabled")
    if not isinstance(enabled, list):
        return jsonify({"error": "enabled must be an array of section IDs"}), 400
    try:
        import json
        from database.database import get_db, close_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO app_config (key, value, updated_at)
            VALUES ('user_visible_sections', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (json.dumps(enabled),))
        conn.commit()
        close_db(conn)
        log_admin_action(g.user_id, "section_visibility_updated", details={"enabled": enabled})
        return jsonify({"message": "Section visibility updated", "enabled": enabled})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── Razorpay Config (Admin) ───────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/razorpay/status", methods=["GET"])
@limiter.limit("60 per minute")
def razorpay_status():
    denied = _require_admin()
    if denied:
        return denied
    return jsonify(razorpay_config_status())


@admin_dashboard_bp.route("/api/admin-dashboard/razorpay/config", methods=["POST"])
@limiter.limit("60 per minute")
def razorpay_config_save():
    denied = _require_admin()
    if denied:
        return denied
    data = request.get_json() or {}
    key_id = (data.get("key_id") or "").strip()
    key_secret = (data.get("key_secret") or "").strip()
    webhook_secret = (data.get("webhook_secret") or "").strip()
    if not key_id or not key_secret:
        return jsonify({"error": "Key ID and Key Secret are required"}), 400
    try:
        result = razorpay_save_config(key_id, key_secret, webhook_secret)
        log_admin_action(g.user_id, "razorpay_configured", details={"mode": result["mode"]})
        return jsonify(result)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_dashboard_bp.route("/api/admin-dashboard/razorpay/sync-plans", methods=["POST"])
@limiter.limit("60 per minute")
def razorpay_sync_plans():
    denied = _require_admin()
    if denied:
        return denied
    try:
        results = sync_plans_to_razorpay()
        log_admin_action(g.user_id, "razorpay_plans_synced", details={"results": results})
        return jsonify({"results": results})
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ── Razorpay Payments (User-facing) ──────────────────────────


@admin_dashboard_bp.route("/api/payments/subscribe", methods=["POST"])
@limiter.limit("60 per minute")
def payment_subscribe():
    """Create a Razorpay subscription for the logged-in user."""
    data = request.get_json() or {}
    try:
        result = razorpay_create_subscription(
            g.user_id, g.user_email,
            data.get("plan", "pro"), data.get("billing_cycle", "monthly"),
        )
        return jsonify(result)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 400


@admin_dashboard_bp.route("/api/payments/verify", methods=["POST"])
@limiter.limit("60 per minute")
def payment_verify():
    data = request.get_json() or {}
    try:
        result = razorpay_verify_subscription(
            data.get("payment_id"), data.get("subscription_id"), data.get("signature"),
        )
        return jsonify(result)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 400


@admin_dashboard_bp.route("/api/payments/start-trial", methods=["POST"])
@limiter.limit("60 per minute")
def payment_start_trial():
    try:
        result = start_promotional_trial(g.user_id)
        return jsonify(result)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 400


@admin_dashboard_bp.route("/api/payments/webhook", methods=["POST"])
@limiter.limit("60 per minute")
def payment_webhook():
    """Razorpay webhook — exempt from auth (uses webhook signature instead)."""
    try:
        payload = request.get_data(as_text=True)
        signature = request.headers.get("X-Razorpay-Signature", "")
        result = razorpay_webhook(payload, signature)
        return jsonify(result)
    except Exception as e:
        print(f"[admin_dashboard] error: {e}")
        return jsonify({"error": "Internal error"}), 400


# ── AI Errors / Telemetry (v3) ───────────────────────────────────────


@admin_dashboard_bp.route("/api/admin-dashboard/ai-errors", methods=["GET"])
@limiter.limit("60 per minute")
def ai_errors():
    """Recent v3 chat failures paired with the question that triggered them.

    Each error row is the assistant turn that has error_type populated; we
    look up the immediately preceding user row (same user_id, lower id) to
    surface the question. Joins user_settings for display_name + email so
    the admin can see who hit the error.

    Query params:
      days   — lookback window (default 7, max 90)
      limit  — max rows (default 100, max 500)
    """
    denied = _require_admin()
    if denied:
        return denied

    from database.database import get_db, close_db
    from datetime import datetime, timedelta, timezone

    try:
        days = max(1, min(int(request.args.get("days", 7)), 90))
    except (TypeError, ValueError):
        days = 7
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except (TypeError, ValueError):
        limit = 100

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH errors AS (
                SELECT id, role, content, user_id, engine, model_used,
                       error_type, error_detail, created_at
                FROM chat_messages
                WHERE error_type IS NOT NULL
                  AND created_at >= %s
                ORDER BY created_at DESC
                LIMIT %s
            )
            SELECT
                e.id            AS error_id,
                e.user_id       AS user_id,
                e.engine        AS engine,
                e.model_used    AS model_used,
                e.error_type    AS error_type,
                e.error_detail  AS error_detail,
                e.content       AS friendly_text,
                e.created_at    AS created_at,
                (
                    SELECT q.content
                    FROM chat_messages q
                    WHERE q.role = 'user'
                      AND q.user_id = e.user_id
                      AND q.id < e.id
                    ORDER BY q.id DESC
                    LIMIT 1
                )               AS question,
                us.display_name AS display_name
            FROM errors e
            LEFT JOIN user_settings us ON us.user_id::text = e.user_id
            ORDER BY e.created_at DESC
            """,
            (cutoff, limit),
        )
        rows = cur.fetchall()

        # Aggregate: total errors, error rate, top error types in same window.
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE role = 'assistant')                            AS total_responses,
                COUNT(*) FILTER (WHERE error_type IS NOT NULL)                        AS total_errors
            FROM chat_messages
            WHERE created_at >= %s
              AND role = 'assistant'
            """,
            (cutoff,),
        )
        agg = cur.fetchone() or {}

        cur.execute(
            """
            SELECT error_type, COUNT(*) AS cnt
            FROM chat_messages
            WHERE error_type IS NOT NULL AND created_at >= %s
            GROUP BY error_type
            ORDER BY cnt DESC
            LIMIT 5
            """,
            (cutoff,),
        )
        top_types = cur.fetchall() or []

        return jsonify({
            "errors": [
                {
                    "error_id": r["error_id"],
                    "user_id": r["user_id"],
                    "display_name": r.get("display_name") or "",
                    "engine": r.get("engine") or "",
                    "model_used": r.get("model_used") or "",
                    "error_type": r["error_type"],
                    "error_detail": r["error_detail"] or "",
                    "friendly_text": r["friendly_text"] or "",
                    "question": r["question"] or "(no preceding user message)",
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ],
            "stats": {
                "days": days,
                "total_responses": int(agg.get("total_responses") or 0),
                "total_errors": int(agg.get("total_errors") or 0),
                "error_rate_pct": round(
                    (int(agg.get("total_errors") or 0) / max(int(agg.get("total_responses") or 0), 1)) * 100,
                    2,
                ),
                "top_error_types": [
                    {"type": t["error_type"], "count": int(t["cnt"])} for t in top_types
                ],
            },
        })
    except Exception as e:
        print(f"[admin_dashboard] ai_errors failed: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)
