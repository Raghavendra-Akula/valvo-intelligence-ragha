-- ═══════════════════════════════════════════════════════════════════════════
-- enable_rls_on_unprotected_user_tables
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Found during the Vamshi-sees-Rohit's-trades audit: four tables had a
-- user_id UUID column but RLS was disabled. service_role (our Python
-- backend) bypasses RLS by design, but anon / authenticated keys via the
-- Supabase client libraries would have been able to SELECT every user's
-- rows directly. Closed here.
--
-- Already applied to prod via Supabase MCP on 2026-04-20. This file is
-- the source-of-truth copy for re-deployments / new environments.
-- ═══════════════════════════════════════════════════════════════════════════

-- nexus_trades / nexus_monthly / nexus_analytics: per-user trading data.
ALTER TABLE public.nexus_trades ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_own_data ON public.nexus_trades
  FOR ALL TO public
  USING (auth.uid() = user_id);

ALTER TABLE public.nexus_monthly ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_own_data ON public.nexus_monthly
  FOR ALL TO public
  USING (auth.uid() = user_id);

ALTER TABLE public.nexus_analytics ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_own_data ON public.nexus_analytics
  FOR ALL TO public
  USING (auth.uid() = user_id);

-- user_ai_context: created recently, missed RLS on the original migration.
ALTER TABLE public.user_ai_context ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_own_data ON public.user_ai_context
  FOR ALL TO public
  USING (auth.uid() = user_id);
