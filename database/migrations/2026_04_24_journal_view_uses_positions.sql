-- ═══════════════════════════════════════════════════════════════════════════
-- journal_view_uses_positions
-- ═══════════════════════════════════════════════════════════════════════════
--
-- The `journal_trades_computed` view derived position_status / realized_pl /
-- is_winner from journal_trades.e1/e2/e3_price/qty. Those columns only get
-- filled when the user manually enters exit info in the journal UI. When a
-- position is closed via the Position Manager (the common path), positions
-- table gets status='closed' + total_pnl + exit_price, but the journal exit
-- columns stay NULL — so the view said the trade was still Open.
--
-- Result: 8 of 9 closed FY26-27 trades for the admin user showed as Open
-- with realized_pl=0 in Stock Scoring, Analytics, and Valvo AI, while
-- Position Manager (which reads positions directly) showed the correct
-- 9 Closed / 6W 3L / ₹17.5L.
--
-- Fix: have the view fall back to positions data when journal exits are
-- empty. Single source of truth — every consumer automatically agrees.
--
-- Join key: p.id = j.position_id. journal_trades.position_id is the FK to
-- positions and is populated for every row. Joining on (user_id, stock_name)
-- broke when a user traded the same stock more than once — each journal row
-- multiplied by the number of positions rows for that stock, inflating
-- counts and P&L in Analytics. position_id is unique per trade, so we get
-- exactly one positions row per journal row.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE VIEW public.journal_trades_computed
WITH (security_invoker = on) AS
SELECT
    j.id,
    j.user_id,
    j.trade_no,
    j.symbol,
    j.symbol AS stock_name,
    j.trade_date,
    j.entry_type,
    j.setup,
    j.self_rating AS rating,
    j.entry_price,
    COALESCE(j.avg_entry, j.entry_price) AS avg_entry,
    j.sl,
    j.initial_qty,
    j.initial_qty AS quantity,
    (COALESCE(j.avg_entry, j.entry_price, 0::numeric) * j.initial_qty::numeric)::real AS buy_value,

    -- sell_value: prefer journal exits, fall back to positions exit_price * initial_qty
    CASE
        WHEN COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0) > 0
            THEN (COALESCE(j.e1_price * j.e1_qty::numeric, 0::numeric)
                  + COALESCE(j.e2_price * j.e2_qty::numeric, 0::numeric)
                  + COALESCE(j.e3_price * j.e3_qty::numeric, 0::numeric))::real
        WHEN p.status ILIKE 'closed' AND p.exit_price IS NOT NULL
            THEN (p.exit_price * j.initial_qty)::real
        ELSE 0::real
    END AS sell_value,

    -- exited_qty: prefer journal sums, fall back to initial_qty when positions closed
    CASE
        WHEN COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0) > 0
            THEN COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0)
        WHEN p.status ILIKE 'closed'
            THEN j.initial_qty
        ELSE 0
    END AS exited_qty,

    -- open_qty: 0 when positions closed, otherwise initial - journal exits
    CASE
        WHEN p.status ILIKE 'closed' THEN 0
        ELSE j.initial_qty - COALESCE(j.e1_qty, 0) - COALESCE(j.e2_qty, 0) - COALESCE(j.e3_qty, 0)
    END AS open_qty,

    -- position_status: 'Closed' when positions says so OR journal exits cover initial_qty
    CASE
        WHEN p.status ILIKE 'closed' THEN 'Closed'::text
        WHEN (j.initial_qty - COALESCE(j.e1_qty, 0) - COALESCE(j.e2_qty, 0) - COALESCE(j.e3_qty, 0)) <= 0 THEN 'Closed'::text
        ELSE 'Open'::text
    END AS position_status,

    -- realized_pl: prefer positions.total_pnl when closed, else journal-derived
    CASE
        WHEN p.status ILIKE 'closed' AND p.total_pnl IS NOT NULL
            THEN p.total_pnl::real
        ELSE (COALESCE(j.e1_price * j.e1_qty::numeric, 0::numeric)
              + COALESCE(j.e2_price * j.e2_qty::numeric, 0::numeric)
              + COALESCE(j.e3_price * j.e3_qty::numeric, 0::numeric)
              - COALESCE(j.avg_entry, j.entry_price, 0::numeric)
                * (COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0))::numeric)::real
    END AS realized_pl,

    -- pl alias (same as realized_pl)
    CASE
        WHEN p.status ILIKE 'closed' AND p.total_pnl IS NOT NULL
            THEN p.total_pnl::real
        ELSE (COALESCE(j.e1_price * j.e1_qty::numeric, 0::numeric)
              + COALESCE(j.e2_price * j.e2_qty::numeric, 0::numeric)
              + COALESCE(j.e3_price * j.e3_qty::numeric, 0::numeric)
              - COALESCE(j.avg_entry, j.entry_price, 0::numeric)
                * (COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0))::numeric)::real
    END AS pl,

    -- realized_pl_pct: prefer positions exit_price-based, else journal
    CASE
        WHEN p.status ILIKE 'closed' AND p.exit_price IS NOT NULL
             AND COALESCE(j.avg_entry, j.entry_price, 0::numeric) > 0::numeric
            THEN round(((p.exit_price - COALESCE(j.avg_entry, j.entry_price, 0::numeric)::real)
                        / COALESCE(j.avg_entry, j.entry_price, 1::numeric)::real * 100::real)::numeric, 2)::real
        WHEN (COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0)) > 0
             AND COALESCE(j.avg_entry, j.entry_price, 0::numeric) > 0::numeric
            THEN round(((COALESCE(j.e1_price * j.e1_qty::numeric, 0::numeric)
                         + COALESCE(j.e2_price * j.e2_qty::numeric, 0::numeric)
                         + COALESCE(j.e3_price * j.e3_qty::numeric, 0::numeric))
                        / NULLIF(COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0), 0)::numeric
                        - COALESCE(j.avg_entry, j.entry_price, 0::numeric))
                       / COALESCE(j.avg_entry, j.entry_price, 1::numeric) * 100::numeric, 2)::real
        ELSE 0::real
    END AS realized_pl_pct,

    -- stock_move_pct: same logic as realized_pl_pct
    CASE
        WHEN p.status ILIKE 'closed' AND p.exit_price IS NOT NULL
             AND COALESCE(j.avg_entry, j.entry_price, 0::numeric) > 0::numeric
            THEN round(((p.exit_price - COALESCE(j.avg_entry, j.entry_price, 0::numeric)::real)
                        / COALESCE(j.avg_entry, j.entry_price, 1::numeric)::real * 100::real)::numeric, 2)::real
        WHEN (COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0)) > 0
             AND COALESCE(j.avg_entry, j.entry_price, 0::numeric) > 0::numeric
            THEN round(((COALESCE(j.e1_price * j.e1_qty::numeric, 0::numeric)
                         + COALESCE(j.e2_price * j.e2_qty::numeric, 0::numeric)
                         + COALESCE(j.e3_price * j.e3_qty::numeric, 0::numeric))
                        / NULLIF(COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0), 0)::numeric
                        - COALESCE(j.avg_entry, j.entry_price, 0::numeric))
                       / COALESCE(j.avg_entry, j.entry_price, 1::numeric) * 100::numeric, 2)::real
        ELSE 0::real
    END AS stock_move_pct,

    -- impact_on_pf: realized_pl as a fraction of 5Cr fixed denominator (legacy)
    round(
      (CASE
          WHEN p.status ILIKE 'closed' AND p.total_pnl IS NOT NULL THEN p.total_pnl::numeric
          ELSE (COALESCE(j.e1_price * j.e1_qty::numeric, 0::numeric)
                + COALESCE(j.e2_price * j.e2_qty::numeric, 0::numeric)
                + COALESCE(j.e3_price * j.e3_qty::numeric, 0::numeric)
                - COALESCE(j.avg_entry, j.entry_price, 0::numeric)
                  * (COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0))::numeric)
       END) / 50000000.0 * 100::numeric,
      6
    )::real AS impact_on_pf,

    -- is_winner: based on canonical realized_pl
    CASE
        WHEN p.status ILIKE 'closed' AND p.total_pnl IS NOT NULL THEN p.total_pnl > 0::real
        ELSE (COALESCE(j.e1_price * j.e1_qty::numeric, 0::numeric)
              + COALESCE(j.e2_price * j.e2_qty::numeric, 0::numeric)
              + COALESCE(j.e3_price * j.e3_qty::numeric, 0::numeric)
              - COALESCE(j.avg_entry, j.entry_price, 0::numeric)
                * (COALESCE(j.e1_qty, 0) + COALESCE(j.e2_qty, 0) + COALESCE(j.e3_qty, 0))::numeric) > 0::numeric
    END AS is_winner,

    (lower(to_char(j.trade_date::timestamp with time zone, 'fmmonth'::text)) || '_'::text)
        || to_char(j.trade_date::timestamp with time zone, 'YYYY'::text) AS month,
    to_char(j.trade_date::timestamp with time zone, 'FMMonth YYYY'::text) AS month_label,

    CASE
        WHEN COALESCE(j.avg_entry, j.entry_price, 0::numeric) > 0::numeric
            THEN round(abs(COALESCE(j.avg_entry, j.entry_price, 0::numeric) - COALESCE(j.sl, 0::numeric))
                       / COALESCE(j.avg_entry, j.entry_price, 1::numeric) * 100::numeric, 2)
        ELSE 3.0
    END::real AS sl_pct,

    j.plan_followed,
    j.exit_trigger,
    j.notes,
    j.sector,
    j.security_id,
    j.created_at
FROM journal_trades j
LEFT JOIN positions p ON p.id = j.position_id
WHERE j.trade_date >= '2026-04-01'::date;
