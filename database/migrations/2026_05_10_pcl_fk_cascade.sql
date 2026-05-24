-- ═══════════════════════════════════════════════════════════════════════════
-- portfolio_capital_log → positions FK with ON DELETE CASCADE
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Background: portfolio_capital_log.position_id was a plain BIGINT with no FK.
-- Three code paths (journal cascade-delete, AI agent _delete_position, chat
-- delete_position tool) ran raw `DELETE FROM positions` without calling
-- services.portfolio_capital_log.delete_for_position(), leaving orphan rows
-- whose realized_pnl kept inflating the equity-curve total forever.
--
-- This migration:
--   1. Drops orphan rows whose position_id no longer exists, so the user's
--      Portfolio Capital snaps back to the correct value immediately.
--   2. Adds a FK with ON DELETE CASCADE so any future `DELETE FROM positions`
--      auto-cleans the log, regardless of which code path triggered it.
--
-- Idempotent — safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════

-- 1) Cleanup existing orphans
DELETE FROM public.portfolio_capital_log
WHERE position_id IS NOT NULL
  AND position_id NOT IN (SELECT id FROM public.positions);

-- 2) Add FK with CASCADE (skip if already present)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.portfolio_capital_log'::regclass
          AND conname = 'pcl_position_fk'
    ) THEN
        ALTER TABLE public.portfolio_capital_log
            ADD CONSTRAINT pcl_position_fk
            FOREIGN KEY (position_id)
            REFERENCES public.positions(id)
            ON DELETE CASCADE;
    END IF;
END
$$;
