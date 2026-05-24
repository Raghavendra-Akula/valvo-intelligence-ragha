"""
Valvo AI v4 -- Tool registry and dispatcher.

Semantic tools for 80%+ of queries + v3 executors for proven tools + v2 actions.
"""
from __future__ import annotations

import json
import re
import traceback
from typing import Any

from services.valvo_ai_v2.utils import to_jsonable

# Re-use proven v3 executors
from services.valvo_ai_v3.tools import (
    _exec_get_analytics,
    _exec_get_equity_curve,
    _exec_get_positions,
    _exec_get_live_market,
    _exec_search_stock,
    _exec_confirm_action,
    _exec_cancel_action,
    _validate_sql,
)
# Re-use v2 action infrastructure
from services.valvo_ai_v2.actions import (
    ACTIONS as V2_ACTIONS,
    get_action_tools as _v2_action_tool_defs,
    run_action,
)

# New v4 semantic tools
from .tool_trades import exec_query_trades, exec_query_monthly
from .tool_extras import (
    exec_scan_stocks,
    exec_get_watchlist,
    exec_get_journal_insights,
    exec_compare_to_index,
    exec_get_fundamentals,
    exec_search_stock,
    exec_get_live_market,
)
from .tool_scoring import exec_lookup_scores, exec_rank_stocks, exec_get_top_scores
from .tool_regime import exec_get_regime
from .tool_sector import exec_get_sectors, exec_get_sector_constituents, exec_compare_sectors


# ═══════════════════════════════════════════════════════════════════════════
#  User context
# ═══════════════════════════════════════════════════════════════════════════

def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  NEW SEMANTIC TOOL DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════

QUERY_TRADES_TOOL = {
    "name": "query_trades",
    "description": (
        "Search and analyze trades across any financial year. "
        "Handles multi-year queries, filtering, and aggregation automatically. "
        "Use this instead of sql_query for ANY trade-related question."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fys": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Financial years to query. e.g. ['2024-25'] or ['all'] for all years. "
                    "Defaults to current FY."
                ),
            },
            "symbol": {
                "type": "string",
                "description": "Filter by stock symbol (e.g. 'RELIANCE'). Case-insensitive.",
            },
            "sector": {
                "type": "string",
                "description": "Filter by sector. Only works for FY 2026-27 journal trades.",
            },
            "winners_only": {
                "type": "boolean",
                "description": "Only winning trades.",
            },
            "losers_only": {
                "type": "boolean",
                "description": "Only losing trades.",
            },
            "min_return_pct": {
                "type": "number",
                "description": "Minimum return percentage.",
            },
            "max_return_pct": {
                "type": "number",
                "description": "Maximum return percentage.",
            },
            "min_pl": {
                "type": "number",
                "description": "Minimum absolute P&L in Rs.",
            },
            "month": {
                "type": "string",
                "description": "Filter by month label, e.g. 'April 2024'.",
            },
            "sort_by": {
                "type": "string",
                "enum": ["pl", "return_pct", "r_multiple", "symbol"],
                "description": "Sort results by. Default: pl.",
            },
            "sort_order": {
                "type": "string",
                "enum": ["asc", "desc"],
                "description": "Sort order. Default: desc.",
            },
            "limit": {
                "type": "integer",
                "description": "Max rows. Default: 50, max: 200.",
            },
            "aggregate": {
                "type": "string",
                "enum": ["monthly", "yearly", "sector", "symbol", "none"],
                "description": (
                    "Group results. 'monthly'=by month, 'yearly'=by FY, "
                    "'sector'=by sector, 'symbol'=by stock, 'none'=individual trades."
                ),
            },
            "metrics": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "count", "sum_pl", "win_rate", "avg_return",
                        "best_trade", "worst_trade", "avg_r_multiple",
                        "profit_factor", "expectancy",
                    ],
                },
                "description": "Metrics to compute when aggregating. Default: count, sum_pl, win_rate.",
            },
        },
    },
}

