-- ═══════════════════════════════════════════════════════════════════════════
-- fix_broken_rls_policies
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Discovered while chasing the "base_capital = Rs50L" leak for user Vamshi.
-- SIX tables had RLS enabled but their policies used `auth.role() =
-- 'authenticated'` or `true` instead of `auth.uid() = user_id`. That means
-- any authenticated user (via anon key OR via sql_query's SET LOCAL ROLE
-- authenticated) could SELECT every row, not just their own.
--
-- Audit ran 2026-04-20; all six tables confirmed exposed. This migration
-- drops the over-permissive policies and replaces them with user-scoped
-- ones matching the convention used by (correctly-configured) siblings
-- like journal_trades, legacy_trades*, submissions.
--
-- The "Allow service role full access" policy (auth.role() = 'service_role')
-- is preserved where it exists — service_role needs unrestricted access for
-- admin / backend operations and already bypasses RLS by design.
--
-- Already applied to prod via Supabase MCP on 2026-04-20. This file is the
-- source-of-truth copy for re-deployments / new environments.
-- ═══════════════════════════════════════════════════════════════════════════


-- ─── user_settings ──────────────────────────────────────────────────────
DROP POLICY IF EXISTS "Allow authenticated full access" ON public.user_settings;
DROP POLICY IF EXISTS "service_role_all"                ON public.user_settings;
CREATE POLICY users_own_data ON public.user_settings
  FOR ALL TO public
  USING (auth.uid() = user_id);


-- ─── backtest_submissions ──────────────────────────────────────────────
DROP POLICY IF EXISTS "Allow authenticated full access" ON public.backtest_submissions;
DROP POLICY IF EXISTS "service_role_all"                ON public.backtest_submissions;
CREATE POLICY users_own_data ON public.backtest_submissions
  FOR ALL TO public
  USING (auth.uid() = user_id);


-- ─── position_daily_updates ────────────────────────────────────────────
DROP POLICY IF EXISTS "Allow authenticated full access" ON public.position_daily_updates;
DROP POLICY IF EXISTS "service_role_all"                ON public.position_daily_updates;
CREATE POLICY users_own_data ON public.position_daily_updates
  FOR ALL TO public
  USING (auth.uid() = user_id);


-- ─── journal_settings ──────────────────────────────────────────────────
DROP POLICY IF EXISTS "service_role_all" ON public.journal_settings;
CREATE POLICY users_own_data ON public.journal_settings
  FOR ALL TO public
  USING (auth.uid() = user_id);


-- ─── journal_fund_months ───────────────────────────────────────────────
DROP POLICY IF EXISTS "service_role_all" ON public.journal_fund_months;
CREATE POLICY users_own_data ON public.journal_fund_months
  FOR ALL TO public
  USING (auth.uid() = user_id);


-- ─── saved_scanners ────────────────────────────────────────────────────
DROP POLICY IF EXISTS "service_role_all" ON public.saved_scanners;
CREATE POLICY users_own_data ON public.saved_scanners
  FOR ALL TO public
  USING (auth.uid() = user_id);
