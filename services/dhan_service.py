"""
Dhan Partner API client.

Handles three things:
  1. Partner consent flow — generate consent → user logs in on Dhan → consume consent
  2. Token storage in dhan_tokens (per-admin)
  3. Authenticated request helper that:
       - reads access_token from DB
       - falls back to VM proxy for order endpoints (which need a whitelisted static IP)
       - lazily refreshes on 401

Order endpoints that REQUIRE the whitelisted IP go through DHAN_ORDER_PROXY_URL
(a tiny Flask app on the data-pipeline VM whose egress IP is registered with Dhan).
Read-only endpoints (holdings, positions, funds, market data) hit api.dhan.co directly
from Cloud Run — Dhan does not enforce IP whitelisting on those.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from database.database import get_db, close_db


DHAN_API_BASE = "https://api.dhan.co/v2"
DHAN_AUTH_BASE = "https://auth.dhan.co"

PARTNER_ID = os.getenv("DHAN_PARTNER_ID", "").strip()
PARTNER_SECRET = os.getenv("DHAN_PARTNER_SECRET", "").strip()

# URL of the order-proxy endpoint running on the whitelisted VM.
# e.g. https://pipeline.valvointelligence.com/dhan-proxy/orders
ORDER_PROXY_URL = os.getenv("DHAN_ORDER_PROXY_URL", "").strip()
ORDER_PROXY_SECRET = os.getenv("DHAN_ORDER_PROXY_SECRET", "").strip()

# Where Dhan should send the user back after they enter credentials + 2FA.
# Must be the public URL of the /api/dhan/consent/callback Flask route.
CONSENT_REDIRECT_URI = os.getenv("DHAN_CONSENT_REDIRECT_URI", "").strip()

REQUEST_TIMEOUT = 20


# ── Partner consent flow ─────────────────────────────────────────────────────


def partner_credentials_present() -> bool:
    return bool(PARTNER_ID and PARTNER_SECRET)


def generate_consent(user_id: str) -> dict:
    """
    Step 1 of the partner flow.

    POST auth.dhan.co/partner/generate-consent with partner headers → returns consentId.
    We persist consent_id ↔ user_id so the callback knows which Supabase user this is for.
    """
    if not partner_credentials_present():
        raise RuntimeError("DHAN_PARTNER_ID / DHAN_PARTNER_SECRET not configured")

    resp = requests.post(
        f"{DHAN_AUTH_BASE}/partner/generate-consent",
        headers={
            "partner_id": PARTNER_ID,
            "partner_secret": PARTNER_SECRET,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    consent_id = data.get("consentId") or data.get("consent_id")
    if not consent_id:
        raise RuntimeError(f"Dhan generate-consent returned no consentId: {data}")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO dhan_consent_state (consent_id, user_id)
            VALUES (%s, %s)
            ON CONFLICT (consent_id) DO UPDATE SET user_id = EXCLUDED.user_id
        """, (consent_id, user_id))
        conn.commit()
    finally:
        close_db(conn)

    login_url = f"{DHAN_AUTH_BASE}/consent-login?consentId={consent_id}"
    return {"consent_id": consent_id, "login_url": login_url}