QUERY_MONTHLY_TOOL = {
    "name": "query_monthly",
    "description": (
        "Get monthly P&L summaries with charges, turnover, win rate, and trade count. "
        "Uses pre-computed monthly tables (FY 2020-21 through 2025-26)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Financial years. e.g. ['2024-25'] or ['all']. Default: current FY.",
            },
        },
    },
}

SCAN_STOCKS_TOOL = {
    "name": "scan_stocks",
    "description": (
        "Screen stocks from the daily scanner. Find stocks near 52-week highs, "
        "above moving averages, with liquidity filters. "
        "Use this for any screener/scanner question."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "preset": {
                "type": "string",
                "enum": ["near_52w_high", "breakout_candidates", "ma_crossover", "beaten_down"],
                "description": (
                    "Preset filter: 'near_52w_high' (default), 'breakout_candidates' (within 10%% of high + liquid), "
                    "'ma_crossover' (EMA20 > EMA50), 'beaten_down' (below MA200, near 52w low)."
                ),
            },
            "min_liquidity_cr": {
                "type": "number",
                "description": "Minimum daily liquidity in Crores. Default: 0.5",
            },
            "above_ma200": {
                "type": "boolean",
                "description": "Price above 200 SMA. Default: true.",
            },
            "above_ma50": {
                "type": "boolean",
                "description": "Price above 50 SMA. Default: false.",
            },
            "within_pct_of_52w_high": {
                "type": "number",
                "description": "Within X%% of 52-week high. Default: 25.",
            },
            "min_price": {"type": "number", "description": "Minimum stock price."},
            "max_price": {"type": "number", "description": "Maximum stock price."},
            "sector": {"type": "string", "description": "Filter by sector name."},
            "sort_by": {
                "type": "string",
                "enum": ["proximity_to_high", "liquidity", "price", "momentum_5d", "momentum_20d"],
                "description": "Sort results by. Default: proximity_to_high.",
            },
            "limit": {"type": "integer", "description": "Max results. Default: 50."},
        },
    },
}

GET_WATCHLIST_TOOL = {
    "name": "get_watchlist",
    "description": (
        "Get user's watchlists with stock items. "
        "Optionally includes latest prices for each stock."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Specific watchlist name to fetch. Omit for all watchlists.",
            },
            "include_prices": {
                "type": "boolean",
                "description": "Include latest closing price for each stock. Default: false.",
            },
        },
    },
}

GET_JOURNAL_INSIGHTS_TOOL = {
    "name": "get_journal_insights",
    "description": (
        "Analyze patterns in the trade journal. Shows win rate, P&L, R-multiple "
        "grouped by entry type, setup, rating, sector, or month. "
        "Answers 'what setup type works best?' or 'which rating predicts winners?'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "group_by": {
                "type": "string",
                "enum": ["entry_type", "setup", "rating", "sector", "month", "position_status"],
                "description": "Group trades by this field. Default: entry_type.",
            },
            "metrics": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["count", "win_rate", "avg_return", "sum_pl", "avg_r_multiple", "profit_factor"],
                },
                "description": "Metrics to compute per group. Default: count, win_rate, avg_return, avg_r_multiple.",
            },
        },
    },
}

COMPARE_TO_INDEX_TOOL = {
    "name": "compare_to_index",
    "description": (
        "Compare portfolio performance against a benchmark index "
        "(e.g. Nifty 50, Nifty Smallcap). Shows alpha generation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "index": {
                "type": "string",
                "description": "Index name like 'Nifty 50', 'Nifty Smallcap 250'. Default: Nifty 50.",
            },
            "fy": {
                "type": "string",
                "description": "Financial year for comparison, e.g. '2024-25'.",
            },
            "days": {
                "type": "integer",
                "description": "Number of trailing days instead of FY. e.g. 90 for last 3 months.",
            },
        },
    },
}

