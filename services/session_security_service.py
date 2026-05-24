import json
from datetime import datetime, timezone

from database.database import close_db, get_db

DEFAULT_SESSION_SECURITY_CONFIG = {
    "enabled": True,
    "max_devices": 3,
    "logout_all_on_limit": True,
    "alert_message": "Maximum devices reached. All active sessions were logged out for security. Please sign in again.",
}

GLOBAL_LOGOUT_CONFIG_KEY = "session_security_global_logout"

REASON_META = {
    "forced_logout": {
        "code": "forced_logout",
        "message": "Your session was closed from backend. Please sign in again.",
    },
    "security_logout_all": {
        "code": "security_logout_all",
        "message": "All users were signed out for a security refresh. Please sign in again.",
    },
    "maximum_devices_reached": {
        "code": "maximum_devices_reached",
        "message": DEFAULT_SESSION_SECURITY_CONFIG["alert_message"],
    },
    "session_invalid": {
        "code": "session_invalid",
        "message": "This session is no longer active. Please sign in again.",
    },
}


class SessionAccessDenied(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _utc_now():
    return datetime.now(timezone.utc)


def _to_utc(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _token_iat(payload):
    raw = payload.get("iat") or payload.get("auth_time")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except Exception:
        return None


def describe_user_agent(user_agent):
    ua = user_agent or ""
    lowered = ua.lower()

    device = "Desktop"
    if any(token in lowered for token in ["iphone", "android", "mobile"]):
        device = "Mobile"
    elif any(token in lowered for token in ["ipad", "tablet"]):
        device = "Tablet"

    browser = "Unknown"
    for token, label in [
        ("edg", "Edge"),
        ("chrome", "Chrome"),
        ("safari", "Safari"),
        ("firefox", "Firefox"),
    ]:
        if token in lowered:
            browser = label
            break

    if browser == "Safari" and "chrome" in lowered:
        browser = "Chrome"

    label = f"{device} · {browser}"
    return {
        "device": device,
        "browser": browser,
        "label": label,
    }


def _reason_payload(reason, message=None):
    base = REASON_META.get(reason, REASON_META["session_invalid"])
    return {
        "reason": reason,
        "code": base["code"],
        "message": message or base["message"],
    }


def _read_app_config(cur, key):
    cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
    row = cur.fetchone()
    return (row or {}).get("value") if row else None


def _write_app_config(cur, key, value):
    cur.execute(
        """
        INSERT INTO app_config (key, value, updated_at)
        VALUES (%s, %s::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = NOW()
        """,
        (key, json.dumps(value)),
    )


def _security_config_from_cursor(cur):
    raw = _read_app_config(cur, "session_security_config") or {}
    config = {**DEFAULT_SESSION_SECURITY_CONFIG, **(raw or {})}
    try:
        config["max_devices"] = max(1, min(int(config.get("max_devices") or 3), 10))
    except Exception:
        config["max_devices"] = DEFAULT_SESSION_SECURITY_CONFIG["max_devices"]
    config["enabled"] = bool(config.get("enabled", True))
    config["logout_all_on_limit"] = bool(config.get("logout_all_on_limit", True))
    config["alert_message"] = str(config.get("alert_message") or DEFAULT_SESSION_SECURITY_CONFIG["alert_message"]).strip() or DEFAULT_SESSION_SECURITY_CONFIG["alert_message"]
    return config


def _get_user_role(cur, user_id):
    cur.execute(
        "SELECT role FROM user_profiles WHERE user_id = %s::uuid",
        (user_id,),
    )
    row = cur.fetchone()
    return (row or {}).get("role") or "user"


def get_session_security_config():
    conn = get_db()
    try:
        cur = conn.cursor()
        return _security_config_from_cursor(cur)
    finally:
        close_db(conn)


def save_session_security_config(data):
    incoming = data or {}
    config = {
        "enabled": bool(incoming.get("enabled", True)),
        "max_devices": max(1, min(int(incoming.get("max_devices") or DEFAULT_SESSION_SECURITY_CONFIG["max_devices"]), 10)),
        "logout_all_on_limit": bool(incoming.get("logout_all_on_limit", True)),
        "alert_message": str(incoming.get("alert_message") or DEFAULT_SESSION_SECURITY_CONFIG["alert_message"]).strip() or DEFAULT_SESSION_SECURITY_CONFIG["alert_message"],
    }

    conn = get_db()
    try:
        cur = conn.cursor()
        _write_app_config(cur, "session_security_config", config)
        conn.commit()
        return config
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def _clear_user_sessions(cur, user_id):
    cur.execute("DELETE FROM auth.refresh_tokens WHERE user_id = %s::uuid", (user_id,))
    cur.execute("DELETE FROM auth.sessions WHERE user_id = %s::uuid", (user_id,))


def _clear_all_sessions(cur):
    cur.execute("DELETE FROM auth.refresh_tokens")
    cur.execute("DELETE FROM auth.sessions")


def _set_user_logout_marker(cur, user_id, reason, message):
    payload = {
        "force_logout_after": _utc_now().isoformat(),
        "reason": reason,
        "message": message,
    }
    cur.execute(
        """
        INSERT INTO user_session_controls (user_id, force_logout_after, reason, message, updated_at)
        VALUES (%s::uuid, %s::timestamptz, %s, %s, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET force_logout_after = EXCLUDED.force_logout_after,
            reason = EXCLUDED.reason,
            message = EXCLUDED.message,
            updated_at = NOW()
        """,
        (user_id, payload["force_logout_after"], reason, message),
    )
    return payload


def _set_global_logout_marker(cur, reason, message):
    payload = {
        "at": _utc_now().isoformat(),
        "reason": reason,
        "message": message,
    }
    _write_app_config(cur, GLOBAL_LOGOUT_CONFIG_KEY, payload)
    return payload


def _get_user_logout_marker(cur, user_id):
    cur.execute(
        """
        SELECT force_logout_after, reason, message
        FROM user_session_controls
        WHERE user_id = %s::uuid
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "force_logout_after": _to_utc(row.get("force_logout_after")),
        "reason": row.get("reason") or "forced_logout",
        "message": row.get("message") or REASON_META["forced_logout"]["message"],
    }


def _get_global_logout_marker(cur):
    raw = _read_app_config(cur, GLOBAL_LOGOUT_CONFIG_KEY) or {}
    if not raw:
        return None
    return {
        "at": _to_utc(raw.get("at")),
        "reason": raw.get("reason") or "security_logout_all",
        "message": raw.get("message") or REASON_META["security_logout_all"]["message"],
    }


def force_logout_user_sessions(user_id, reason="forced_logout", message=None):
    notice = _reason_payload(reason, message)
    conn = get_db()
    try:
        cur = conn.cursor()
        _set_user_logout_marker(cur, user_id, reason, notice["message"])
        _clear_user_sessions(cur, user_id)
        conn.commit()
        return notice
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def force_logout_all_sessions(reason="security_logout_all", message=None):
    notice = _reason_payload(reason, message)
    conn = get_db()
    try:
        cur = conn.cursor()
        _set_global_logout_marker(cur, reason, notice["message"])
        _clear_all_sessions(cur)
        conn.commit()
        return notice
    except Exception:
        conn.rollback()
        raise
    finally:
        close_db(conn)


def list_active_sessions(limit=200):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.id, s.user_id, s.created_at, s.updated_at, s.user_agent, s.ip,
                   u.email, p.display_name
            FROM auth.sessions s
            JOIN auth.users u ON u.id = s.user_id
            LEFT JOIN user_profiles p ON p.user_id = s.user_id
            WHERE u.deleted_at IS NULL
            ORDER BY COALESCE(s.updated_at, s.created_at) DESC
            LIMIT %s
            """,
            (limit,),
        )
        sessions = []
        for row in cur.fetchall():
            meta = describe_user_agent(row.get("user_agent"))
            sessions.append({
                "session_id": str(row["id"]),
                "user_id": str(row["user_id"]),
                "email": row.get("email"),
                "display_name": row.get("display_name"),
                "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
                "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
                "user_agent": row.get("user_agent"),
                "ip": str(row["ip"]) if row.get("ip") else None,
                **meta,
            })
        return sessions
    finally:
        close_db(conn)


def get_session_security_overview(limit=120):
    conn = get_db()
    try:
        cur = conn.cursor()
        config = _security_config_from_cursor(cur)

        cur.execute(
            """
            SELECT s.user_id, u.email, p.display_name, COALESCE(p.role, 'user') AS role,
                   COUNT(*) AS active_devices,
                   MAX(COALESCE(s.updated_at, s.created_at)) AS last_seen
            FROM auth.sessions s
            JOIN auth.users u ON u.id = s.user_id
            LEFT JOIN user_profiles p ON p.user_id = s.user_id
            WHERE u.deleted_at IS NULL
            GROUP BY s.user_id, u.email, p.display_name, p.role
            ORDER BY active_devices DESC, last_seen DESC
            LIMIT %s
            """,
            (limit,),
        )
        users = []
        over_limit = []
        for row in cur.fetchall():
            item = {
                "user_id": str(row["user_id"]),
                "email": row.get("email"),
                "display_name": row.get("display_name"),
                "role": row.get("role") or "user",
                "active_devices": int(row.get("active_devices") or 0),
                "last_seen": row["last_seen"].isoformat() if row.get("last_seen") else None,
            }
            users.append(item)
            if item["role"] != "admin" and item["active_devices"] > config["max_devices"]:
                over_limit.append(item)

        recent_sessions = list_active_sessions(min(limit, 80))

        return {
            "config": config,
            "total_active_sessions": sum(item["active_devices"] for item in users),
            "users_with_sessions": len(users),
            "over_limit_users": over_limit,
            "users": users,
            "recent_sessions": recent_sessions,
        }
    finally:
        close_db(conn)


def enforce_session_access(user_id, payload):
    session_id = payload.get("session_id")
    token_iat = _token_iat(payload)

    conn = get_db()
    try:
        cur = conn.cursor()
        user_role = _get_user_role(cur, user_id)
        is_admin = user_role == "admin"

        global_marker = _get_global_logout_marker(cur)
        if global_marker and global_marker.get("at") and token_iat and token_iat <= global_marker["at"]:
            raise SessionAccessDenied(
                REASON_META["security_logout_all"]["code"],
                global_marker.get("message") or REASON_META["security_logout_all"]["message"],
            )

        user_marker = _get_user_logout_marker(cur, user_id)
        if user_marker and user_marker.get("force_logout_after") and token_iat and token_iat <= user_marker["force_logout_after"]:
            payload_meta = _reason_payload(user_marker.get("reason"), user_marker.get("message"))
            raise SessionAccessDenied(payload_meta["code"], payload_meta["message"])

        session_row_exists = True
        if session_id:
            cur.execute(
                "SELECT 1 FROM auth.sessions WHERE id = %s::uuid AND user_id = %s::uuid",
                (session_id, user_id),
            )
            session_row_exists = bool(cur.fetchone())

        config = _security_config_from_cursor(cur)
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM auth.sessions WHERE user_id = %s::uuid",
            (user_id,),
        )
        active_devices = int((cur.fetchone() or {}).get("cnt") or 0)

        # Treat missing auth.sessions rows as advisory only. Supabase access tokens can
        # remain valid briefly after a backing session row rotates or is cleaned up, and
        # forcing a logout here creates false sign-outs during normal app use.
        #
        # Also keep device-cap evaluation non-destructive inside the request path.
        # Session deletion is too disruptive to run during normal page loads and can
        # cascade into logout loops; explicit admin-driven force logout remains available.
        over_device_limit = (
            config["enabled"]
            and config["logout_all_on_limit"]
            and not is_admin
            and active_devices > config["max_devices"]
        )

        return {
            "session_id": str(session_id) if session_id else None,
            "session_row_exists": session_row_exists,
            "role": user_role,
            "active_devices": active_devices,
            "over_device_limit": over_device_limit,
            "config": config,
        }
    finally:
        close_db(conn)