def consume_consent(token_id: str, consent_id_hint: str | None = None) -> dict:
    """
    Step 3 of the partner flow.

    Dhan redirects user back with ?tokenId=...&consentId=... after they log in.
    We POST tokenId to consume-consent → get the user's access_token + dhanClientId.
    Dhan's consume-consent response sometimes omits consentId, so the frontend
    forwards it from the redirect URL as consent_id_hint to look up which
    Supabase user this consent originally belonged to.
    """
    if not partner_credentials_present():
        raise RuntimeError("DHAN_PARTNER_ID / DHAN_PARTNER_SECRET not configured")

    resp = requests.post(
        f"{DHAN_AUTH_BASE}/partner/consume-consent",
        headers={
            "partner_id": PARTNER_ID,
            "partner_secret": PARTNER_SECRET,
        },
        params={"tokenId": token_id},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    access_token = data.get("accessToken") or data.get("access_token")
    dhan_client_id = data.get("dhanClientId") or data.get("dhan_client_id")
    consent_id = (
        data.get("consentId") or data.get("consent_id") or consent_id_hint
    )
    if not access_token or not dhan_client_id:
        raise RuntimeError(f"Dhan consume-consent missing fields: {data}")
    if not consent_id:
        raise RuntimeError("consent_id missing — Dhan response had no consentId and no hint provided")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM dhan_consent_state WHERE consent_id = %s", (consent_id,))
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"Unknown consentId {consent_id} (no user mapping)")
        user_id = row["user_id"]

        # Dhan partner tokens are valid for 24h. Store with a small safety margin.
        expires_at = datetime.now(timezone.utc) + timedelta(hours=23, minutes=30)

        cur.execute("""
            INSERT INTO dhan_tokens (user_id, dhan_client_id, access_token, expires_at, last_refreshed)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                dhan_client_id = EXCLUDED.dhan_client_id,
                access_token   = EXCLUDED.access_token,
                expires_at     = EXCLUDED.expires_at,
                last_refreshed = NOW(),
                updated_at     = NOW()
        """, (user_id, dhan_client_id, access_token, expires_at))

        cur.execute("""
            UPDATE dhan_consent_state SET consumed_at = NOW() WHERE consent_id = %s
        """, (consent_id,))
        conn.commit()
    finally:
        close_db(conn)

    return {"user_id": user_id, "dhan_client_id": dhan_client_id, "expires_at": expires_at.isoformat()}


def get_token(user_id: str) -> dict | None:
    """Return {access_token, dhan_client_id, expires_at} or None if not connected."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT access_token, dhan_client_id, expires_at
            FROM dhan_tokens WHERE user_id = %s
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        close_db(conn)


def disconnect(user_id: str) -> bool:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dhan_tokens WHERE user_id = %s", (user_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        close_db(conn)


# ── Authenticated request helpers ────────────────────────────────────────────


class DhanNotConnected(Exception):
    """Raised when the admin hasn't completed the partner consent flow yet."""


class DhanApiError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"Dhan API {status}: {body}")
        self.status = status
        self.body = body


