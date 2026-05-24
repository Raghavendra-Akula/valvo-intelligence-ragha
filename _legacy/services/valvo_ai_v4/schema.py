"""
Valvo AI v4 -- Minimal schema for sql_query escape hatch.

Only loaded when the AI falls back to raw SQL.
Kept lean — semantic tools handle 80%+ of queries without this.
"""
from __future__ import annotations

ESCAPE_HATCH_SCHEMA = """\
TABLES (use only when no semantic tool fits):

legacy_trades (FY25-26): id, month, month_label, symbol, quantity, buy_value, sell_value, realized_pl, realized_pl_pct, impact_on_pf, is_winner(bool)
legacy_trades_fy2021..fy2425: same schema (FY20-21 through FY24-25)
journal_trades_computed (FY26-27): id, trade_no, symbol, stock_name, trade_date, entry_type, setup(jsonb), rating, entry_price, avg_entry, sl, initial_qty, quantity, buy_value, sell_value, exited_qty, open_qty, position_status, realized_pl, realized_pl_pct, impact_on_pf, is_winner, month, month_label, sector, user_id
positions: id, stock_name, entry_price, stop_loss, quantity, position_value, one_r_value, risk_pct, current_price, current_r_multiple, market_regime, defensive_status, bucket_sold_pct, first_sell_done, sell_history(jsonb), status, security_id, trailing_mode, exit_price, total_pnl, user_id
candles_daily: security_id, date, open, high, low, close, volume
candles_indices: security_id, symbol, date, open, high, low, close, volume
stock_universe: security_id, symbol, company_name, exchange, is_active
stock_daily_summary: security_id, symbol, company_name, prev_close, high_52w, low_52w, ma50, ma200, liq_cr, ema20, ema50, ema200, ath, close_5d, close_20d, close_60d, close_120d, close_252d, is_etf, first_trade_date, computed_date
index_constituents: id, index_symbol, index_display, stock_symbol, stock_name, sector, security_id, weightage
watchlists: id, name, pin_slot, color, sort_order, user_id
watchlist_items: id, watchlist_id(FK), symbol, company_name, security_id, notes, section_name, sort_order, user_id
journal_trades: (raw journal — prefer journal_trades_computed for analytics)
submissions: id, stock_name, market_price, final_score, rating, sector, setup_type, extension_pct, timestamp
market_regime_history: id, regime, note, updated_at
leading_sectors: id, sectors(ARRAY), regime, note, updated_at

FUNDAMENTAL TABLES:
fundamentals_overview: security_id(PK), eps_ttm, revenue_ttm_cr, net_profit_ttm_cr, net_profit_margin, operating_profit_margin, roe, roce, debt_to_equity, current_ratio, interest_coverage, book_value, dividend_per_share, promoter_holding_pct, fii_pct, dii_pct, last_result_date
financials_quarterly: security_id, period, period_end_date, revenue_cr, operating_profit_cr, opm_percent, net_profit_cr, eps, is_consolidated
financials_annual: security_id, fiscal_year, period_end_date, revenue_cr, net_profit_cr, eps, roe, roce, debt_to_equity, operating_cashflow_cr, free_cashflow_cr, dividend_per_share, is_consolidated
shareholding_quarterly: security_id, period, period_end_date, promoter_percent, fii_percent, dii_percent, public_percent, promoter_pledge_percent
corporate_actions: security_id, symbol, action_type(DIVIDEND/BONUS/SPLIT), ex_date, details, dividend_amount, bonus_ratio, split_ratio
segments_quarterly: symbol, period_end_date, segment_name, segment_revenue_cr, segment_profit_cr, segment_revenue_pct, segment_margin_pct
peers: security_id, peer_security_id, relevance_rank
bse_company_master: bse_code(PK), symbol, company_name, isin, sector, industry

FY TABLE MAP: 2020-21→legacy_trades_fy2021, 2021-22→fy2122, 2022-23→fy2223, 2023-24→fy2324, 2024-25→fy2425, 2025-26→legacy_trades, 2026-27→journal_trades_computed
MONTHLY TABLES: legacy_monthly_fy2021..fy2425, legacy_monthly_summary(FY25-26)
"""