GET_FUNDAMENTALS_TOOL = {
    "name": "get_fundamentals",
    "description": (
        "Get fundamental data for a stock: financial ratios, quarterly/annual results, "
        "shareholding pattern, and industry peers. "
        "Use for questions about revenue, profit, EPS, ROE, debt, promoter holding, etc."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Stock symbol (e.g. 'RELIANCE', 'TCS').",
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["overview", "quarterly", "annual", "shareholding", "peers"],
                },
                "description": (
                    "Which sections to fetch. Default: ['overview']. "
                    "'overview' = key ratios (ROE, margins, debt). "
                    "'quarterly' = recent quarterly P&L. "
                    "'annual' = yearly financials + cash flow + ratios. "
                    "'shareholding' = promoter/FII/DII ownership. "
                    "'peers' = industry competitors."
                ),
            },
            "limit_quarters": {
                "type": "integer",
                "description": "Max quarterly results. Default: 8.",
            },
            "limit_years": {
                "type": "integer",
                "description": "Max annual results. Default: 5.",
            },
        },
        "required": ["symbol"],
    },
}


# ═══════════════════════════════════════════════════════════════════════════
#  KEPT FROM V3 (definitions only — executors imported above)
# ═══════════════════════════════════════════════════════════════════════════

GET_ANALYTICS_TOOL = {
    "name": "get_analytics",
    "description": (
        "Get pre-computed trading analytics for a financial year. "
        "'full' = complete dashboard (win rate, P&L, profit factor, top winners/losers). "
        "'outliers' = outlier analysis with return distribution. "
        "'advanced-v2' = equity curve, drawdown, streaks."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "endpoint": {
                "type": "string",
                "enum": ["full", "outliers", "advanced-v2"],
                "description": "Analytics type.",
            },
            "fy": {
                "type": "string",
                "description": "Financial year, e.g. '2025-26'.",
            },
        },
        "required": ["endpoint"],
    },
}

GET_EQUITY_CURVE_TOOL = {
    "name": "get_equity_curve",
    "description": (
        "Get long-term equity curve or drawdown analysis across all FYs. "
        "'long-term' = portfolio value over time with yearly returns. "
        "'drawdown-deep' = drawdown periods with recovery."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["long-term", "drawdown-deep"],
                "description": "Type of curve data.",
            },
        },
        "required": ["type"],
    },
}

GET_POSITIONS_TOOL = {
    "name": "get_positions",
    "description": "Get portfolio positions with current P&L, R-multiples, defensive status.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "closed", "all"],
                "description": "Filter by position status. Default: active.",
            },
        },
    },
}

GET_LIVE_MARKET_TOOL = {
    "name": "get_live_market",
    "description": (
        "Get market data for stocks: latest price, 52-week high/low, ATH, "
        "MA50/MA200, EMA20/50/200, liquidity, 5-day and 20-day momentum. "
        "Works for ALL listed stocks."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "NSE stock symbols (e.g. ['RELIANCE', 'TCS']).",
            },
            "include_ma": {
                "type": "boolean",
                "description": "Include moving averages. Default: true.",
            },
        },
        "required": ["symbols"],
    },
}

SEARCH_STOCK_TOOL = {
    "name": "search_stock",
    "description": "Search for a stock by name or symbol. Returns security_id, symbol, company name.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Stock name or symbol.",
            },
        },
        "required": ["query"],
    },
}


# ═══════════════════════════════════════════════════════════════════════════
#  SCORING TOOLS (read-only — specialist never scores)
# ═══════════════════════════════════════════════════════════════════════════

