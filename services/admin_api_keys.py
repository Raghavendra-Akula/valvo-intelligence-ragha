"""Long-lived admin API keys for headless / CLI authentication.

Used by the Claude-Code research worker so it can call the deployed
backend over HTTPS from any environment (web Claude Code, codespace,
cloud VM) without needing direct Supabase credentials.

Plaintext format:  vk_<43 url-safe random chars>
Stored:            sha256(plaintext) hex + first 11 chars for display

Cross-mode safety:
  • Plaintext is NEVER stored. It's returned exactly once at create time.
  • Validation is constant-time-ish (sha256 lookup is O(1) via the index).
  • Revoking sets revoked_at; the partial index excludes revoked keys
    so validation stays a pure UNIQUE-index lookup.

Required schema: see Backend/database/migrations/2026_05_03_admin_api_keys.sql
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from database.database import close_db, get_db


_KEY_PREFIX = "vk_"
_KEY_PLAIN_LEN = 43          # url-safe base64 of 32 random bytes
_DISPLAY_PREFIX_LEN = 11     # "vk_" + first 8 chars


def _hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _gen_plaintext() -> str:
    """Generate a fresh `vk_<43 chars>` key. Random component is 256 bits."""
    body = secrets.token_urlsafe(32)  # ~43 chars of url-safe base64
    return f"{_KEY_PREFIX}{body[:_KEY_PLAIN_LEN]}"


def create_key(*, user_id: str, label: str, expires_at: Optional[str] = None) -> dict:
    """Mint a new API key for an admin user. Returns:

      {
        "id":           <bigint>,
        "label":        <str>,
        "key_prefix":   "vk_AbCdEf12",
        "plaintext":    "vk_AbCdEf12_full_secret…",   # SHOWN ONCE
        "created_at":   <iso>,
        "expires_at":   <iso|None>
      }

    The caller must surface `plaintext` to the user immediately and warn
    them it cannot be retrieved later.
    """
    plaintext = _gen_plaintext()
    key_hash = _hash_key(plaintext)
    key_prefix = plaintext[:_DISPLAY_PREFIX_LEN]

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO admin_api_keys (label, user_id, key_hash, key_prefix, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, label, key_prefix, created_at, expires_at
            """,
            (label.strip()[:100] or "unnamed", user_id, key_hash, key_prefix, expires_at),
        )
        row = cur.fetchone()
        conn.commit()
        return {
            "id":         row["id"],
            "label":      row["label"],
            "key_prefix": row["key_prefix"],
            "plaintext":  plaintext,
            "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
            "expires_at": row["expires_at"].isoformat() if row.get("expires_at") else None,
        }
    finally:
        close_db(conn)


def validate_key(plaintext: str) -> Optional[str]:
    """Look up an API key by plaintext. Returns the owning user_id if the
    key is valid (active, not revoked, not expired) — else None.

    Side effect: bumps last_used_at on hit (best-effort; ignored on error).
    """
    if not plaintext or not plaintext.startswith(_KEY_PREFIX):
        return None
    if len(plaintext) < len(_KEY_PREFIX) + 16:
        return None  # impossibly short — skip
    key_hash = _hash_key(plaintext)

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, user_id, expires_at
              FROM admin_api_keys
             WHERE key_hash = %s
               AND revoked_at IS NULL
            """,
            (key_hash,),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row.get("expires_at") is not None:
            cur.execute("SELECT %s::timestamptz < NOW() AS expired", (row["expires_at"],))
            exp_row = cur.fetchone()
            if exp_row and exp_row.get("expired"):
                return None

        # Best-effort heartbeat — don't fail validation if this hiccups
        try:
            cur.execute(
                "UPDATE admin_api_keys SET last_used_at = NOW() WHERE id = %s",
                (row["id"],),
            )
            conn.commit()
        except Exception:
            conn.rollback()

        return row["user_id"]
    except Exception:
        conn.rollback()
        return None
    finally:
        close_db(conn)


def list_keys(user_id: str) -> list[dict]:
    """List all keys belonging to a user. Plaintext is never returned."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, label, key_prefix, created_at, last_used_at,
                   revoked_at, expires_at
              FROM admin_api_keys
             WHERE user_id = %s
          ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return [
            {
                "id":           r["id"],
                "label":        r["label"],
                "key_prefix":   r["key_prefix"],
                "created_at":   r["created_at"].isoformat() if r.get("created_at") else None,
                "last_used_at": r["last_used_at"].isoformat() if r.get("last_used_at") else None,
                "revoked_at":   r["revoked_at"].isoformat() if r.get("revoked_at") else None,
                "expires_at":   r["expires_at"].isoformat() if r.get("expires_at") else None,
                "active":       r.get("revoked_at") is None,
            }
            for r in cur.fetchall()
        ]
    finally:
        close_db(conn)


def revoke_key(*, user_id: str, key_id: int) -> bool:
    """Mark a key revoked. Idempotent — returns True iff a row was updated."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE admin_api_keys
               SET revoked_at = NOW()
             WHERE id = %s
               AND user_id = %s
               AND revoked_at IS NULL
         RETURNING id
            """,
            (key_id, user_id),
        )
        row = cur.fetchone()
        conn.commit()
        return row is not None
    finally:
        close_db(conn)
