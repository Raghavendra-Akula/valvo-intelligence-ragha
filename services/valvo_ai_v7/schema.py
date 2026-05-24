"""
Valvo AI v6 -- Embedded database schema for fast system-prompt construction.
"""
from __future__ import annotations

DB_SCHEMA = """\
analytics_config: id(integer), config_key(text), config_value(jsonb), description(text), statistical_basis(text), last_computed_at(timestamp), updated_at(timestamp)
app_settings: key(text), value(text), updated_at(timestamptz)
candles_daily: security_id(text), date(date), open(float8), high(float8), low(float8), close(float8), volume(bigint)
candles_indices: security_id(text), symbol(text), date(date), open(float8), high(float8), low(float8), close(float8), volume(bigint)
index_constituents: id(integer), index_symbol(text), index_display(text), stock_symbol(text), stock_name(text), sector(text), security_id(text), weightage(float8)
journal_trades: id(integer), trade_no(integer), trade_date(date), symbol(varchar), name(varchar), setup(jsonb), entry_type(varchar), self_rating(smallint), entry_price(numeric), avg_entry(numeric), sl(numeric), initial_qty(integer), sector(varchar), security_id(varchar), notes(text), e1_price(numeric), e1_qty(integer), e1_date(date), e2_price(numeric), e2_qty(integer), e2_date(date), e3_price(numeric), e3_qty(integer), e3_date(date), plan_followed(varchar), exit_trigger(text), growth_areas(text), base_duration(varchar), position_id(integer). Pyramid legs live on positions.pyramid_history (JSONB array of {date, price, shares, slot}) — JOIN positions for pyramid data.
journal_trades_computed: id(integer), trade_no(integer), symbol(varchar), stock_name(varchar), trade_date(date), entry_type(varchar), setup(jsonb), rating(smallint), entry_price(numeric), avg_entry(numeric), sl(numeric), initial_qty(integer), quantity(integer), buy_value(numeric), sell_value(numeric), exited_qty(integer), open_qty(integer), position_status(text), realized_pl(numeric), realized_pl_pct(numeric), impact_on_pf(numeric), is_winner(boolean), month(text), month_label(text), sector(varchar)
legacy_trades (FY25-26): id(integer), month(text), month_label(text), symbol(text), quantity(real), buy_value(real), sell_value(real), realized_pl(real), realized_pl_pct(real), impact_on_pf(real), is_winner(boolean GENERATED)
legacy_trades_fy2021 (FY20-21): same schema as legacy_trades
legacy_trades_fy2122 (FY21-22): same schema as legacy_trades
legacy_trades_fy2223 (FY22-23): same schema as legacy_trades
legacy_trades_fy2324 (FY23-24): same schema as legacy_trades
legacy_trades_fy2425 (FY24-25): same schema as legacy_trades
legacy_monthly_fy2021 (FY20-21): id(integer), month(text), month_label(text), month_order(integer), net_pf_impact(real), after_charges(real), charges(real), scripts_traded(integer), approx_trades(integer), win_rate(real), total_buy_value(real)
legacy_monthly_fy2122 (FY21-22): same schema
legacy_monthly_fy2223 through legacy_monthly_summary: same schema (FY25-26 also has nifty_smallcap_change)
positions: id(integer), stock_name(text), entry_price(real), stop_loss(real), quantity(integer), position_value(real), one_r_value(real), risk_pct(real), current_price(real), current_r_multiple(real), market_regime(text), defensive_status(text), bucket_sold_pct(real), first_sell_done(boolean), sell_history(jsonb), status(text), security_id(text), trailing_mode(text), exit_price(real), total_pnl(real)
stock_universe: security_id(text), symbol(text), company_name(text), exchange(text), is_active(boolean)
submissions: id(integer), stock_name(text), market_price(real), final_score(real), rating(text), sector(text), setup_type(text), extension_pct(real), timestamp(timestamp)
stock_daily_summary: security_id(text), symbol(text), company_name(text), prev_close(float8), high_52w(float8), low_52w(float8), ma50(float8), ma200(float8), liq_cr(float8), ema20(float8), ema50(float8), ema200(float8), ath(float8), close_5d(float8), close_20d(float8), close_60d(float8), close_120d(float8), close_252d(float8), is_etf(boolean), first_trade_date(date), computed_date(date)
watchlists: id(integer), name(text), pin_slot(integer), color(text), sort_order(integer), user_id(uuid), created_at(timestamp)
watchlist_items: id(integer), watchlist_id(integer FK→watchlists.id), symbol(text), company_name(text), security_id(text), notes(text), section_name(text), sort_order(integer), user_id(uuid), added_at(timestamp)
market_regime_history: id(integer), regime(text), note(text), updated_at(timestamp)
leading_sectors: id(integer), sectors(ARRAY), regime(text), note(text), updated_at(timestamp)
user_settings: id(integer), user_id(uuid), display_name(text), palette(text), show_52w(boolean) -- NOTE: base_capital column exists but is DEPRECATED and no longer written. Read from user_fy_config instead.
user_fy_config: user_id(uuid), fy(text) -- e.g. '2026-27', base_capital(numeric), created_at(timestamp). PRIMARY KEY (user_id, fy). This is the SOURCE OF TRUTH for per-user base capital per FY.

# FUNDAMENTALS (P/E, P/B, RoE, margins, growth, shareholding, peers, corporate actions)
# These tables are global (no user filter) — same data is visible to every user.
fundamentals_overview: security_id(text), symbol(text), nse_code(text), bse_code(text), isin(text), company_name(text), sector(text), industry(text), about(text), website(text), current_price(numeric), market_cap_cr(numeric), enterprise_value_cr(numeric), pe_ratio(numeric), pb_ratio(numeric), ev_to_ebitda(numeric), price_to_sales(numeric), eps_ttm(numeric), book_value_per_share(numeric), revenue_per_share(numeric), face_value(numeric), dividend_yield(numeric), promoter_holding_pct(numeric), roe(numeric), roce(numeric), roa(numeric), roe_10yr_avg(numeric), gross_profit_margin(numeric), operating_profit_margin(numeric), net_profit_margin(numeric), debt_to_equity(numeric), net_debt_to_equity(numeric), debt_to_assets(numeric), interest_coverage(numeric), current_ratio(numeric), quick_ratio(numeric), inventory_days(numeric), debtor_days(numeric), days_payable(numeric), cash_conversion_cycle(numeric), sales_growth_ttm(numeric), sales_growth_3yr_cagr(numeric), sales_growth_5yr_cagr(numeric), sales_growth_10yr_cagr(numeric), profit_growth_ttm(numeric), profit_growth_3yr_cagr(numeric), profit_growth_5yr_cagr(numeric), profit_growth_10yr_cagr(numeric), price_cagr_1yr(numeric), price_cagr_3yr(numeric), price_cagr_5yr(numeric), price_cagr_10yr(numeric), week_52_high(numeric), week_52_low(numeric), total_shares_cr(numeric), is_consolidated(bool), data_quality_flag(text), last_result_date(date), listing_date(date), updated_at(timestamp)
financials_quarterly: id(bigint), security_id(text), symbol(text), period(text), period_end_date(date), filing_date(date), is_consolidated(bool), is_audited(bool), revenue_cr(numeric), other_income_cr(numeric), total_income_cr(numeric), expenses_cr(numeric), raw_material_cost_cr(numeric), employee_cost_cr(numeric), other_expenses_cr(numeric), operating_profit_cr(numeric), opm_percent(numeric), interest_cr(numeric), depreciation_cr(numeric), profit_before_tax_cr(numeric), tax_cr(numeric), net_profit_cr(numeric), adjusted_net_profit_cr(numeric), eps(numeric), eps_diluted(numeric), exceptional_items_cr(numeric), has_exceptional_items(bool), minority_interest_cr(numeric), face_value(numeric), source_url(text), updated_at(timestamp)
financials_annual: id(bigint), security_id(text), symbol(text), fiscal_year(text), period_end_date(date), filing_date(date), is_consolidated(bool), is_audited(bool), revenue_cr(numeric), total_income_cr(numeric), expenses_cr(numeric), operating_profit_cr(numeric), opm_percent(numeric), net_profit_cr(numeric), adjusted_net_profit_cr(numeric), eps(numeric), eps_diluted(numeric), dividend_per_share(numeric), interest_coverage(numeric), debt_equity_ratio(numeric), debt_to_equity(numeric), current_ratio(numeric), roe(numeric), roce(numeric), equity_capital_cr(numeric), reserves_cr(numeric), total_equity_cr(numeric), total_borrowings_cr(numeric), long_term_borrowings_cr(numeric), short_term_borrowings_cr(numeric), total_assets_cr(numeric), total_liabilities_cr(numeric), fixed_assets_cr(numeric), intangible_assets_cr(numeric), goodwill_cr(numeric), cwip_cr(numeric), investments_cr(numeric), cash_equivalents_cr(numeric), inventory_cr(numeric), trade_receivables_cr(numeric), trade_payables_cr(numeric), total_current_assets_cr(numeric), total_current_liabilities_cr(numeric), operating_cashflow_cr(numeric), investing_cashflow_cr(numeric), financing_cashflow_cr(numeric), free_cashflow_cr(numeric), capex_cr(numeric), net_cashflow_cr(numeric), face_value(numeric), source_url(text), updated_at(timestamp)
segments_quarterly: id(bigint), security_id(text), symbol(text), period_end_date(date), segment_name(text), segment_order(int), segment_revenue_cr(numeric), segment_revenue_pct(numeric), segment_profit_cr(numeric), segment_margin_pct(numeric), segment_assets_cr(numeric), segment_liabilities_cr(numeric), is_consolidated(bool)
shareholding_quarterly: id(bigint), security_id(text), symbol(text), period(text), period_end_date(date), submission_date(date), promoter_percent(numeric), promoter_shares(bigint), promoter_pledge_percent(numeric), fii_percent(numeric), fii_shares(bigint), dii_percent(numeric), dii_shares(bigint), mutual_fund_percent(numeric), insurance_percent(numeric), government_percent(numeric), public_percent(numeric), public_shares(bigint), employee_trusts_percent(numeric), other_percent(numeric), total_shares(bigint), number_of_shareholders(bigint)
corporate_actions: id(bigint), security_id(text), symbol(text), action_type(text) -- 'dividend'|'bonus'|'split'|'rights', action_date(date), ex_date(date), record_date(date), payment_date(date), dividend_amount(numeric), dividend_type(text), bonus_ratio(text), split_ratio(text), face_value_before(numeric), face_value_after(numeric), details(text)
filings: id(bigint), security_id(text), symbol(text), exchange(text), filing_type(text), filing_date(date), period(text), fiscal_year(text), description(text), pdf_url(text), file_size_kb(int)
peers: id(bigint), security_id(text), symbol(text), peer_security_id(text), peer_symbol(text), industry(text), relevance_rank(int)
breadth_daily_history: date(date), advance_count(int), decline_count(int), new_highs(int), new_lows(int), total_stocks(int), pct_above_ema20(float), pct_above_ema50(float), pct_above_ema200(float), momentum_20pc(int), thrust(float), pct_down_20(float), pct_down_30(float), pct_down_50(float), up_20pc_5d(int), up_30pc_5d(int), up_4pc_vol(int), down_4pc_vol(int), sector_breadth(jsonb)  -- up_*pc_5d: count of stocks where (close / close_5_trading_days_ago - 1) > threshold. up_4pc_vol/down_4pc_vol: Stockbee-style daily count of stocks moving >=4% (or <=-4%) with volume > prior day — conviction breadth (vs `thrust` which is the A/D ratio %).
"""