LOOKUP_SCORES_TOOL = {
    "name": "lookup_scores",
    "description": (
        "Look up Valvo score history from the submissions table for one or more stocks. "
        "Returns a history list per symbol (most recent first) — each entry has final "
        "score (0-10), rating, setup_type (OOB or Large Base), sector, date scored, "
        "top contributors, and raw judgment inputs. Use history_limit to get more than "
        "just the latest. Use since_date (ISO format like '2026-04-11') to filter to a "
        "time window — essential for queries like 'last week', 'this month', 'yesterday'. "
        "DOES NOT score — scoring happens on /scoring page only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Stock tickers or names. At least 1.",
            },
            "history_limit": {
                "type": "integer",
                "description": (
                    "Max recent submissions to return per symbol. Default 2 (latest + "
                    "prior for trend context). Bump to 5 for 'show me X scoring history'. "
                    "Max 10."
                ),
                "default": 2,
            },
            "fresh_days": {
                "type": "integer",
                "description": "Entries older than this are flagged fresh=False. Default 3.",
                "default": 3,
            },
            "since_date": {
                "type": "string",
                "description": (
                    "ISO date or timestamp. When user asks about a time window ('last "
                    "week', 'this month'), compute the date and pass it here. If no "
                    "submission exists in that window, results come back with found=false "
                    "and a note. Today's date is available in context."
                ),
            },
        },
        "required": ["symbols"],
    },
}


RANK_STOCKS_TOOL = {
    "name": "rank_stocks",
    "description": (
        "Rank 2+ stocks by their Valvo final_score (highest first). Near-ties within 0.1 "
        "are broken by sector_strength (True wins over False). If still tied, both are "
        "shown as a tied_pair — you must ask the user to judge. Stale scores are included "
        "with stale=True. Missing stocks are returned in missing_or_stale with a /scoring "
        "link. Use for 'pick best among', 'rank these 5', 'compare by setup quality'. "
        "DOES NOT score new names — user must score them on /scoring first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2 or more stock tickers or names to rank.",
                "minItems": 2,
            },
            "fresh_days": {
                "type": "integer",
                "description": "Freshness window in days. Default 3.",
                "default": 3,
            },
        },
        "required": ["symbols"],
    },
}


GET_TOP_SCORES_TOOL = {
    "name": "get_top_scores",
    "description": (
        "Return the user's top-scored stocks from the submissions table, deduplicated "
        "so each stock appears once at its latest score, sorted by final_score desc. "
        "Use this when the user wants the overall best / top / highest-scoring setups "
        "WITHOUT naming specific stocks. Examples: 'what's my best setup right now', "
        "'top 5 scored stocks', 'my excellent-rated picks', 'rank all my recent scores'. "
        "For a specific named list of stocks to compare, use rank_stocks instead. "
        "Default window is 30 days (users score weekly/biweekly, not daily). Tighten "
        "to 7 days only when the user explicitly says 'this week'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": (
                    "Max stocks to return. Default 10. Use 1 for 'best setup', "
                    "5 for 'top 5', etc. Max 50."
                ),
                "default": 10,
            },
            "fresh_days": {
                "type": "integer",
                "description": (
                    "Submission age window in days. Default 30 — covers user's active "
                    "scoring universe. Pass 7 for 'this week', 1 for 'today', 90 for "
                    "'this quarter'. Do NOT pass 3 (too tight, usually returns empty)."
                ),
                "default": 30,
            },
            "min_score": {
                "type": "number",
                "description": (
                    "Optional lower bound on final_score (0-10 scale). Pass 8 for "
                    "'excellent-rated', 6 for 'strong and above', etc."
                ),
            },
        },
    },
}


# ═══════════════════════════════════════════════════════════════════════════
#  REGIME TOOL (read-only; writes via v2 action set_market_regime)
# ═══════════════════════════════════════════════════════════════════════════

GET_REGIME_TOOL = {
    "name": "get_regime",
    "description": (
        "Return the user's current market regime + how long it's been active. "
        "Set include_history=True to also return regime-change history with "
        "per-regime duration (how long each past regime lasted before changing). "
        "Regime values: bull, sideways, grind_down, sharp_down. "
        "Use for queries like 'what's the regime', 'how long in bull', 'regime "
        "history'. To CHANGE the regime, call set_market_regime action instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "include_history": {
                "type": "boolean",
                "description": (
                    "If true, include history list with per-entry duration_days. "
                    "Default false (just the current regime)."
                ),
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": "Max history entries when included. Default 10, max 50.",
                "default": 10,
            },
        },
    },
}


