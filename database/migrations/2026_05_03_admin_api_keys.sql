-- ════════════════════════════════════════════════════════════════════
--  admin_api_keys — long-lived tokens that authenticate CLI workers
--  to the deployed backend over HTTPS.
--
--  Path: /api/admin/api-keys (POST mints a new key, GET lists, DELETE
--  revokes). The plaintext key is shown ONCE at creation time; the
--  server stores only sha256(key). On every request, app.py's
--  require_auth checks for `X-Admin-Token: vk_…` and falls back to
--  the existing Supabase-JWT path.
--
--  Used by scripts/research_worker_*.py + research_dossier.py +
--  research_save.py so the worker session can run anywhere with HTTPS
--  egress (web Claude Code, free Codespace, cheap cloud VM) without
--  needing direct Supabase credentials.
--
--  Idempotent — safe to re-run.
-- ════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS admin_api_keys (
    id            BIGSERIAL PRIMARY KEY,
    label         TEXT NOT NULL,                 -- human-friendly name e.g. "research-worker-laptop"
    user_id       TEXT NOT NULL,                 -- the admin user this key belongs to
    key_hash      TEXT NOT NULL UNIQUE,          -- sha256(plaintext) hex
    key_prefix    TEXT NOT NULL,                 -- first 11 chars of the plaintext for display ("vk_AbCdEf12")
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,                   -- NULL while active
    expires_at    TIMESTAMPTZ                    -- NULL = never expires
);

-- Hot path: validate header on every request — needs hash lookup
CREATE INDEX IF NOT EXISTS idx_admin_api_keys_hash
    ON admin_api_keys (key_hash)
    WHERE revoked_at IS NULL;

-- Listing UI / audit: keys per admin
CREATE INDEX IF NOT EXISTS idx_admin_api_keys_user
    ON admin_api_keys (user_id, created_at DESC);
