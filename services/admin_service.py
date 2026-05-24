"""
Admin Service — Supabase Admin API integration for user management.
Uses the service_role key (server-side only, never exposed to frontend).
"""
import os, json, time, copy, requests
from database.database import get_db, close_db
from services.session_security_service import describe_user_agent


# ── Pricing cache (process-local, short TTL) ──────────────────
# Pricing plans + paywall config change rarely (admin edits them) but
# /api/pricing is hit on every visit. Caching for 60s drops hot-path DB
# load to zero on warm workers and keeps the page up during pool blips.
_PRICING_CACHE_TTL = 60.0
_pricing_cache = {}


def _pricing_cache_get(key):
    entry = _pricing_cache.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if expires_at < time.monotonic():
        return None
    return copy.deepcopy(value)


def _pricing_cache_set(key, value):
    _pricing_cache[key] = (time.monotonic() + _PRICING_CACHE_TTL, copy.deepcopy(value))


def _pricing_cache_invalidate(*keys):
    if not keys:
        _pricing_cache.clear()
        return
    for key in keys:
        _pricing_cache.pop(key, None)

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://sxyktzpiixmidlxxfgdd.supabase.co")
SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")


def _admin_headers():
    return {
        "apikey": SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


# ── User CRUD via Supabase Admin API ──────────────────────────


def list_all_users_from_supabase():
    """Fetch all users from Supabase Auth admin endpoint."""
    resp = requests.get(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers=_admin_headers(),
        params={"page": 1, "per_page": 500},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("users", [])


def create_supabase_user(email, password):
    """Create a new user in Supabase Auth. Returns the created user dict."""
    resp = requests.post(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers=_admin_headers(),
        json={
            "email": email,
            "password": password,
            "email_confirm": True,
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        err = resp.json()
        raise Exception(err.get("msg") or err.get("message") or str(err))
    return resp.json()


def delete_supabase_user(user_id):
    """Delete a user from Supabase Auth."""
    resp = requests.delete(
        f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
        headers=_admin_headers(),
        timeout=15,
    )
    if resp.status_code >= 400 and resp.status_code != 404:
        err = resp.json()
        raise Exception(err.get("msg") or err.get("message") or str(err))
    return True


def reset_supabase_password(user_id, new_password):
    """Reset a user's password via Supabase Admin API."""
    resp = requests.put(
        f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
        headers=_admin_headers(),
        json={"password": new_password},
        timeout=15,
    )
    if resp.status_code >= 400:
        err = resp.json()
        raise Exception(err.get("msg") or err.get("message") or str(err))
    return True


def ban_supabase_user(user_id, duration="876600h"):
    """Ban a user via Supabase Admin API. Default 100 years (effectively permanent)."""
    resp = requests.put(
        f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
        headers=_admin_headers(),
        json={"ban_duration": duration},
        timeout=15,
    )
    if resp.status_code >= 400:
        err = resp.json()
        raise Exception(err.get("msg") or err.get("message") or str(err))
    return True


def unban_supabase_user(user_id):
    """Unban a user via Supabase Admin API."""
    resp = requests.put(
        f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
        headers=_admin_headers(),
        json={"ban_duration": "none"},
        timeout=15,
    )
    if resp.status_code >= 400:
        err = resp.json()
        raise Exception(err.get("msg") or err.get("message") or str(err))
    return True


# ── Database helpers ──────────────────────────────────────────


def is_admin(user_id):
    """Check if user has admin role in user_profiles."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM user_profiles WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        return row and row["role"] == "admin"
    finally:
        close_db(conn)


def get_dashboard_stats():
    """Aggregate rich stats for the admin dashboard — pulls from every Supabase table."""
    conn = get_db()
    try:
        cur = conn.cursor()

        # ── User counts ──
        cur.execute("SELECT COUNT(*) AS cnt FROM auth.users WHERE deleted_at IS NULL")
        total = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM auth.sessions WHERE created_at > NOW() - INTERVAL '7 days'")
        active_7d = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM auth.sessions WHERE created_at > NOW() - INTERVAL '24 hours'")
        active_24h = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(DISTINCT user_id) AS cnt FROM auth.sessions WHERE created_at > NOW() - INTERVAL '1 hour'")
        active_1h = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM user_subscriptions WHERE plan != 'free' AND status = 'active'")
        paid = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM auth.users WHERE email_confirmed_at IS NOT NULL AND deleted_at IS NULL")
        confirmed = cur.fetchone()["cnt"]

        # ── Auth sessions stats ──
        cur.execute("SELECT COUNT(*) AS cnt FROM auth.sessions")
        total_sessions = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) AS cnt FROM auth.refresh_tokens WHERE revoked = false")
        active_tokens = cur.fetchone()["cnt"]

        # ── Platform usage (per-table record counts) ──
        table_counts = {}
        for tbl in ["submissions", "positions", "journal_trades",
                     "chat_messages", "watchlist_items", "watchlists", "saved_scanners",
                     "price_alerts", "notifications", "project_tasks"]:
            try:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {tbl}")
                table_counts[tbl] = cur.fetchone()["cnt"]
            except Exception:
                table_counts[tbl] = 0

        # ── Per-user usage breakdown ──
        cur.execute("""
            SELECT u.id, u.email, p.display_name, p.role,
                   u.last_sign_in_at, u.created_at,
                   s.plan,
                   COALESCE(sub.submissions_count, 0) AS submissions,
                   COALESCE(pos.positions_count, 0) AS positions,
                   COALESCE(chat.chat_count, 0) AS chats,
                   COALESCE(jrn.journal_count, 0) AS journal_trades,
                   COALESCE(wl.watchlist_count, 0) AS watchlist_items
            FROM auth.users u
            LEFT JOIN user_profiles p ON p.user_id = u.id
            LEFT JOIN user_subscriptions s ON s.user_id = u.id
            LEFT JOIN (SELECT user_id, COUNT(*) AS submissions_count FROM submissions GROUP BY user_id) sub ON sub.user_id = u.id
            LEFT JOIN (SELECT user_id, COUNT(*) AS positions_count FROM positions GROUP BY user_id) pos ON pos.user_id = u.id
            LEFT JOIN (SELECT user_id, COUNT(*) AS chat_count FROM chat_messages GROUP BY user_id) chat ON chat.user_id = u.id
            LEFT JOIN (SELECT user_id, COUNT(*) AS journal_count FROM journal_trades GROUP BY user_id) jrn ON jrn.user_id = u.id
            LEFT JOIN (SELECT user_id, COUNT(*) AS watchlist_count FROM watchlist_items GROUP BY user_id) wl ON wl.user_id = u.id
            WHERE u.deleted_at IS NULL
            ORDER BY u.created_at DESC
        """)
        user_breakdown = []
        for r in cur.fetchall():
            user_breakdown.append({
                "id": str(r["id"]), "email": r["email"],
                "display_name": r["display_name"], "role": r["role"] or "user",
                "plan": r["plan"] or "free",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "last_sign_in": r["last_sign_in_at"].isoformat() if r["last_sign_in_at"] else None,
                "submissions": r["submissions"], "positions": r["positions"],
                "chats": r["chats"], "journal_trades": r["journal_trades"],
                "watchlist_items": r["watchlist_items"],
                "total_actions": r["submissions"] + r["positions"] + r["chats"] + r["journal_trades"] + r["watchlist_items"],
            })

        # ── Market data stats ──
        market_data = {}
        for tbl, label in [("stock_universe", "stocks"), ("candles_daily", "daily_candles"),
                           ("financials_quarterly", "quarterly_financials"),
                           ("financials_annual", "annual_financials"),
                           ("filings", "filings"), ("corporate_actions", "corporate_actions")]:
            try:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {tbl}")
                market_data[label] = cur.fetchone()["cnt"]
            except Exception:
                market_data[label] = 0

        # ── Recent sessions with details ──
        cur.execute("""
            SELECT s.user_id, s.created_at, s.user_agent, s.ip,
                   u.email, p.display_name
            FROM auth.sessions s
            JOIN auth.users u ON u.id = s.user_id
            LEFT JOIN user_profiles p ON p.user_id = s.user_id
            ORDER BY s.created_at DESC LIMIT 20
        """)
        recent_sessions = []
        for r in cur.fetchall():
            ua = r["user_agent"] or ""
            # Parse device type from user agent
            device = "Desktop"
            if "Mobile" in ua or "Android" in ua or "iPhone" in ua:
                device = "Mobile"
            elif "iPad" in ua or "Tablet" in ua:
                device = "Tablet"
            # Parse browser
            browser = "Unknown"
            for b in ["Chrome", "Safari", "Firefox", "Edge"]:
                if b in ua:
                    browser = b
                    break
            recent_sessions.append({
                "user_id": str(r["user_id"]), "email": r["email"],
                "display_name": r["display_name"],
                "ip": str(r["ip"]) if r["ip"] else None,
                "device": device, "browser": browser,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })

        # ── Database size ──
        try:
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database())) AS size")
            db_size = cur.fetchone()["size"]
        except Exception:
            db_size = "N/A"

        # ── Daily cumulative user growth (last 90 days) ──
        # For each day, show how many total users existed by that date
        cur.execute("""
            WITH daily AS (
                SELECT DATE(created_at) AS day, COUNT(*) AS new_users
                FROM auth.users
                WHERE deleted_at IS NULL
                GROUP BY day ORDER BY day
            )
            SELECT day, SUM(new_users) OVER (ORDER BY day) AS total_users, new_users
            FROM daily
            WHERE day > NOW() - INTERVAL '90 days'
            ORDER BY day
        """)
        daily_user_growth = [{"day": r["day"].isoformat(), "total": r["total_users"], "new": r["new_users"]} for r in cur.fetchall()]

        # Fill gaps — if no signups on a day, carry forward the cumulative total
        if daily_user_growth:
            from datetime import date, timedelta
            filled = []
            start = date.fromisoformat(daily_user_growth[0]["day"])
            end = date.today()
            day_map = {d["day"]: d for d in daily_user_growth}
            last_total = daily_user_growth[0]["total"] - daily_user_growth[0]["new"]
            d = start
            while d <= end:
                ds = d.isoformat()
                if ds in day_map:
                    last_total = day_map[ds]["total"]
                    filled.append({"day": ds, "total": last_total, "new": day_map[ds]["new"]})
                else:
                    filled.append({"day": ds, "total": last_total, "new": 0})
                d += timedelta(days=1)
            daily_user_growth = filled

        # Backward-compat key
        monthly_signups = daily_user_growth

        # ── Daily cumulative revenue (last 90 days) ──
        cur.execute("""
            WITH daily AS (
                SELECT DATE(created_at) AS day, SUM(amount) AS revenue
                FROM payment_events
                WHERE status = 'success' AND event_type = 'payment'
                GROUP BY day ORDER BY day
            )
            SELECT day, revenue, SUM(revenue) OVER (ORDER BY day) AS cumulative
            FROM daily
            WHERE day > NOW() - INTERVAL '90 days'
            ORDER BY day
        """)
        raw_rev = [{"day": r["day"].isoformat(), "revenue": float(r["revenue"]), "cumulative": float(r["cumulative"])} for r in cur.fetchall()]

        # Fill gaps for revenue too
        if raw_rev:
            from datetime import date, timedelta
            filled_rev = []
            start = date.fromisoformat(raw_rev[0]["day"])
            end = date.today()
            rev_map = {d["day"]: d for d in raw_rev}
            last_cum = 0
            d = start
            while d <= end:
                ds = d.isoformat()
                if ds in rev_map:
                    last_cum = rev_map[ds]["cumulative"]
                    filled_rev.append({"day": ds, "revenue": rev_map[ds]["revenue"], "cumulative": last_cum})
                else:
                    filled_rev.append({"day": ds, "revenue": 0, "cumulative": last_cum})
                d += timedelta(days=1)
            raw_rev = filled_rev

        monthly_revenue = raw_rev

        # ── MRR from active subscriptions ──
        try:
            cur.execute("""
                SELECT s.plan, s.billing_cycle, pp.*
                FROM user_subscriptions s
                JOIN pricing_plans pp ON pp.name = s.plan
                WHERE s.plan != 'free' AND s.status = 'active'
            """)
            mrr = round(sum(_monthly_equivalent_from_plan(row, row.get("billing_cycle")) for row in cur.fetchall()), 2)
        except Exception:
            mrr = 0

        # ── Total revenue all time ──
        try:
            cur.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM payment_events WHERE status = 'success' AND event_type = 'payment'")
            total_revenue = float(cur.fetchone()["total"])
        except Exception:
            total_revenue = 0

        # ── Recent signups (last 5) ──
        cur.execute("""
            SELECT u.email, p.display_name, u.created_at
            FROM auth.users u
            LEFT JOIN user_profiles p ON p.user_id = u.id
            WHERE u.deleted_at IS NULL
            ORDER BY u.created_at DESC LIMIT 5
        """)
        recent_signups = [{
            "email": r["email"], "name": r["display_name"],
            "joined": r["created_at"].isoformat() if r["created_at"] else None,
        } for r in cur.fetchall()]

        # ── Recent admin actions (last 5) ──
        cur.execute("""
            SELECT a.action, a.details, a.created_at, p.display_name AS admin_name
            FROM admin_activity_log a
            LEFT JOIN user_profiles p ON p.user_id = a.admin_user_id
            ORDER BY a.created_at DESC LIMIT 5
        """)
        recent_actions = [{
            "action": r["action"], "details": r["details"],
            "admin": r["admin_name"],
            "time": r["created_at"].isoformat() if r["created_at"] else None,
        } for r in cur.fetchall()]

        return {
            "total_users": total,
            "active_7d": active_7d,
            "active_24h": active_24h,
            "active_1h": active_1h,
            "paid_users": paid,
            "free_users": total - paid,
            "confirmed_users": confirmed,
            "total_sessions": total_sessions,
            "active_tokens": active_tokens,
            "mrr": mrr,
            "total_revenue": total_revenue,
            "table_counts": table_counts,
            "user_breakdown": user_breakdown,
            "market_data": market_data,
            "recent_sessions": recent_sessions,
            "db_size": db_size,
            "monthly_signups": monthly_signups,
            "monthly_revenue": monthly_revenue,
            "recent_signups": recent_signups,
            "recent_actions": recent_actions,
        }
    finally:
        close_db(conn)


def get_all_users():
    """List all users joined with profiles and subscriptions."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                u.id, u.email, u.created_at, u.last_sign_in_at,
                u.email_confirmed_at, u.banned_until,
                p.display_name, p.role,
                s.plan, s.status AS sub_status, s.billing_cycle,
                s.start_date AS sub_start, s.end_date AS sub_end,
                s.amount AS sub_amount, s.payment_method,
                COALESCE(sess.active_devices, 0) AS active_devices
            FROM auth.users u
            LEFT JOIN user_profiles p ON p.user_id = u.id
            LEFT JOIN user_subscriptions s ON s.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS active_devices
                FROM auth.sessions
                GROUP BY user_id
            ) sess ON sess.user_id = u.id
            WHERE u.deleted_at IS NULL
            ORDER BY u.created_at DESC
        """)
        users = []
        for r in cur.fetchall():
            users.append({
                "id": str(r["id"]),
                "email": r["email"],
                "display_name": r["display_name"],
                "role": r["role"] or "user",
                "plan": r["plan"] or "free",
                "sub_status": r["sub_status"] or "active",
                "billing_cycle": r["billing_cycle"] or "monthly",
                "sub_amount": float(r["sub_amount"]) if r["sub_amount"] else 0,
                "payment_method": r["payment_method"],
                "sub_start": r["sub_start"].isoformat() if r["sub_start"] else None,
                "sub_end": r["sub_end"].isoformat() if r["sub_end"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "last_sign_in": r["last_sign_in_at"].isoformat() if r["last_sign_in_at"] else None,
                "email_confirmed": r["email_confirmed_at"] is not None,
                "banned": r["banned_until"] is not None,
                "active_devices": int(r["active_devices"] or 0),
            })
        return users
    finally:
        close_db(conn)


def create_user_full(email, password, display_name, role, plan):
    """Create user in Supabase Auth + user_profiles + user_subscriptions."""
    # 1. Create in Supabase Auth
    sb_user = create_supabase_user(email, password)
    user_id = sb_user.get("id")

    # 2. Insert profile + subscription in our DB
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_profiles (user_id, role, display_name, email)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE
            SET role = EXCLUDED.role, display_name = EXCLUDED.display_name
        """, (user_id, role, display_name, email))

        cur.execute("""
            INSERT INTO user_subscriptions (user_id, plan, status)
            VALUES (%s, %s, 'active')
            ON CONFLICT (user_id) DO UPDATE
            SET plan = EXCLUDED.plan, updated_at = NOW()
        """, (user_id, plan))

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)

    return {"id": user_id, "email": email, "role": role, "plan": plan}


def delete_user_full(user_id):
    """Delete user from Supabase Auth. DB rows cascade via FK or we clean up manually."""
    # Clean up our custom tables first (no FK to auth.users on some)
    conn = get_db()
    try:
        cur = conn.cursor()
        for tbl in ["user_subscriptions", "user_profiles", "user_settings",
                     "user_fy_config", "chat_messages", "notifications"]:
            cur.execute(f"DELETE FROM {tbl} WHERE user_id = %s::uuid", (user_id,))
        # Tables with text user_id column
        for tbl in ["price_alerts"]:
            cur.execute(f"DELETE FROM {tbl} WHERE user_id = %s", (user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        close_db(conn)

    # Delete from Supabase Auth (cascades auth.sessions, refresh_tokens etc.)
    delete_supabase_user(user_id)
    return True


def update_user(user_id, role=None, display_name=None, plan=None, sub_status=None):
    """Update user profile and/or subscription."""
    conn = get_db()
    try:
        cur = conn.cursor()

        if role is not None or display_name is not None:
            sets, vals = [], []
            if role is not None:
                sets.append("role = %s")
                vals.append(role)
            if display_name is not None:
                sets.append("display_name = %s")
                vals.append(display_name)
            vals.append(user_id)
            cur.execute(
                f"UPDATE user_profiles SET {', '.join(sets)} WHERE user_id = %s",
                vals,
            )

        if plan is not None or sub_status is not None:
            sets, vals = [], []
            if plan is not None:
                sets.append("plan = %s")
                vals.append(plan)
            if sub_status is not None:
                sets.append("status = %s")
                vals.append(sub_status)
            sets.append("updated_at = NOW()")
            vals.append(user_id)
            cur.execute(
                f"UPDATE user_subscriptions SET {', '.join(sets)} WHERE user_id = %s",
                vals,
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def update_subscription(user_id, **kwargs):
    """Update subscription fields (plan, status, billing_cycle, amount, dates, etc.)."""
    allowed = {"plan", "status", "billing_cycle", "amount", "payment_method",
               "payment_ref", "start_date", "end_date", "notes"}
    sets, vals = ["updated_at = NOW()"], []
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            sets.append(f"{k} = %s")
            vals.append(v)
    if len(sets) == 1:
        return  # nothing to update
    vals.append(user_id)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE user_subscriptions SET {', '.join(sets)} WHERE user_id = %s",
            vals,
        )
        if cur.rowcount == 0:
            # No subscription row exists — create one
            cur.execute("""
                INSERT INTO user_subscriptions (user_id, plan, status)
                VALUES (%s, %s, %s)
            """, (user_id, kwargs.get("plan", "free"), kwargs.get("status", "active")))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def get_active_sessions():
    """Get recent login sessions from auth.sessions."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.id, s.user_id, s.created_at, s.updated_at,
                   s.user_agent, s.ip,
                   u.email, p.display_name
            FROM auth.sessions s
            JOIN auth.users u ON u.id = s.user_id
            LEFT JOIN user_profiles p ON p.user_id = s.user_id
            ORDER BY s.created_at DESC
            LIMIT 50
        """)
        sessions = []
        for r in cur.fetchall():
            sessions.append({
                "id": str(r["id"]),
                "user_id": str(r["user_id"]),
                "email": r["email"],
                "display_name": r["display_name"],
                "user_agent": r["user_agent"],
                "ip": str(r["ip"]) if r["ip"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            })
        return sessions
    finally:
        close_db(conn)


def get_user_detail(user_id):
    """Get detailed user info including usage stats."""
    conn = get_db()
    try:
        cur = conn.cursor()

        # Profile + subscription
        cur.execute("""
            SELECT u.id, u.email, u.created_at, u.last_sign_in_at,
                   p.display_name, p.role,
                   s.plan, s.status AS sub_status, s.billing_cycle,
                   s.start_date, s.end_date, s.amount, s.payment_method
            FROM auth.users u
            LEFT JOIN user_profiles p ON p.user_id = u.id
            LEFT JOIN user_subscriptions s ON s.user_id = u.id
            WHERE u.id = %s::uuid
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            return None

        # Usage stats
        stats = {}
        for tbl, col in [("submissions", "user_id"), ("positions", "user_id"),
                         ("chat_messages", "user_id"), ("journal_trades", "user_id"),
                         ("watchlist_items", "user_id")]:
            try:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {tbl} WHERE {col} = %s::uuid", (user_id,))
                stats[tbl] = cur.fetchone()["cnt"]
            except Exception:
                stats[tbl] = 0

        # Recent sessions
        cur.execute("""
            SELECT created_at, user_agent, ip
            FROM auth.sessions
            WHERE user_id = %s::uuid
            ORDER BY created_at DESC LIMIT 5
        """, (user_id,))
        sessions = [
            {
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "user_agent": r["user_agent"],
                "ip": str(r["ip"]) if r["ip"] else None,
                **describe_user_agent(r["user_agent"]),
            }
            for r in cur.fetchall()
        ]

        cur.execute("SELECT COUNT(*) AS cnt FROM auth.sessions WHERE user_id = %s::uuid", (user_id,))
        active_devices = int((cur.fetchone() or {}).get("cnt") or 0)

        return {
            "id": str(row["id"]),
            "email": row["email"],
            "display_name": row["display_name"],
            "role": row["role"] or "user",
            "plan": row["plan"] or "free",
            "sub_status": row["sub_status"] or "active",
            "billing_cycle": row["billing_cycle"],
            "sub_amount": float(row["amount"]) if row["amount"] else 0,
            "payment_method": row["payment_method"],
            "sub_start": row["start_date"].isoformat() if row["start_date"] else None,
            "sub_end": row["end_date"].isoformat() if row["end_date"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "last_sign_in": row["last_sign_in_at"].isoformat() if row["last_sign_in_at"] else None,
            "active_devices": active_devices,
            "usage": stats,
            "recent_sessions": sessions,
        }
    finally:
        close_db(conn)


def log_admin_action(admin_user_id, action, target_user_id=None, details=None):
    """Record an admin action in the activity log."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO admin_activity_log (admin_user_id, action, target_user_id, details)
            VALUES (%s, %s, %s, %s)
        """, (admin_user_id, action, target_user_id, json.dumps(details or {})))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        close_db(conn)


# ── Revenue & Pricing ─────────────────────────────────────────


DEFAULT_PAYWALL_CONFIG = {
    "headline": "Choose Your Plan",
    "subheadline": "Pick the plan that fits your trading style",
    "footer_note": "Secure payments via Razorpay. Cancel anytime.",
    "default_billing_cycle": "monthly",
    "trial_offer": {
        "enabled": False,
        "label": "Start Free Trial for 24 Hours",
        "plan_name": "pro",
        "billing_cycle": "monthly",
        "duration_hours": 24,
        "badge": "Limited promotion",
    },
}

_BILLING_TEMPLATES = {
    "monthly": {"label": "Monthly", "months": 1},
    "quarterly": {"label": "Quarterly", "months": 3},
    "half_yearly": {"label": "Half Yearly", "months": 6},
    "yearly": {"label": "Yearly", "months": 12},
}


def _json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return fallback


def _billing_cycle_id(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    raw = raw.replace("-", "_").replace(" ", "_")
    aliases = {
        "annual": "yearly",
        "annually": "yearly",
        "semiannual": "half_yearly",
        "semi_annually": "half_yearly",
        "semi_annuallyy": "half_yearly",
        "semi_yearly": "half_yearly",
        "halfyearly": "half_yearly",
        "halfyear": "half_yearly",
        "6_months": "half_yearly",
        "3_months": "quarterly",
        "12_months": "yearly",
        "1_month": "monthly",
    }
    return aliases.get(raw, raw)


def _billing_template_for(option_id, months):
    option_id = _billing_cycle_id(option_id)
    if option_id in _BILLING_TEMPLATES:
        return _BILLING_TEMPLATES[option_id]
    if months == 1:
        return _BILLING_TEMPLATES["monthly"]
    if months == 3:
        return _BILLING_TEMPLATES["quarterly"]
    if months == 6:
        return _BILLING_TEMPLATES["half_yearly"]
    if months == 12:
        return _BILLING_TEMPLATES["yearly"]
    return {"label": f"{months} Months", "months": months}


def normalize_billing_options(raw_options=None, monthly_price=0, yearly_price=0):
    raw = _json_value(raw_options, None)
    if not raw:
        raw = []
        if float(monthly_price or 0) > 0:
            raw.append({"id": "monthly", "price": float(monthly_price or 0), "enabled": True, "sort_order": 1})
        if float(yearly_price or 0) > 0:
            compare_total = float(monthly_price or 0) * 12
            yearly = {
                "id": "yearly",
                "price": float(yearly_price or 0),
                "enabled": True,
                "sort_order": 12,
            }
            if compare_total > 0:
                yearly["discount_pct"] = round((1 - float(yearly_price or 0) / compare_total) * 100, 1)
            raw.append(yearly)

    normalized = []
    seen = set()
    monthly_base = float(monthly_price or 0)
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        option_id = _billing_cycle_id(item.get("id") or item.get("label") or f"cycle_{idx + 1}")
        if not option_id or option_id in seen:
            continue
        seen.add(option_id)

        months = int(item.get("months") or _billing_template_for(option_id, 0)["months"] or 1)
        template = _billing_template_for(option_id, months)
        price = float(item.get("price") or 0)
        if price <= 0:
            if months == 1 and float(monthly_price or 0) > 0:
                price = float(monthly_price or 0)
            elif months == 12 and float(yearly_price or 0) > 0:
                price = float(yearly_price or 0)

        compare_total = monthly_base * months if monthly_base > 0 and months > 0 else 0
        discount_pct = item.get("discount_pct")
        if discount_pct in ("", None):
            discount_pct = round((1 - price / compare_total) * 100, 1) if compare_total > 0 and price > 0 and months > 1 else 0
        else:
            discount_pct = round(float(discount_pct), 2)

        badge = str(item.get("badge") or "").strip()
        if not badge and discount_pct > 0 and months > 1:
            badge = f"Save {int(round(discount_pct))}%"

        normalized.append({
            "id": option_id,
            "label": str(item.get("label") or template["label"]).strip(),
            "months": months,
            "price": round(price, 2),
            "discount_pct": round(max(0, discount_pct), 2),
            "enabled": bool(item.get("enabled", True)),
            "badge": badge,
            "sort_order": int(item.get("sort_order") or months or idx + 1),
            "razorpay_plan_id": str(item.get("razorpay_plan_id") or "").strip() or None,
        })

    normalized.sort(key=lambda item: (item["sort_order"], item["months"], item["label"]))
    return normalized


def _legacy_prices_from_billing(options, fallback_monthly=0, fallback_yearly=0):
    monthly = next((float(item["price"]) for item in options if item["months"] == 1), float(fallback_monthly or 0))
    yearly = next((float(item["price"]) for item in options if item["months"] == 12), float(fallback_yearly or 0))
    return round(monthly, 2), round(yearly, 2)


def _serialize_pricing_plan(row):
    plan = dict(row)
    monthly_price = float(plan.get("price_monthly") or 0)
    yearly_price = float(plan.get("price_yearly") or 0)
    billing_options = normalize_billing_options(plan.get("billing_options"), monthly_price, yearly_price)
    legacy_monthly, legacy_yearly = _legacy_prices_from_billing(billing_options, monthly_price, yearly_price)
    plan["price_monthly"] = legacy_monthly
    plan["price_yearly"] = legacy_yearly
    plan["features"] = _json_value(plan.get("features"), [])
    plan["billing_options"] = billing_options
    plan["trial_days"] = int(plan.get("trial_days") or 0)
    return plan


def _monthly_equivalent_from_plan(plan_row, billing_cycle):
    option_id = _billing_cycle_id(billing_cycle)
    plan = _serialize_pricing_plan(plan_row)
    option = next((item for item in plan["billing_options"] if item["id"] == option_id and item.get("enabled")), None)
    if not option:
        option = next((item for item in plan["billing_options"] if item["id"] == option_id), None)
    if not option and option_id == "yearly" and plan["price_yearly"] > 0:
        return plan["price_yearly"] / 12
    if not option and option_id == "monthly" and plan["price_monthly"] > 0:
        return plan["price_monthly"]
    if not option:
        option = next((item for item in plan["billing_options"] if item.get("enabled")), None)
    if not option:
        return 0
    months = max(1, int(option.get("months") or 1))
    return float(option.get("price") or 0) / months


def _load_app_config(cur, key, default):
    cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
    row = cur.fetchone()
    if not row:
        return default
    return _json_value(row["value"], default)


def _fetch_paywall_config(cur):
    raw = _load_app_config(cur, "paywall_config", DEFAULT_PAYWALL_CONFIG)
    config = {**DEFAULT_PAYWALL_CONFIG, **(raw or {})}
    config["trial_offer"] = {
        **DEFAULT_PAYWALL_CONFIG["trial_offer"],
        **((raw or {}).get("trial_offer") or {}),
    }
    config["default_billing_cycle"] = _billing_cycle_id(config.get("default_billing_cycle") or "monthly") or "monthly"
    config["trial_offer"]["billing_cycle"] = _billing_cycle_id(
        config["trial_offer"].get("billing_cycle") or "monthly"
    ) or "monthly"
    config["trial_offer"]["duration_hours"] = max(1, min(int(config["trial_offer"].get("duration_hours") or 24), 168))
    return config


def get_paywall_config():
    cached = _pricing_cache_get("paywall_config")
    if cached is not None:
        return cached
    conn = get_db()
    try:
        config = _fetch_paywall_config(conn.cursor())
        _pricing_cache_set("paywall_config", config)
        return config
    finally:
        close_db(conn)


def save_paywall_config(config):
    trial_offer = {**DEFAULT_PAYWALL_CONFIG["trial_offer"], **(config.get("trial_offer") or {})}
    payload = {
        "headline": str(config.get("headline") or DEFAULT_PAYWALL_CONFIG["headline"]).strip() or DEFAULT_PAYWALL_CONFIG["headline"],
        "subheadline": str(config.get("subheadline") or DEFAULT_PAYWALL_CONFIG["subheadline"]).strip() or DEFAULT_PAYWALL_CONFIG["subheadline"],
        "footer_note": str(config.get("footer_note") or DEFAULT_PAYWALL_CONFIG["footer_note"]).strip() or DEFAULT_PAYWALL_CONFIG["footer_note"],
        "default_billing_cycle": _billing_cycle_id(config.get("default_billing_cycle") or "monthly") or "monthly",
        "trial_offer": {
            "enabled": bool(trial_offer.get("enabled")),
            "label": str(trial_offer.get("label") or DEFAULT_PAYWALL_CONFIG["trial_offer"]["label"]).strip() or DEFAULT_PAYWALL_CONFIG["trial_offer"]["label"],
            "plan_name": str(trial_offer.get("plan_name") or "pro").strip() or "pro",
            "billing_cycle": _billing_cycle_id(trial_offer.get("billing_cycle") or "monthly") or "monthly",
            "duration_hours": max(1, min(int(trial_offer.get("duration_hours") or 24), 168)),
            "badge": str(trial_offer.get("badge") or DEFAULT_PAYWALL_CONFIG["trial_offer"]["badge"]).strip(),
        },
    }
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO app_config (key, value, updated_at)
            VALUES ('paywall_config', %s::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (json.dumps(payload),))
        conn.commit()
        _pricing_cache_invalidate("paywall_config")
        return payload
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def _compute_trial_offer_status(cur, user_id, paywall_config):
    trial = {**paywall_config["trial_offer"], "eligible": False}
    if not trial.get("enabled") or not user_id:
        return trial

    cur.execute("""
        SELECT plan, status, trial_used_at, end_date
        FROM user_subscriptions
        WHERE user_id = %s
    """, (user_id,))
    row = cur.fetchone()
    if not row:
        trial["eligible"] = True
        return trial

    if row.get("trial_used_at"):
        return trial
    if row.get("status") == "trial" and row.get("end_date"):
        return trial
    if row.get("plan") and row.get("plan") != "free" and row.get("status") in {"active", "trial"}:
        return trial

    trial["eligible"] = True
    return trial


def get_trial_offer_status(user_id):
    config = get_paywall_config()
    if not config["trial_offer"].get("enabled") or not user_id:
        return {**config["trial_offer"], "eligible": False}

    conn = get_db()
    try:
        return _compute_trial_offer_status(conn.cursor(), user_id, config)
    finally:
        close_db(conn)


def fetch_pricing_payload(user_id):
    """Single-connection fetch of plans + paywall + trial-offer for the
    public /api/pricing page. Returns (plans, paywall, trial_offer, failures).

    Each piece is fetched in its own try/except — a transient failure in
    trial-offer no longer hides plans behind a 500. Cached values
    short-circuit DB access entirely so warm workers don't open any
    connection on the hot path. Previous implementation opened up to four
    sequential connections per request, which was the main source of pool
    exhaustion behind 'Pricing temporarily unavailable'.
    """
    import traceback

    plans = _pricing_cache_get("plans_active")
    paywall = _pricing_cache_get("paywall_config")
    trial_offer = None
    failures = []

    needs_db = (plans is None) or (paywall is None) or bool(user_id)
    if not needs_db:
        # All static pieces cached and no signed-in user — no DB needed.
        trial_offer = {**(paywall or DEFAULT_PAYWALL_CONFIG)["trial_offer"], "eligible": False}
        return plans or [], paywall, trial_offer, failures

    try:
        conn = get_db()
    except Exception as e:
        print(f"[admin_service] fetch_pricing_payload pool unavailable: {type(e).__name__}: {e}")
        traceback.print_exc()
        if plans is None:
            failures.append("plans")
        if paywall is None:
            failures.append("paywall")
        failures.append("trial_offer")
        return plans or [], paywall, None, failures

    try:
        cur = conn.cursor()

        if plans is None:
            try:
                plans = _fetch_pricing_plans(cur, active_only=True)
                _pricing_cache_set("plans_active", plans)
            except Exception as e:
                print(f"[admin_service] fetch_pricing_payload.plans error: {type(e).__name__}: {e}")
                traceback.print_exc()
                failures.append("plans")
                conn.rollback()  # clear aborted-tx state for next read

        if paywall is None:
            try:
                paywall = _fetch_paywall_config(cur)
                _pricing_cache_set("paywall_config", paywall)
            except Exception as e:
                print(f"[admin_service] fetch_pricing_payload.paywall error: {type(e).__name__}: {e}")
                traceback.print_exc()
                failures.append("paywall")
                conn.rollback()

        try:
            trial_offer = _compute_trial_offer_status(
                cur, user_id, paywall or DEFAULT_PAYWALL_CONFIG
            )
        except Exception as e:
            print(f"[admin_service] fetch_pricing_payload.trial_offer error: {type(e).__name__}: {e}")
            traceback.print_exc()
            failures.append("trial_offer")
            conn.rollback()
            trial_offer = {"enabled": False, "eligible": False}
    finally:
        close_db(conn)

    return plans or [], paywall, trial_offer, failures


def start_promotional_trial(user_id):
    config = get_paywall_config()
    trial = config["trial_offer"]
    if not trial.get("enabled"):
        raise Exception("Free trial is not active right now")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT plan, status, trial_used_at
            FROM user_subscriptions
            WHERE user_id = %s
        """, (user_id,))
        row = cur.fetchone()
        if row and row.get("trial_used_at"):
            raise Exception("You have already used the free trial")
        if row and row.get("plan") != "free" and row.get("status") in {"active", "trial"}:
            raise Exception("Free trial is only available to free users")

        plans = {plan["name"]: plan for plan in get_pricing_plans(active_only=True)}
        plan = plans.get(trial.get("plan_name"))
        if not plan:
            raise Exception("Trial plan is not configured correctly")

        option_id = _billing_cycle_id(trial.get("billing_cycle") or "monthly") or "monthly"
        option = next((item for item in plan["billing_options"] if item["id"] == option_id and item.get("enabled")), None)
        if not option:
            option = next((item for item in plan["billing_options"] if item.get("enabled")), None)
        if not option:
            raise Exception("No active billing option available for the trial plan")

        duration_hours = max(1, min(int(trial.get("duration_hours") or 24), 168))
        cur.execute("""
            INSERT INTO user_subscriptions (
                user_id, plan, status, billing_cycle, amount,
                start_date, end_date, notes, trial_used_at, updated_at
            )
            VALUES (
                %s, %s, 'trial', %s, 0,
                NOW(), NOW() + (%s || ' hours')::interval,
                %s, NOW(), NOW()
            )
            ON CONFLICT (user_id) DO UPDATE SET
                plan = EXCLUDED.plan,
                status = 'trial',
                billing_cycle = EXCLUDED.billing_cycle,
                amount = 0,
                start_date = NOW(),
                end_date = NOW() + (%s || ' hours')::interval,
                notes = EXCLUDED.notes,
                trial_used_at = COALESCE(user_subscriptions.trial_used_at, NOW()),
                updated_at = NOW()
            RETURNING end_date
        """, (
            user_id,
            plan["name"],
            option["id"],
            str(duration_hours),
            "Promotional paywall trial",
            str(duration_hours),
        ))
        trial_end = cur.fetchone()["end_date"]
        cur.execute("""
            INSERT INTO payment_events (user_id, event_type, plan, amount, billing_cycle, status, metadata)
            VALUES (%s, 'trial_started', %s, 0, %s, 'success', %s::jsonb)
        """, (
            user_id,
            plan["name"],
            option["id"],
            json.dumps({"duration_hours": duration_hours}),
        ))
        conn.commit()
        return {
            "plan": plan["name"],
            "status": "trial",
            "billing_cycle": option["id"],
            "trial_ends_at": trial_end.isoformat() if trial_end else None,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def get_revenue_stats():
    """Calculate MRR, ARR, daily/quarterly revenue, churn, ARPU from Supabase data."""
    conn = get_db()
    try:
        cur = conn.cursor()

        # MRR — sum monthly-equivalent of all active paid subscriptions
        cur.execute("""
            SELECT s.plan, s.billing_cycle, pp.*
            FROM user_subscriptions s
            JOIN pricing_plans pp ON pp.name = s.plan
            WHERE s.plan != 'free' AND s.status = 'active'
        """)
        mrr = round(sum(_monthly_equivalent_from_plan(row, row.get("billing_cycle")) for row in cur.fetchall()), 2)
        arr = mrr * 12

        # Paid user count
        cur.execute("SELECT COUNT(*) AS cnt FROM user_subscriptions WHERE plan != 'free' AND status = 'active'")
        paid_users = cur.fetchone()["cnt"]

        # Total users
        cur.execute("SELECT COUNT(*) AS cnt FROM auth.users WHERE deleted_at IS NULL")
        total_users = cur.fetchone()["cnt"]

        # ARPU
        arpu = mrr / total_users if total_users > 0 else 0

        # Churn (cancelled in last 30 days / total paid start of period)
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM payment_events
            WHERE event_type = 'subscription_cancel' AND created_at > NOW() - INTERVAL '30 days'
        """)
        cancellations_30d = cur.fetchone()["cnt"]

        # Daily revenue last 30 days
        cur.execute("""
            SELECT DATE(created_at) AS day, SUM(amount) AS total
            FROM payment_events
            WHERE status = 'success' AND event_type = 'payment'
              AND created_at > NOW() - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY day
        """)
        daily = [{"day": r["day"].isoformat(), "revenue": float(r["total"])} for r in cur.fetchall()]

        # Fill empty days with zero
        from datetime import date, timedelta
        today = date.today()
        day_map = {d["day"]: d["revenue"] for d in daily}
        daily_filled = []
        for i in range(30):
            d = (today - timedelta(days=29 - i)).isoformat()
            daily_filled.append({"day": d, "revenue": day_map.get(d, 0)})

        # Quarterly revenue
        cur.execute("""
            SELECT EXTRACT(YEAR FROM created_at)::int AS yr,
                   EXTRACT(QUARTER FROM created_at)::int AS qtr,
                   SUM(amount) AS total,
                   COUNT(DISTINCT user_id) AS users
            FROM payment_events
            WHERE status = 'success' AND event_type = 'payment'
            GROUP BY yr, qtr ORDER BY yr, qtr
        """)
        quarterly = [{"quarter": f"Q{r['qtr']} {r['yr']}", "revenue": float(r["total"]),
                       "users": r["users"]} for r in cur.fetchall()]

        # Total revenue all time
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM payment_events WHERE status = 'success' AND event_type = 'payment'")
        total_revenue = float(cur.fetchone()["total"])

        # Recent payments
        cur.execute("""
            SELECT pe.id, pe.event_type, pe.plan, pe.amount, pe.currency,
                   pe.billing_cycle, pe.status, pe.razorpay_payment_id,
                   pe.created_at, u.email, p.display_name
            FROM payment_events pe
            JOIN auth.users u ON u.id = pe.user_id
            LEFT JOIN user_profiles p ON p.user_id = pe.user_id
            ORDER BY pe.created_at DESC LIMIT 20
        """)
        recent_payments = []
        for r in cur.fetchall():
            recent_payments.append({
                "id": r["id"], "event_type": r["event_type"], "plan": r["plan"],
                "amount": float(r["amount"]), "currency": r["currency"],
                "billing_cycle": r["billing_cycle"], "status": r["status"],
                "razorpay_id": r["razorpay_payment_id"],
                "email": r["email"], "display_name": r["display_name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })

        return {
            "mrr": mrr, "arr": arr, "paid_users": paid_users,
            "total_users": total_users, "arpu": round(arpu, 2),
            "total_revenue": total_revenue,
            "cancellations_30d": cancellations_30d,
            "churn_rate": round(cancellations_30d / max(paid_users, 1) * 100, 1),
            "daily_revenue": daily_filled,
            "quarterly_revenue": quarterly,
            "recent_payments": recent_payments,
        }
    finally:
        close_db(conn)


def _fetch_pricing_plans(cur, active_only=False):
    if active_only:
        cur.execute("SELECT * FROM pricing_plans WHERE is_active = true ORDER BY sort_order")
    else:
        cur.execute("SELECT * FROM pricing_plans ORDER BY sort_order")
    return [_serialize_pricing_plan(r) for r in cur.fetchall()]


def get_pricing_plans(active_only=False):
    if active_only:
        cached = _pricing_cache_get("plans_active")
        if cached is not None:
            return cached
    conn = get_db()
    try:
        plans = _fetch_pricing_plans(conn.cursor(), active_only=active_only)
        if active_only:
            _pricing_cache_set("plans_active", plans)
        return plans
    finally:
        close_db(conn)


def upsert_pricing_plan(plan_id=None, **kwargs):
    allowed = {"name", "display_name", "price_monthly", "price_yearly", "features", "is_active", "sort_order",
               "razorpay_plan_id_monthly", "razorpay_plan_id_yearly",
               "trial_days", "badge", "description", "highlight",
               "billing_options", "cta_label", "icon"}
    conn = get_db()
    try:
        cur = conn.cursor()
        existing = None
        if plan_id:
            cur.execute("SELECT * FROM pricing_plans WHERE id = %s", (plan_id,))
            existing = cur.fetchone()
            if not existing:
                raise Exception("Plan not found")

        fallback_monthly = kwargs.get("price_monthly")
        fallback_yearly = kwargs.get("price_yearly")
        if fallback_monthly is None and existing:
            fallback_monthly = existing.get("price_monthly")
        if fallback_yearly is None and existing:
            fallback_yearly = existing.get("price_yearly")

        billing_options = kwargs.get("billing_options")
        if billing_options is not None or not plan_id:
            normalized_options = normalize_billing_options(billing_options, fallback_monthly or 0, fallback_yearly or 0)
            if not normalized_options:
                normalized_options = normalize_billing_options(None, fallback_monthly or 0, fallback_yearly or 0)
            kwargs["billing_options"] = normalized_options
            kwargs["price_monthly"], kwargs["price_yearly"] = _legacy_prices_from_billing(
                normalized_options,
                fallback_monthly or 0,
                fallback_yearly or 0,
            )
            monthly_opt = next((item for item in normalized_options if item["months"] == 1), None)
            yearly_opt = next((item for item in normalized_options if item["months"] == 12), None)
            kwargs["razorpay_plan_id_monthly"] = monthly_opt.get("razorpay_plan_id") if monthly_opt else None
            kwargs["razorpay_plan_id_yearly"] = yearly_opt.get("razorpay_plan_id") if yearly_opt else None

        if plan_id:
            sets, vals = ["updated_at = NOW()"], []
            for k, v in kwargs.items():
                if k in allowed and v is not None:
                    if k in {"features", "billing_options"}:
                        sets.append(f"{k} = %s::jsonb")
                        vals.append(json.dumps(v))
                    else:
                        sets.append(f"{k} = %s")
                        vals.append(v)
            vals.append(plan_id)
            cur.execute(f"UPDATE pricing_plans SET {', '.join(sets)} WHERE id = %s", vals)
        else:
            cur.execute("""
                INSERT INTO pricing_plans (
                    name, display_name, price_monthly, price_yearly, features, is_active, sort_order,
                    trial_days, badge, description, highlight, billing_options, cta_label, icon
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                RETURNING id
            """, (
                kwargs.get("name"),
                kwargs.get("display_name"),
                kwargs.get("price_monthly", 0),
                kwargs.get("price_yearly", 0),
                json.dumps(kwargs.get("features", [])),
                kwargs.get("is_active", True),
                kwargs.get("sort_order", 0),
                kwargs.get("trial_days", 0),
                kwargs.get("badge"),
                kwargs.get("description"),
                kwargs.get("highlight", False),
                json.dumps(kwargs.get("billing_options", normalize_billing_options(None, kwargs.get("price_monthly", 0), kwargs.get("price_yearly", 0)))),
                kwargs.get("cta_label"),
                kwargs.get("icon"),
            ))
        conn.commit()
        _pricing_cache_invalidate("plans_active")
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def delete_pricing_plan(plan_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE pricing_plans SET is_active = false, updated_at = NOW() WHERE id = %s", (plan_id,))
        conn.commit()
        _pricing_cache_invalidate("plans_active")
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


# ── Coupon System ─────────────────────────────────────────────


def create_coupon(code, name, discount_type, discount_value, max_uses=None,
                  max_uses_per_user=1, applies_to=None, billing_cycles=None,
                  valid_from=None, valid_until=None, created_by=None, metadata=None):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO coupons (code, name, discount_type, discount_value, max_uses,
                max_uses_per_user, applies_to, billing_cycles, valid_from, valid_until,
                created_by, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb)
            RETURNING id
        """, (
            code.upper().strip(), name, discount_type, float(discount_value),
            max_uses, max_uses_per_user,
            json.dumps(applies_to or ["pro", "premium"]),
            json.dumps(billing_cycles or ["monthly", "quarterly", "half_yearly", "yearly"]),
            valid_from, valid_until, created_by,
            json.dumps(metadata or {}),
        ))
        conn.commit()
        return cur.fetchone()["id"]
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def get_coupons():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.*,
                   (SELECT COUNT(*) FROM coupon_redemptions WHERE coupon_id = c.id) AS total_redemptions,
                   (SELECT COALESCE(SUM(discount_amount), 0) FROM coupon_redemptions WHERE coupon_id = c.id) AS total_discount_given
            FROM coupons c ORDER BY c.created_at DESC
        """)
        coupons = []
        for r in cur.fetchall():
            coupons.append({
                "id": r["id"], "code": r["code"], "name": r["name"],
                "discount_type": r["discount_type"],
                "discount_value": float(r["discount_value"]),
                "max_uses": r["max_uses"], "used_count": r["used_count"],
                "max_uses_per_user": r["max_uses_per_user"],
                "applies_to": r["applies_to"], "billing_cycles": r["billing_cycles"],
                "valid_from": r["valid_from"].isoformat() if r["valid_from"] else None,
                "valid_until": r["valid_until"].isoformat() if r["valid_until"] else None,
                "is_active": r["is_active"],
                "total_redemptions": r["total_redemptions"],
                "total_discount_given": float(r["total_discount_given"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })
        return coupons
    finally:
        close_db(conn)


def update_coupon(coupon_id, **kwargs):
    allowed = {"code", "name", "discount_type", "discount_value", "max_uses",
               "max_uses_per_user", "applies_to", "billing_cycles",
               "valid_from", "valid_until", "is_active", "metadata"}
    sets, vals = ["updated_at = NOW()"], []
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            if k in ("applies_to", "billing_cycles", "metadata"):
                sets.append(f"{k} = %s::jsonb")
                vals.append(json.dumps(v))
            elif k == "code":
                sets.append(f"{k} = %s")
                vals.append(v.upper().strip())
            else:
                sets.append(f"{k} = %s")
                vals.append(v)
    vals.append(coupon_id)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE coupons SET {', '.join(sets)} WHERE id = %s", vals)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def deactivate_coupon(coupon_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE coupons SET is_active = false, updated_at = NOW() WHERE id = %s", (coupon_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def validate_coupon(code, plan=None, billing_cycle=None):
    """Validate a coupon code. Returns discount info or raises Exception."""
    conn = get_db()
    try:
        cur = conn.cursor()
        plan = (plan or "").strip() or None
        billing_cycle = _billing_cycle_id(billing_cycle) or None
        cur.execute("SELECT * FROM coupons WHERE code = %s", (code.upper().strip(),))
        c = cur.fetchone()
        if not c:
            raise Exception("Invalid coupon code")
        if not c["is_active"]:
            raise Exception("This coupon is no longer active")
        if c["valid_until"] and c["valid_until"].replace(tzinfo=None) < __import__("datetime").datetime.utcnow():
            raise Exception("This coupon has expired")
        if c["max_uses"] and c["used_count"] >= c["max_uses"]:
            raise Exception("This coupon has reached its usage limit")
        applies_to = c["applies_to"] or []
        allowed_cycles = [_billing_cycle_id(cycle) for cycle in (c["billing_cycles"] or [])]

        if plan and plan not in applies_to:
            raise Exception(f"This coupon doesn't apply to the {plan} plan")
        if billing_cycle and billing_cycle not in allowed_cycles:
            raise Exception(f"This coupon doesn't apply to {billing_cycle} billing")

        # Calculate discount preview for all applicable plans and enabled billing options
        previews = []
        for pricing_plan in get_pricing_plans(active_only=True):
            if applies_to and pricing_plan["name"] not in applies_to:
                continue
            for option in pricing_plan["billing_options"]:
                option_cycle = _billing_cycle_id(option.get("id"))
                if not option.get("enabled"):
                    continue
                if allowed_cycles and option_cycle not in allowed_cycles:
                    continue
                price = float(option.get("price") or 0)
                if price <= 0:
                    continue
                if c["discount_type"] == "percent":
                    discount = round(price * float(c["discount_value"]) / 100, 2)
                else:
                    discount = min(float(c["discount_value"]), price)
                previews.append({
                    "plan": pricing_plan["name"],
                    "plan_label": pricing_plan["display_name"],
                    "cycle": option_cycle,
                    "cycle_label": option.get("label"),
                    "original": price,
                    "discount": discount,
                    "final": round(price - discount, 2),
                })

        return {
            "valid": True, "code": c["code"], "name": c["name"],
            "discount_type": c["discount_type"],
            "discount_value": float(c["discount_value"]),
            "previews": previews,
        }
    finally:
        close_db(conn)


def redeem_coupon(code, user_id, plan, original_amount):
    """Redeem a coupon — increments used_count, logs redemption, returns final amount."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM coupons WHERE code = %s AND is_active = true", (code.upper().strip(),))
        c = cur.fetchone()
        if not c:
            raise Exception("Invalid coupon")

        # Check per-user limit
        cur.execute("SELECT COUNT(*) AS cnt FROM coupon_redemptions WHERE coupon_id = %s AND user_id = %s", (c["id"], user_id))
        if cur.fetchone()["cnt"] >= (c["max_uses_per_user"] or 1):
            raise Exception("You've already used this coupon")

        if c["discount_type"] == "percent":
            discount = round(float(original_amount) * float(c["discount_value"]) / 100, 2)
        else:
            discount = min(float(c["discount_value"]), float(original_amount))
        final = round(float(original_amount) - discount, 2)

        cur.execute("UPDATE coupons SET used_count = used_count + 1, updated_at = NOW() WHERE id = %s", (c["id"],))
        cur.execute("""
            INSERT INTO coupon_redemptions (coupon_id, user_id, plan, original_amount, discount_amount, final_amount)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (c["id"], user_id, plan, original_amount, discount, final))
        conn.commit()
        return {"original": float(original_amount), "discount": discount, "final": final, "coupon": c["code"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def get_activity_log(limit=50):
    """Fetch recent admin activity log entries."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT a.id, a.action, a.target_user_id, a.details, a.created_at,
                   p.display_name AS admin_name, au.email AS admin_email
            FROM admin_activity_log a
            LEFT JOIN user_profiles p ON p.user_id = a.admin_user_id
            LEFT JOIN auth.users au ON au.id = a.admin_user_id
            ORDER BY a.created_at DESC
            LIMIT %s
        """, (limit,))
        entries = []
        for r in cur.fetchall():
            entries.append({
                "id": r["id"],
                "action": r["action"],
                "target_user_id": str(r["target_user_id"]) if r["target_user_id"] else None,
                "details": r["details"],
                "admin_name": r["admin_name"] or r["admin_email"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            })
        return entries
    finally:
        close_db(conn)