GET_SECTORS_TOOL = {
    "name": "get_sectors",
    "description": (
        "Return sectoral-index health for Indian markets. 37 curated indices "
        "(Nifty Metal, Pharma, IT, Bank, Energy, etc. + broad market indices) "
        "are evaluated for MA health — each gets a 0-5 score based on whether "
        "price is above its 5/10/20/50/200-day moving averages. Also returns "
        "1-week and 1-month change and % distance from 200 MA. "
        "Use focus='metal' or focus='pharma' to narrow to one sector. "
        "Set include_leading=True to also include the user's leading_sectors "
        "(their manually-curated list of sectors they're bullish on from "
        "Settings). This is READ-ONLY — users change leading sectors via "
        "Settings page, not chat."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": (
                    "Sector/index name or symbol to narrow to one entry. "
                    "Examples: 'metal', 'pharma', 'bank nifty', 'auto', "
                    "'FINNIFTY'. Case-insensitive substring match."
                ),
            },
            "include_leading": {
                "type": "boolean",
                "description": (
                    "If true, also return the user's leading_sectors entry "
                    "from Settings. Default false."
                ),
                "default": False,
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Number of top + bottom entries to return when no focus. "
                    "Default 5, max 37."
                ),
                "default": 5,
            },
            "group": {
                "type": "string",
                "description": (
                    "Restrict to 'sectoral' (30 sector indices) or 'broad' "
                    "(7 broad market indices). Default both."
                ),
                "enum": ["sectoral", "broad"],
            },
        },
    },
}


GET_SECTOR_CONSTITUENTS_TOOL = {
    "name": "get_sector_constituents",
    "description": (
        "Return the stocks INSIDE a sector index with per-stock MA health. "
        "Use this when the user wants to drill INTO a sector: 'which stocks "
        "in metal are leading?', 'show me pharma stocks above 50MA', 'which "
        "bank nifty stocks are weakest?'. Returns the above and below lists "
        "plus breadth stats (% of stocks above the chosen MA). "
        "Complements get_sectors (which shows sector-level health) and "
        "compare_sectors (which compares multiple sectors). "
        "Sort is always by distance from MA (desc) — farthest-above first, "
        "most-negative first for below."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sector": {
                "type": "string",
                "description": (
                    "Sector name. Examples: 'metal', 'pharma', 'bank nifty', "
                    "'defence', 'auto', 'healthcare', 'EV'."
                ),
            },
            "ma_period": {
                "type": "integer",
                "description": "MA period for the above/below check. Default 20. Common: 5/10/20/50/200.",
                "default": 20,
            },
            "condition": {
                "type": "string",
                "description": (
                    "'above' = only stocks above the MA, 'below' = only below, "
                    "'all' = both (default)."
                ),
                "enum": ["above", "below", "all"],
                "default": "all",
            },
            "limit": {
                "type": "integer",
                "description": "Max stocks per slice (above/below each capped at limit). Default 25, max 100.",
                "default": 25,
            },
        },
        "required": ["sector"],
    },
}


COMPARE_SECTORS_TOOL = {
    "name": "compare_sectors",
    "description": (
        "Compare 2-5 sectors side by side on index-level health metrics: "
        "score (0-5 MA health), 1-week change, 1-month change, and % from "
        "200 MA. Use for 'compare metal vs pharma vs IT', 'is auto or bank "
        "stronger?', 'rotation between realty and bank'. For questions about "
        "stocks INSIDE one sector, use get_sector_constituents instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sectors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2 to 5 sector names (e.g. ['metal', 'pharma', 'IT']).",
                "minItems": 2,
                "maxItems": 5,
            },
        },
        "required": ["sectors"],
    },
}


