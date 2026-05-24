"""
Backend step-up authentication helpers.

The main app login stays on the normal Supabase session.
Access to /backend requires an additional short-lived token minted only after
the user re-confirms their password.
"""
import os
from datetime import datetime, timedelta, timezone

import jwt
import requests

UTC = timezone.utc
BACKEND_ACCESS_AUDIENCE = "valvo-backend-dashboard"
BACKEND_ACCESS_ISSUER = "valvo-backend"


class BackendAccessError(Exception):
    pass


class BackendAccessExpired(BackendAccessError):
    pass


class BackendAccessInvalid(BackendAccessError):
    pass


class BackendPasswordInvalid(BackendAccessError):
    pass


def _env(*names, default=""):
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def _ttl_minutes():
    try:
        return max(5, min(int(_env("BACKEND_ACCESS_TTL_MINUTES", default="120")), 480))
    except (TypeError, ValueError):
        return 120


def _backend_access_secret():
    secret = _env(
        "BACKEND_ACCESS_SECRET",
        "SUPABASE_JWT_SECRET",
        "SUPABASE_SERVICE_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_KEY",
    )
    if not secret:
        raise BackendAccessError("Backend access secret is not configured")
    return secret


def _supabase_auth_config():
    supabase_url = _env("SUPABASE_URL", default="https://sxyktzpiixmidlxxfgdd.supabase.co")
    supabase_key = _env("SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        raise BackendAccessError("Supabase auth verification is not configured")
    return supabase_url.rstrip("/"), supabase_key


def verify_backend_password(email, password):
    if not (email or "").strip():
        raise BackendAccessError("Email missing from active session")
    if not (password or "").strip():
        raise BackendPasswordInvalid("Password is required")

    supabase_url, supabase_key = _supabase_auth_config()
    resp = requests.post(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
        },
        json={"email": email.strip(), "password": password},
        timeout=20,
    )
    if resp.status_code >= 400:
        raise BackendPasswordInvalid("Incorrect password")

    # Intentionally do not call Supabase logout here.
    # Logging out the verification session can revoke or disturb the user's
    # primary app session, which shows up as surprise logouts on the website.
    return True


def issue_backend_access_token(user_id, email):
    now = datetime.now(tz=UTC)
    exp = now + timedelta(minutes=_ttl_minutes())
    token = jwt.encode(
        {
            "sub": str(user_id),
            "email": email,
            "scope": "backend_dashboard",
            "iss": BACKEND_ACCESS_ISSUER,
            "aud": BACKEND_ACCESS_AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": exp,
        },
        _backend_access_secret(),
        algorithm="HS256",
    )
    return {
        "access_token": token,
        "expires_at": exp.isoformat(),
        "ttl_minutes": _ttl_minutes(),
    }


def verify_backend_access_token(token, *, expected_user_id=None):
    if not token:
        raise BackendAccessInvalid("Backend access token missing")
    try:
        payload = jwt.decode(
            token,
            _backend_access_secret(),
            algorithms=["HS256"],
            audience=BACKEND_ACCESS_AUDIENCE,
            issuer=BACKEND_ACCESS_ISSUER,
        )
    except jwt.ExpiredSignatureError as exc:
        raise BackendAccessExpired("Backend access token expired") from exc
    except Exception as exc:
        raise BackendAccessInvalid("Backend access token invalid") from exc

    if expected_user_id and str(payload.get("sub")) != str(expected_user_id):
        raise BackendAccessInvalid("Backend access token user mismatch")
    if payload.get("scope") != "backend_dashboard":
        raise BackendAccessInvalid("Backend access token scope invalid")
    return payload
