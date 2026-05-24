-- ═══════════════════════════════════════════════════════════════════════════
-- portfolio_capital_log
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Time-series log of every realized-PnL event so we can plot the equity curve
-- and run drawdown / FY-running-capital analytics without recomputing from
-- positions.sell_history JSONB on every read.
--
-- A "realized event" is any time shares get booked at a price different from
-- entry: regular partial exits (E1/E2/E3 in sell_history), the final close
-- block, and pyramid-leg unwinds (pyramid_history[i].exits[]). Pyramid ADDs
-- are NOT logged — they realize nothing.
--
-- Source of truth: positions table. This log is a derived projection, fully
-- rebuildable by services.portfolio_capital_log.rebuild_for_position. The
-- (user_id, source_key) UNIQUE makes the rebuild idempotent — it deletes by
-- position_id and re-inserts. Edits to a sell slot just rebuild that
-- position's rows.
--
-- capital_after is NOT stored. Running capital = base_capital(fy) + Σ
-- realized_pnl ordered by event_ts; it's computed at query time so a single
-- position's rebuild can't desync the rest of the FY.
--
-- Idempotent — safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.portfolio_capital_log (
    id            BIGSERIAL PRIMARY KEY,
    user_id       UUID        NOT NULL,
    fy            TEXT        NOT NULL,
    position_id   BIGINT,
    event_date    DATE        NOT NULL,
    event_ts      TIMESTAMPTZ NOT NULL,
    event_type    TEXT        NOT NULL CHECK (event_type IN ('partial_exit', 'final_close', 'pyramid_exit')),
    source_key    TEXT        NOT NULL,
    stock_name    TEXT,
    shares        INTEGER,
    price         NUMERIC,
    realized_pnl  NUMERIC     NOT NULL,
    trigger       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT portfolio_capital_log_user_source_uniq UNIQUE (user_id, source_key)
);

CREATE INDEX IF NOT EXISTS idx_pcl_user_fy_ts
    ON public.portfolio_capital_log (user_id, fy, event_ts);

CREATE INDEX IF NOT EXISTS idx_pcl_user_position
    ON public.portfolio_capital_log (user_id, position_id);

CREATE INDEX IF NOT EXISTS idx_pcl_user_fy_date
    ON public.portfolio_capital_log (user_id, fy, event_date);

-- RLS — backend connects as service_role and bypasses RLS, but every backend
-- query MUST filter by user_id explicitly. Policy is for any future direct
-- client read (e.g. via Supabase JS client) so a user only sees their rows.
ALTER TABLE public.portfolio_capital_log ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'portfolio_capital_log'
          AND policyname = 'pcl_owner'
    ) THEN
        CREATE POLICY pcl_owner ON public.portfolio_capital_log
            FOR ALL
            USING (user_id = auth.uid())
            WITH CHECK (user_id = auth.uid());
    END IF;
END
$$;