# ═══════════════════════════════════════════════════════════════════════════
#  SQL ESCAPE HATCH (deprioritized in prompt)
# ═══════════════════════════════════════════════════════════════════════════

SQL_QUERY_TOOL = {
    "name": "sql_query",
    "description": (
        "Execute a read-only SQL query. ONLY use this when no semantic tool fits. "
        "Prefer query_trades, scan_stocks, get_watchlist, etc. over raw SQL. "
        "Returns up to 100 rows. SELECT/WITH only."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Read-only PostgreSQL query (SELECT/WITH).",
            },
        },
        "required": ["query"],
    },
}

CONFIRM_ACTION_TOOL = {
    "name": "confirm_action",
    "description": "Confirm a pending action requiring user approval.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pending_action_id": {
                "type": "string",
                "description": "Pending action UUID to confirm.",
            },
        },
        "required": ["pending_action_id"],
    },
}

CANCEL_ACTION_TOOL = {
    "name": "cancel_action",
    "description": "Cancel a pending action.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pending_action_id": {
                "type": "string",
                "description": "Pending action UUID to cancel.",
            },
        },
        "required": ["pending_action_id"],
    },
}


# ═══════════════════════════════════════════════════════════════════════════
#  SQL EXECUTOR (with better error wrapping)
# ═══════════════════════════════════════════════════════════════════════════

def _exec_sql_query(params: dict) -> dict:
    """SQL escape hatch — same as v3 but with friendlier error messages."""
    query = (params.get("query") or "").strip()
    error = _validate_sql(query)
    if error:
        return {"error": error}

    from database.database import get_db, close_db

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        if uid:
            cur.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(uid)}),),
            )
            cur.execute("SET LOCAL ROLE authenticated")

        # Add statement timeout to prevent runaway queries
        cur.execute("SET LOCAL statement_timeout = '15s'")

        cur.execute(query)
        rows = cur.fetchmany(100)
        result = [dict(r) for r in rows]
        text = json.dumps(to_jsonable(result), default=str, ensure_ascii=False)
        if len(text) > 8000:
            text = text[:8000] + "...(truncated)"
        return {
            "rows": json.loads(text) if len(text) <= 8000 else result[:50],
            "count": len(result),
            "row_count": len(result),
        }
    except Exception as exc:
        err_str = str(exc)
        # Friendlier error hints
        if "does not exist" in err_str:
            return {"error": f"SQL error: {err_str}", "hint": "Check table/column names against the schema."}
        if "permission denied" in err_str:
            return {"error": f"SQL error: {err_str}", "hint": "This table may require a different query approach."}
        if "statement timeout" in err_str:
            return {"error": "Query timed out (15s limit). Try a simpler query or use a semantic tool instead."}
        return {"error": f"SQL error: {err_str}"}
    finally:
        close_db(conn)


def _exec_v2_action(action_name: str, params: dict) -> dict:
    return to_jsonable(run_action(action_name, params, request_text="valvo-ai-v4"))


# ═══════════════════════════════════════════════════════════════════════════
#  REGISTRY
# ═══════════════════════════════════════════════════════════════════════════

# New semantic tools first (higher priority in the tool list)
_SEMANTIC_TOOLS = [
    QUERY_TRADES_TOOL,
    QUERY_MONTHLY_TOOL,
    SCAN_STOCKS_TOOL,
    GET_WATCHLIST_TOOL,
    GET_JOURNAL_INSIGHTS_TOOL,
    COMPARE_TO_INDEX_TOOL,
    GET_FUNDAMENTALS_TOOL,
    LOOKUP_SCORES_TOOL,
    RANK_STOCKS_TOOL,
    GET_TOP_SCORES_TOOL,
    GET_REGIME_TOOL,
    GET_SECTORS_TOOL,
    GET_SECTOR_CONSTITUENTS_TOOL,
    COMPARE_SECTORS_TOOL,
]

