import hashlib
import os
import re

from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


_TRUE_VALUES = {"1", "true", "yes", "on"}
_SAFE_HEADER_RE = re.compile(r"[^a-zA-Z0-9_.:-]")


def _env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _env_limits(name, default):
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _short_hash(value):
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:24]


def _safe_header(value, max_len=96):
    value = (value or "").strip()[:max_len]
    return _SAFE_HEADER_RE.sub("_", value)


def client_rate_limit_key():
    """Bucket requests by browser/device.

    The frontend sends X-Client-Device-ID from localStorage, so each browser
    installation gets its own rate-limit bucket. Non-browser/API clients that do
    not send the header fall back to IP + user-agent because the server has no
    stronger device signal for those requests.
    """
    device_id = _safe_header(request.headers.get("X-Client-Device-ID"))
    if device_id:
        return f"device:{_short_hash(device_id)}"

    ip = get_remote_address() or "unknown"
    user_agent = request.headers.get("User-Agent", "")[:512]
    fallback = _short_hash(f"{ip}|{user_agent}")
    return f"device-fallback:{fallback}"


limiter = Limiter(
    key_func=client_rate_limit_key,
    default_limits=_env_limits(
        "RATELIMIT_DEFAULTS",
        "120 per minute,1200 per hour",
    ),
    # Applies across all endpoints, even routes that define narrower local
    # limits. The first limit is the short burst guard for rapid-fire clients.
    application_limits=_env_limits(
        "RATELIMIT_APPLICATION_LIMITS",
        "100 per 10 seconds,300 per minute,2000 per hour",
    ),
    strategy=os.getenv("RATELIMIT_STRATEGY", "moving-window"),
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    headers_enabled=_env_bool("RATELIMIT_HEADERS_ENABLED", True),
    key_prefix=os.getenv("RATELIMIT_KEY_PREFIX", "valvo-api"),
    enabled=_env_bool("RATELIMIT_ENABLED", True),
)
