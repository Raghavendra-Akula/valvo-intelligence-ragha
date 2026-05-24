"""
Authentication Routes — Supabase Auth Integration
Verifies Supabase-issued JWTs using JWKS (asymmetric ES256 keys).
Users are created by admin in Supabase Dashboard (no signup endpoint).
"""

from flask import Blueprint, request, jsonify, g
from functools import wraps
import jwt
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
import os
import json
import time
import requests
from extensions import limiter

auth_bp = Blueprint("auth", __name__)

# Supabase JWKS endpoint for asymmetric key verification
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://sxyktzpiixmidlxxfgdd.supabase.co")
_jwks_url = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
_jwks_cache = {"keys": {}, "expires_at": 0.0}
_jwks_cache_ttl = 600

# Fallback: Legacy HS256 secret (for anon/service_role keys)
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")


def _load_jwks(force_refresh=False):
    now = time.time()
    if not force_refresh and _jwks_cache["keys"] and now < _jwks_cache["expires_at"]:
        return _jwks_cache["keys"]

    response = requests.get(_jwks_url, timeout=5)
    response.raise_for_status()
    payload = response.json() or {}

    keys = {}
    for jwk in payload.get("keys", []):
        kid = jwk.get("kid")
        kty = jwk.get("kty")
        if not kid or not kty:
            continue
        jwk_json = json.dumps(jwk)
        if kty == "EC":
            keys[kid] = ECAlgorithm.from_jwk(jwk_json)
        elif kty == "RSA":
            keys[kid] = RSAAlgorithm.from_jwk(jwk_json)

    if keys:
        _jwks_cache["keys"] = keys
        _jwks_cache["expires_at"] = now + _jwks_cache_ttl
    return _jwks_cache["keys"]


def _verify_supabase_jwt(token):
    """Verify a Supabase JWT — tries JWKS (ES256) first, falls back to HS256 legacy secret."""
    header = jwt.get_unverified_header(token)
    alg = header.get("alg", "ES256")
    kid = header.get("kid")

    if kid:
        for force_refresh in (False, True):
            try:
                signing_key = _load_jwks(force_refresh=force_refresh).get(kid)
                if signing_key is None:
                    continue
                return jwt.decode(
                    token,
                    signing_key,
                    algorithms=[alg],
                    audience="authenticated",
                )
            except Exception:
                if force_refresh:
                    break

    # Fallback: legacy HS256 symmetric secret
    if SUPABASE_JWT_SECRET:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
        return payload

    raise jwt.InvalidTokenError("Token verification failed")


def token_required(f):
    """Decorator — verifies Supabase JWT from Authorization: Bearer <token> header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Login required"}), 401

        token = auth_header.split(" ")[1]
        try:
            payload = _verify_supabase_jwt(token)
            g.user_id = payload.get("sub")
            g.user_email = payload.get("email", "")
            g.user_role = payload.get("role", "authenticated")
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Session expired, please login again"}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid token"}), 401

        return f(*args, **kwargs)
    return decorated


@auth_bp.route("/api/verify-token", methods=["GET"])
@limiter.limit("10 per minute")
def verify_token():
    """Check if a Supabase JWT is still valid. Used by frontend on app mount."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return jsonify({"valid": False}), 401

    token = auth_header.split(" ")[1]
    try:
        payload = _verify_supabase_jwt(token)
        return jsonify({
            "valid": True,
            "user_id": payload.get("sub"),
            "email": payload.get("email", ""),
        })
    except Exception:
        return jsonify({"valid": False}), 401