# Proven v3 tools
_PROVEN_TOOLS = [
    GET_ANALYTICS_TOOL,
    GET_EQUITY_CURVE_TOOL,
    GET_POSITIONS_TOOL,
    GET_LIVE_MARKET_TOOL,
    SEARCH_STOCK_TOOL,
]

# Escape hatch + meta
_UTILITY_TOOLS = [
    SQL_QUERY_TOOL,
    CONFIRM_ACTION_TOOL,
    CANCEL_ACTION_TOOL,
]

_EXECUTOR_MAP: dict[str, Any] = {
    # New semantic tools
    "query_trades": exec_query_trades,
    "query_monthly": exec_query_monthly,
    "scan_stocks": exec_scan_stocks,
    "get_watchlist": exec_get_watchlist,
    "get_journal_insights": exec_get_journal_insights,
    "compare_to_index": exec_compare_to_index,
    "get_fundamentals": exec_get_fundamentals,
    "lookup_scores": exec_lookup_scores,
    "rank_stocks": exec_rank_stocks,
    "get_top_scores": exec_get_top_scores,
    "get_regime": exec_get_regime,
    "get_sectors": exec_get_sectors,
    "get_sector_constituents": exec_get_sector_constituents,
    "compare_sectors": exec_compare_sectors,
    # Proven v3 executors
    "get_analytics": _exec_get_analytics,
    "get_equity_curve": _exec_get_equity_curve,
    "get_positions": _exec_get_positions,
    "get_live_market": exec_get_live_market,
    "search_stock": exec_search_stock,
    # Utility
    "sql_query": _exec_sql_query,
    "confirm_action": _exec_confirm_action,
    "cancel_action": _exec_cancel_action,
}


def get_all_tool_definitions() -> list[dict]:
    """Return complete tool schema list (all 23+ tools). Used for complex queries."""
    tools = list(_SEMANTIC_TOOLS) + list(_PROVEN_TOOLS) + list(_UTILITY_TOOLS)
    tools.extend(_v2_action_tool_defs())
    return tools


# Simple tier: fewer tools = smaller prompt = cheaper + faster
_SIMPLE_SEMANTIC = [QUERY_TRADES_TOOL, QUERY_MONTHLY_TOOL, GET_FUNDAMENTALS_TOOL, GET_WATCHLIST_TOOL]
_SIMPLE_PROVEN = [GET_ANALYTICS_TOOL, GET_POSITIONS_TOOL, GET_LIVE_MARKET_TOOL, SEARCH_STOCK_TOOL]
_SIMPLE_UTILITY = [SQL_QUERY_TOOL, CONFIRM_ACTION_TOOL, CANCEL_ACTION_TOOL]


def get_simple_tool_definitions() -> list[dict]:
    """Return reduced tool set for simple queries. ~15 tools instead of 23+."""
    tools = list(_SIMPLE_SEMANTIC) + list(_SIMPLE_PROVEN) + list(_SIMPLE_UTILITY)
    tools.extend(_v2_action_tool_defs())
    return tools


def execute_tool(name: str, params: dict) -> dict:
    """Execute a tool by name. Returns dict result."""
    executor = _EXECUTOR_MAP.get(name)
    if executor:
        try:
            return executor(params)
        except Exception as exc:
            traceback.print_exc()
            return {"error": f"Tool '{name}' failed: {exc}"}

    # Fall through to v2 action tools
    if name in V2_ACTIONS:
        try:
            return _exec_v2_action(name, params)
        except Exception as exc:
            traceback.print_exc()
            return {"error": f"Action '{name}' failed: {exc}"}

    return {"error": f"Unknown tool: {name}"}


def is_action_tool(tool_name: str) -> bool:
    """Check if a tool is a write action (needs history skip)."""
    if tool_name in {"confirm_action", "cancel_action"}:
        return True
    return tool_name in V2_ACTIONS