def _headers(token: dict) -> dict:
    return {
        "access-token": token["access_token"],
        "client-id": token["dhan_client_id"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def call(user_id: str, method: str, path: str, *, json: dict | None = None,
         params: dict | None = None, via_proxy: bool = False) -> Any:
    """
    Make an authenticated Dhan API call as the given admin user.

    via_proxy=True routes the request through the whitelisted VM (required for
    order placement / modification / cancellation since those endpoints reject
    Cloud Run's dynamic egress IPs).
    """
    token = get_token(user_id)
    if not token:
        raise DhanNotConnected("User has no Dhan token; complete the consent flow")

    if via_proxy:
        if not ORDER_PROXY_URL or not ORDER_PROXY_SECRET:
            raise RuntimeError("DHAN_ORDER_PROXY_URL / DHAN_ORDER_PROXY_SECRET not configured")
        resp = requests.post(
            ORDER_PROXY_URL,
            headers={
                "X-Proxy-Secret": ORDER_PROXY_SECRET,
                "Content-Type": "application/json",
            },
            json={
                "method": method.upper(),
                "path": path,
                "json": json,
                "params": params,
                "access_token": token["access_token"],
                "dhan_client_id": token["dhan_client_id"],
            },
            timeout=REQUEST_TIMEOUT,
        )
    else:
        resp = requests.request(
            method.upper(),
            f"{DHAN_API_BASE}{path}",
            headers=_headers(token),
            json=json,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

    if resp.status_code == 401:
        # Token expired or revoked — wipe so the user gets prompted to reconnect.
        disconnect(user_id)
        raise DhanNotConnected("Dhan token rejected (401) — reconnect required")

    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise DhanApiError(resp.status_code, body)

    if not resp.content:
        return None
    try:
        return resp.json()
    except Exception:
        return resp.text


# ── Valvo-friendly order placement ───────────────────────────────────────────


_EXCHANGE_SEGMENT = {
    "NSE": "NSE_EQ",
    "BSE": "BSE_EQ",
    "NSE_EQ": "NSE_EQ",
    "BSE_EQ": "BSE_EQ",
}

_VALID_PRODUCTS = {"CNC", "INTRADAY", "MARGIN", "MTF", "CO", "BO"}
_VALID_ORDER_TYPES = {"MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"}
_VALID_VALIDITY = {"DAY", "IOC"}


def resolve_symbol(symbol: str, exchange: str = "NSE") -> dict | None:
    """Look up security_id + canonical metadata for a symbol from stock_universe."""
    if not symbol:
        return None
    sym = symbol.strip().upper()
    exch = (exchange or "NSE").strip().upper()
    is_nse = exch in {"NSE", "NSE_EQ"}
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT security_id, symbol, company_name, exchange, isin, bse_code
            FROM stock_universe
            WHERE symbol = %s AND COALESCE(is_active, true) = true
            ORDER BY (CASE WHEN %s THEN is_nse_listed ELSE is_bse_listed END) DESC NULLS LAST
            LIMIT 1
        """, (sym, is_nse))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        close_db(conn)


def place_simple_order(user_id: str, *, symbol: str, exchange: str, side: str,
                       quantity: int, order_type: str = "MARKET",
                       product: str = "CNC", price: float | None = None,
                       trigger_price: float | None = None, validity: str = "DAY",
                       disclosed_quantity: int = 0, after_market: bool = False,
                       amo_time: str = "OPEN") -> Any:
    """
    Valvo-friendly order placement. Resolves symbol→security_id, builds the
    Dhan API body, and routes through the VM proxy.
    """
    side_norm = (side or "").strip().upper()
    if side_norm not in {"BUY", "SELL"}:
        raise ValueError(f"side must be BUY or SELL (got {side!r})")

    order_type_norm = (order_type or "MARKET").strip().upper()
    if order_type_norm not in _VALID_ORDER_TYPES:
        raise ValueError(f"order_type must be one of {_VALID_ORDER_TYPES} (got {order_type!r})")

    product_norm = (product or "CNC").strip().upper()
    if product_norm not in _VALID_PRODUCTS:
        raise ValueError(f"product must be one of {_VALID_PRODUCTS} (got {product!r})")

    validity_norm = (validity or "DAY").strip().upper()
    if validity_norm not in _VALID_VALIDITY:
        raise ValueError(f"validity must be one of {_VALID_VALIDITY} (got {validity!r})")

    if quantity is None or int(quantity) <= 0:
        raise ValueError("quantity must be a positive integer")

    exch_norm = (exchange or "NSE").strip().upper()
    segment = _EXCHANGE_SEGMENT.get(exch_norm)
    if not segment:
        raise ValueError(f"exchange must be NSE or BSE (got {exchange!r})")

    resolved = resolve_symbol(symbol, exch_norm)
    if not resolved or not resolved.get("security_id"):
        raise ValueError(f"Unknown symbol {symbol!r} on {exch_norm} (no security_id in stock_universe)")

    token = get_token(user_id)
    if not token:
        raise DhanNotConnected("User has no Dhan token; complete the consent flow")

    body: dict = {
        "dhanClientId": token["dhan_client_id"],
        "transactionType": side_norm,
        "exchangeSegment": segment,
        "productType": product_norm,
        "orderType": order_type_norm,
        "validity": validity_norm,
        "securityId": str(resolved["security_id"]),
        "quantity": int(quantity),
        "disclosedQuantity": int(disclosed_quantity or 0),
        "afterMarketOrder": bool(after_market),
    }
    if after_market:
        amo_norm = (amo_time or "OPEN").strip().upper()
        if amo_norm not in {"OPEN", "OPEN_30", "OPEN_60", "OPEN_90", "PRE_OPEN"}:
            raise ValueError(f"amo_time must be OPEN/OPEN_30/OPEN_60/OPEN_90/PRE_OPEN (got {amo_time!r})")
        body["amoTime"] = amo_norm
    if order_type_norm in {"LIMIT", "STOP_LOSS"} and price is not None:
        body["price"] = float(price)
    if order_type_norm in {"STOP_LOSS", "STOP_LOSS_MARKET"} and trigger_price is not None:
        body["triggerPrice"] = float(trigger_price)

    return call(user_id, "POST", "/orders", json=body, via_proxy=True)