SCHEMA_NOTES = """\
FY TRADE TABLES (closed-trade data per financial year):
  - legacy_trades_fy2021   -> FY 2020-21
  - legacy_trades_fy2122   -> FY 2021-22
  - legacy_trades_fy2223   -> FY 2022-23
  - legacy_trades_fy2324   -> FY 2023-24
  - legacy_trades_fy2425   -> FY 2024-25
  - legacy_trades          -> FY 2025-26
  - journal_trades_computed -> FY 2026-27 (current year)

FY MONTHLY TABLES (monthly P&L summaries):
  - legacy_monthly_fy2021  -> FY 2020-21
  - legacy_monthly_fy2122  -> FY 2021-22
  - legacy_monthly_fy2223  -> FY 2022-23
  - legacy_monthly_fy2324  -> FY 2023-24
  - legacy_monthly_fy2425  -> FY 2024-25
  - legacy_monthly_summary -> FY 2025-26

USER BASE CAPITAL (per FY, per user):
  Source of truth: user_fy_config(user_id, fy, base_capital).
  "what's my base capital" / "my capital for FY X" / "how much capital do I have":
    SELECT base_capital FROM user_fy_config WHERE user_id = <uid> AND fy = '<fy>'
  If no row exists for the FY, the user has not set one yet — say so plainly, do
  NOT fall back to the historical chain below and pretend it's their capital.
  DO NOT read user_settings.base_capital — that column is deprecated (still in
  the table for back-compat but no longer updated anywhere).

HISTORICAL STARTING CAPITAL (legacy reference, NOT the user's current capital):
  The owner's historical per-FY starting capital (post-tax, beginning-of-year)
  used by legacy analytics only: FY20-21: Rs 6,000,000 | FY21-22: Rs 9,075,419
  | FY22-23: Rs 16,187,147 | FY23-24: Rs 18,028,240 | FY24-25: Rs 28,000,000 |
  FY25-26: Rs 50,000,000. These are NOT the current user's base capital — never
  quote these when asked about "my" capital.

KEY FORMULAS:
  realized_pl_pct = (realized_pl / buy_value) * 100
  R-multiple     = realized_pl_pct / 3.0
  is_winner      = realized_pl > 0

CLOSED-TRADE QUERY ROUTING (read this carefully — most "no data" failures
happen here because the model picks the wrong table or filter):

  "Closed trades / closed positions / my P&L THIS FY" (current FY 2026-27):
    SELECT trade_date, symbol, stock_name, ROUND(realized_pl::numeric) AS pl,
           ROUND(realized_pl_pct::numeric, 2) AS pct, sector, position_status,
           open_qty, exited_qty
    FROM journal_trades_computed
    WHERE (
        position_status ILIKE '%closed%'
        OR position_status ILIKE '%exited%'
        OR (open_qty IS NOT NULL AND open_qty = 0)
    )
    ORDER BY trade_date DESC

  position_status values vary ("Closed", "CLOSED", "Exited", null) —
  ALWAYS use ILIKE OR an open_qty=0 fallback. NEVER use position_status='Closed'
  alone (case-sensitive equality has missed real rows).

  "Closed trades for FY25-26":   SELECT * FROM legacy_trades
  "Closed trades for FY24-25":   SELECT * FROM legacy_trades_fy2425
  ...older FYs via legacy_trades_fy<YYYY> table per the FY map above.
  Every row in a legacy_trades_* table IS a closed trade — no status filter needed.

  "Across ALL FYs" — UNION ALL:
    SELECT '2020-21' fy, symbol, realized_pl, realized_pl_pct FROM legacy_trades_fy2021
    UNION ALL SELECT '2021-22', symbol, realized_pl, realized_pl_pct FROM legacy_trades_fy2122
    UNION ALL SELECT '2022-23', symbol, realized_pl, realized_pl_pct FROM legacy_trades_fy2223
    UNION ALL SELECT '2023-24', symbol, realized_pl, realized_pl_pct FROM legacy_trades_fy2324
    UNION ALL SELECT '2024-25', symbol, realized_pl, realized_pl_pct FROM legacy_trades_fy2425
    UNION ALL SELECT '2025-26', symbol, realized_pl, realized_pl_pct FROM legacy_trades
    UNION ALL SELECT '2026-27', symbol, realized_pl, realized_pl_pct FROM journal_trades_computed
        WHERE (position_status ILIKE '%closed%' OR open_qty = 0)

  "Active / open positions"  → use the get_positions(status="active") TOOL,
  not raw SQL. The positions table is the live portfolio, not the journal.

JOINS:
  candles_daily has daily OHLCV for NSE stocks; use security_id to join with stock_universe.
  positions table tracks the live portfolio; status='active' for current positions.

WATCHLISTS:
  watchlists = the user's watchlist groups (e.g. "Primary Watchlist", "Stock Universe")
  watchlist_items = individual stocks in each watchlist, linked via watchlist_id
  To get all stocks in a user's watchlists: SELECT wi.symbol, wi.company_name, w.name as list_name FROM watchlist_items wi JOIN watchlists w ON w.id = wi.watchlist_id ORDER BY w.name, wi.sort_order
  Both tables have RLS — only the current user's watchlists are visible.

MONTH FORMAT in legacy tables: "April 2022", "May 2022", etc.

SCREENER / SCANNER DATA:
  stock_daily_summary is the pre-computed screener table used by the Valvo Scanner/Screener page.
  - "legacy scanner", "screener", "scanning section", "scanner results" = stock_daily_summary table
  - Default scan filters: prev_close >= 0.75 * high_52w (within 25% of 52W high), prev_close > ma200, liq_cr > 0.5, is_etf = false
  - liq_cr = average daily liquidity in Crores (20-day average turnover / 1e7)
  - Use this table for any question about scanned stocks, screener results, liquid stocks, stocks near highs, etc.
  - submissions table = SCORED stocks from the Scoring page (not the scanner). Only stocks the user has manually scored.
  - Do NOT confuse scanner (stock_daily_summary) with scored submissions (submissions table).

FUNDAMENTALS:
  fundamentals_overview is the single-row snapshot per stock — 60+ headline ratios
  (P/E, P/B, ROE, RoCE, margins, growth CAGRs, debt ratios, 52-week high/low).
  Use sql_query for "what's the P/E of RELIANCE", "show me TCS fundamentals",
  "compare ROCE of INFY and WIPRO". Join key: symbol OR security_id.
    SELECT pe_ratio, pb_ratio, roe, roce, market_cap_cr, net_profit_margin
    FROM fundamentals_overview WHERE symbol ILIKE '%RELIANCE%'

  For time-series line items use financials_quarterly (last ~20 quarters) or
  financials_annual (10+ years of P&L + balance sheet + cash flow).
  period_end_date is the as-of date; filing_date is when the company reported.
  is_consolidated=true rows are preferred when available.

  ALWAYS SELECT period_end_date for time-series — the `period` text column is
  unreliable (often literally "Quarterly" for newly-listed names like GROWW).
  Render the row label from period_end_date as "Q<n> FY<yy>" (Indian fiscal
  year, Apr–Mar). Mapping by month: Jun→Q1, Sep→Q2, Dec→Q3, Mar→Q4. FY = year
  if month ≤ 3 else year + 1 (e.g. 2025-06-30 → "Q1 FY26").

  Example: "last 4 quarter revenue of TATASTEEL" →
    SELECT period_end_date, period, revenue_cr, net_profit_cr, opm_percent
    FROM financials_quarterly
    WHERE symbol ILIKE '%TATASTEEL%' AND is_consolidated = true
    ORDER BY period_end_date DESC LIMIT 4

  Peers: SELECT peer_symbol FROM peers WHERE symbol ILIKE '%X%' ORDER BY relevance_rank
  Corporate actions: SELECT action_type, ex_date, dividend_amount, bonus_ratio,
    split_ratio FROM corporate_actions WHERE symbol ILIKE '%X%' ORDER BY ex_date DESC
  Shareholding: latest row from shareholding_quarterly, ordered by period_end_date DESC.
  Segments: financials_quarterly tells you company total; segments_quarterly breaks
    it down by business segment for the same period_end_date.
  Filings: filings table has links to the raw PDF/XBRL for audit trails.

MARKET BREADTH:
  breadth_daily_history is the EoD snapshot (one row per trading day) with
  advance/decline, % of universe above EMA20/50/200, new highs/lows, and a
  JSONB sector_breadth map (key=sector name, value=breadth stats).
  "how's market breadth" → SELECT date, advance_count, decline_count, new_highs,
    new_lows, pct_above_ema50, pct_above_ema200 FROM breadth_daily_history
    ORDER BY date DESC LIMIT 5
"""
