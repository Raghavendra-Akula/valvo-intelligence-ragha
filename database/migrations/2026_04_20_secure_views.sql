-- ═══════════════════════════════════════════════════════════════════════════
-- secure_views
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Postgres views default to bypassing the RLS policies of underlying tables
-- unless the view is explicitly marked security_invoker=on. Two views in
-- public schema referenced per-user tables but didn't have this flag, so
-- queries against the view ignored RLS and returned every user's rows.
--
-- Already applied to prod via Supabase MCP on 2026-04-20. This file is the
-- source-of-truth copy for re-deployments / new environments.
-- ═══════════════════════════════════════════════════════════════════════════

-- journal_trades_computed: view over journal_trades (per-user trade journal).
-- Fixed first, when the Vamshi leak was reported.
ALTER VIEW public.journal_trades_computed SET (security_invoker = on);

-- team_members: view over user_profiles WHERE role='admin'. Leaked every
-- admin's email + user_id + display_name before this fix.
ALTER VIEW public.team_members SET (security_invoker = on);
