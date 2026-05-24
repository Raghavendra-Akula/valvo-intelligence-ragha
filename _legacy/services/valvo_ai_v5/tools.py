"""
Valvo AI v5 -- Tool definitions and executors (cloned from v3).

Every tool is declared as a dict with Anthropic-compatible schema and a paired
executor function.  The engine calls ``get_all_tool_definitions()`` for the
schema list and ``execute_tool(name, input)`` at runtime.
"""
from __future__ import annotations

import json
import re
import traceback
from datetime import date
from typing import Any

from database.database import get_db, close_db

# ---------------------------------------------------------------------------
# Re-use v2 action infrastructure (confirmation flow, audit, pending table)
# ---------------------------------------------------------------------------
from services.valvo_ai_v2.actions import (
    ACTIONS as V2_ACTIONS,
    confirm_pending_action,
    cancel_pending_action,
    get_action_tools as _v2_action_tool_defs,
    run_action,
)
from services.valvo_ai_v2.utils import to_jsonable


# ---------------------------------------------------------------------------
# User context — get authenticated user from Flask request
# ---------------------------------------------------------------------------
def _get_user_id():
    """Get user_id from Flask g (set by auth middleware). Returns None if unavailable."""
    try:
        from flask import g
        return getattr(g, 'user_id', None)
    except RuntimeError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  TOOL DEFINITIONS  (Anthropic tool-use schema format)
# ═══════════════════════════════════════════════════════════════════════════

SQL_QUERY_TOOL = {
    "name": "sql_query",
    "description": (
        "FALLBACK: run a read-only PostgreSQL query (SELECT / WITH only, up "
        "to 100 rows). Use ONLY when no dedicated tool fits — dedicated "
        "tools are faster, cheaper, and less error-prone. DO NOT use for: "
        "live prices (use get_live_market), active portfolio (get_positions), "
        "current regime (get_market_regime), leading sectors "
        "(get_leading_sectors), pre-computed analytics (get_analytics), "
        "equity curve / drawdown (get_equity_curve), stock lookup "
        "(search_stock). DO use for: custom filters, aggregations, joins, "
        "historical drill-downs, anything ad-hoc."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A read-only PostgreSQL query (SELECT / WITH only). Prefer ILIKE over = for sector / setup / rating text columns.",
            },
        },
        "required": ["query"],
    },
}

GET_ANALYTICS_TOOL = {
    "name": "get_analytics",
    "description": (
        "Pre-computed FY analytics. Use this (NOT sql_query) for win rate, "
        "P&L summary, profit factor, top winners / losers, monthly trend, "
        "outlier distribution, equity curve, drawdown, streaks, holding "
        "period. Endpoints: 'full' = headline stats + top 5 winners + top "
        "5 losers; 'outliers' = PnL distribution buckets + >3R trades; "
        "'advanced-v2' = equity curve + max drawdown + win/loss streaks."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "endpoint": {
                "type": "string",
                "enum": ["full", "outliers", "advanced-v2"],
                "description": "Which analytics endpoint to query.",
            },
            "fy": {
                "type": "string",
                "description": "FY label e.g. '2024-25'. Defaults to current FY.",
            },
        },
        "required": ["endpoint"],
    },
}

GET_EQUITY_CURVE_TOOL = {
    "name": "get_equity_curve",
    "description": (
        "Multi-year equity trajectory. Use for questions spanning ALL FYs: "
        "\"my equity curve\", \"portfolio growth over the years\", \"my "
        "worst drawdown\". 'long-term' returns per-FY start / net / end / "
        "return%. 'drawdown-deep' returns max drawdown % per FY."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["long-term", "drawdown-deep"],
                "description": "'long-term' = year-over-year capital growth; 'drawdown-deep' = deepest DD per year.",
            },
        },
        "required": ["type"],
    },
}

GET_LIVE_MARKET_TOOL = {
    "name": "get_live_market",
    "description": (
        "Latest close + 5 / 10 / 20-day moving averages for up to 10 STOCK "
        "symbols. Use for \"current price\", \"where is X trading\", \"is X "
        "above its MA\" on an equity ticker. Data source: candles_daily. "
        "DO NOT call before an update_stop_loss with an EMA trailing_mode "
        "(ema20/ema50/ema200) — the backend recomputes the EMA from "
        "trailing_mode. The CMP of any active position is already in "
        "LIVE STATE, so a preflight fetch before create/pyramid/sell is "
        "redundant too. DOES NOT SUPPORT INDICES — for \"how is NIFTY "
        "METAL / BANKNIFTY / NIFTY IT\" questions, call get_index_snapshot "
        "instead (separate table candles_indices + index_daily_summary). "
        "Passing an index name here will return \"stock not found\"."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "NSE tickers (e.g. ['RELIANCE', 'TCS']). Max 10.",
            },
            "include_ma": {
                "type": "boolean",
                "description": "Include 5/10/20-day MAs. Default true.",
            },
        },
        "required": ["symbols"],
    },
}

GET_POSITIONS_TOOL = {
    "name": "get_positions",
    "description": (
        "FULL per-position analytics: R-multiples, defensive_status, "
        "next_result_date / days_left for event risk, and historical "
        "fields. Use for questions the LIVE STATE snapshot can't answer "
        "(sell history, event risk, closed positions, deep analytics). "
        "DO NOT call this before a write action (log_trade / "
        "pyramid_position / update_stop_loss / record_sell / "
        "close_position) — the LIVE STATE block at the top of your "
        "context already lists held stocks, qty, entry, CMP, SL; the "
        "write action has everything it needs. Preflighting with "
        "get_positions wastes tokens and a round trip. "
        "RESPONSE shape: {count, totals: {value, pl, pl_pct, cost}, "
        "positions: [...]} — the `totals` object is PRE-COMPUTED. "
        "When the user asks for total value or total P&L, COPY from "
        "totals; do not sum the rows yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "closed", "all"],
                "description": "Position status filter. Default 'active'.",
            },
        },
    },
}

SEARCH_STOCK_TOOL = {
    "name": "search_stock",
    "description": (
        "Resolve a stock name fragment to security_id + canonical symbol. "
        "Use before calling get_live_market if the user says \"TaTa steel\" "
        "rather than the ticker. Matches symbol OR company_name via ILIKE, "
        "returns top 10."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Partial stock name or symbol.",
            },
        },
        "required": ["query"],
    },
}

GET_FUNDAMENTALS_TOOL = {
    "name": "get_fundamentals",
    "description": (
        "Fundamentals snapshot for one stock (P/E, P/B, ROE, RoCE, margins, debt "
        "ratios, growth CAGRs, 52-week range, promoter/FII/DII holding). Returns "
        "the fundamentals_overview row plus the latest shareholding and up to 5 "
        "peers. Use for any 'what's the P/E of X', 'show me X fundamentals', "
        "'X vs Y' comparison, 'X peers' question. Prefer this over sql_query — "
        "joins the right tables automatically. For time-series (quarterly / annual "
        "line items over multiple periods) use sql_query on financials_quarterly "
        "or financials_annual instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "NSE ticker (e.g. 'RELIANCE', 'TCS'). Accepts partial match via ILIKE.",
            },
        },
        "required": ["symbol"],
    },
}

GET_COMPARE_STOCKS_TOOL = {
    "name": "get_compare_stocks",
    "description": (
        "Side-by-side comparison of 2-4 stocks. Returns per-stock "
        "fundamentals (P/E, P/B, ROE, RoCE, margins, debt ratios), "
        "price context (current, MA50/MA200 alignment, 52w range), "
        "growth (1y/3y/5y price + sales + profit CAGRs), and the "
        "user's position in each (if any). Use for \"INFY vs TCS\", "
        "\"compare A B C\", \"my holdings vs peers\". Output is "
        "structured so the model can build a comparison table the "
        "user instantly scans. DO NOT chain 3 get_stock_snapshot "
        "calls — this tool resolves all symbols in one round-trip."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-4 NSE tickers or fragments (e.g. ['INFY', 'TCS', 'WIPRO']).",
            },
        },
        "required": ["symbols"],
    },
}

GET_INDEX_SNAPSHOT_TOOL = {
    "name": "get_index_snapshot",
    "description": (
        "Snapshot of any NSE index — broad (NIFTY, BANKNIFTY, NIFTY 500) or "
        "sector (NIFTY METAL, NIFTY IT, NIFTY PHARMA, etc.). Returns latest "
        "close, returns over 5d/20d/60d/252d, MA50/MA200 alignment, 52w "
        "range, category ('broad'/'sector'/'thematic'), and leadership "
        "score for sector indices. Use this — NOT get_live_market — for "
        "questions like \"how is NIFTY METAL today\" / \"what's BANKNIFTY \"\n"
        "\"doing\" / \"year-to-date NIFTY IT\". Accepts an index name "
        "fragment (ILIKE on symbol + category aliases)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "NSE index name (e.g. 'NIFTY METAL', 'BANKNIFTY', 'Metal', 'IT') — ILIKE fuzzy match.",
            },
        },
        "required": ["symbol"],
    },
}

GET_STOCK_SNAPSHOT_TOOL = {
    "name": "get_stock_snapshot",
    "description": (
        "360° single-stock view. ONE call returns fundamentals snapshot "
        "(P/E, P/B, ROE, margins, 52w range), live-ish price + MAs from "
        "stock_daily_summary, the user's position in the stock (if any — "
        "active or closed, with P&L), recent closed-trade history in the "
        "stock, latest scoring submission if any, and active price alerts. "
        "Use for ANY \"tell me everything about X\" / \"what do I know "
        "about X\" / \"how does X look\" question. Beats calling "
        "get_fundamentals + get_live_market + get_positions + sql_query "
        "separately. Accepts symbol (NSE ticker or fragment; resolved via "
        "ILIKE on stock_fundamentals.symbol, stock_fundamentals.nse_code, "
        "and stock_universe.symbol)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "NSE ticker or fragment (e.g. 'RELIANCE', 'nalco', 'HITACHI').",
            },
        },
        "required": ["symbol"],
    },
}

GET_POSITIONS_SECTOR_MEMBERSHIP_TOOL = {
    "name": "get_positions_sector_membership",
    "description": (
        "One-shot cross-reference of every ACTIVE position against NSE sector "
        "indices via index_constituents, with a flag for which positions sit "
        "in the CURRENT leading sectors (from index_daily_summary). Returns "
        "{leading_sectors, positions:[{stock_name, security_id, all_indices, "
        "all_sectors, leading_sector_memberships}], matches_in_leading, "
        "count_matching}. ALWAYS use this for \"my positions in leading "
        "sectors\", \"which of my stocks are in hot sectors\", \"is NALCO "
        "in a leading sector\" — it does the correct security_id join "
        "internally. Do NOT try to replicate this with sql_query joining "
        "on stock_name/symbol strings: positions stores display names "
        "(\"HITACHI ENERGY INDIA LTD\"), index_constituents stores NSE "
        "tickers (\"POWERINDIA\") — string joins silently miss real matches."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit_sectors": {
                "type": "integer",
                "description": "How many leading sectors to check against (1-20, default 5).",
            },
        },
    },
}

GET_MARKET_REGIME_TOOL = {
    "name": "get_market_regime",
    "description": (
        "The CURRENT market regime the user has journaled (latest row of "
        "market_regime_history). Use for a direct question about the "
        "regime OR when the user wants regime history. The current "
        "regime is also in LIVE STATE, so for a one-off 'what's the "
        "regime' the LIVE STATE line is enough — only call this tool "
        "when the user asks for more (history, transitions, notes). "
        "DO NOT confuse with positions.market_regime — that field is "
        "a per-position snapshot."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "history": {
                "type": "integer",
                "description": "How many recent regime rows to return (1-10, default 1).",
            },
        },
    },
}

GET_LEADING_SECTORS_TOOL = {
    "name": "get_leading_sectors",
    "description": (
        "Top sector indices ranked automatically by leadership score "
        "(composite of 20d return, 60d return, MA50 alignment — derived "
        "from the index_daily_summary table, refreshed from "
        "candles_indices). Use for any \"leading sectors / sector leaders "
        "/ what sectors are working / hot sectors\" question. Returns "
        "sector names (pretty), raw NSE symbols, short-term returns, and "
        "an explanation of the ranking. When cross-referencing with "
        "user's positions or stocks, use case-insensitive substring match "
        "on the sector name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many leading sectors to return (1-20, default 5).",
            },
        },
    },
}

CONFIRM_ACTION_TOOL = {
    "name": "confirm_action",
    "description": (
        "Confirm a pending action that requires user approval. "
        "Pass the pending_action_id returned by a previous action tool call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pending_action_id": {
                "type": "string",
                "description": "The pending action UUID to confirm.",
            },
        },
        "required": ["pending_action_id"],
    },
}

CANCEL_ACTION_TOOL = {
    "name": "cancel_action",
    "description": (
        "Cancel a pending action. Pass the pending_action_id to cancel."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pending_action_id": {
                "type": "string",
                "description": "The pending action UUID to cancel.",
            },
        },
        "required": ["pending_action_id"],
    },
}


# ═══════════════════════════════════════════════════════════════════════════
#  SQL VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

_DML_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE|CREATE|GRANT|REVOKE|VACUUM|COPY)\b",
    re.IGNORECASE,
)

_ALLOWED_START = re.compile(
    r"^\s*(SELECT|WITH)\b",
    re.IGNORECASE,
)


def _validate_sql(query: str) -> str | None:
    """Return an error string if the query is not a valid read-only SELECT/WITH."""
    stripped = query.strip().rstrip(";").strip()
    if not stripped:
        return "Empty query"
    if not _ALLOWED_START.match(stripped):
        return "Only SELECT and WITH statements are allowed."
    # Scan for forbidden DML keywords outside of string literals.
    # Simple heuristic: strip quoted strings, then check.
    sanitized = re.sub(r"'[^']*'", "''", stripped)
    if _DML_PATTERN.search(sanitized):
        return "Query contains forbidden DML keywords. Only SELECT/WITH queries are permitted."
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  TOOL EXECUTORS
# ═══════════════════════════════════════════════════════════════════════════

# Pattern: `column = 'literal'` (quoted string literal) with optional schema-qualified column.
# We do NOT touch `column = %s`, `column = number`, `column = NULL`, or already-ILIKE/LIKE clauses.
_STRING_EQ_PATTERN = re.compile(
    r"""
    (?<![\w"])                      # not preceded by identifier char / closing quote
    ([\w\.\"]+)                     # column (supports schema."col")
    \s*=\s*                         # equals, any whitespace
    '((?:[^']|'')+)'                # single-quoted string literal (handles '' escapes)
    """,
    re.VERBOSE,
)


def _rewrite_string_equality_to_ilike(query: str) -> str | None:
    """Rewrite `col = 'x'` → `col ILIKE '%x%'`. Returns None if nothing to rewrite.

    Only touches string equalities the regex recognises; leaves numeric, NULL,
    parameterised, and already-LIKE/ILIKE clauses alone. Used as a fallback
    retry path when the model's first query returns 0 rows — sector and
    setup_type columns in this DB are stored with inconsistent capitalisation,
    which is the most common cause of spurious empties.
    """
    new_query, n = _STRING_EQ_PATTERN.subn(
        lambda m: f"{m.group(1)} ILIKE '%{m.group(2)}%'",
        query,
    )
    return new_query if n > 0 and new_query != query else None


def _run_sql(cur, query: str) -> list[dict]:
    cur.execute(query)
    return [dict(r) for r in cur.fetchmany(100)]


def _exec_sql_query(params: dict) -> dict:
    query = (params.get("query") or "").strip()
    error = _validate_sql(query)
    if error:
        return {"error": error}

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()

        # Enforce RLS — set authenticated role so auth.uid() returns this user's ID
        # Without this, service_role bypasses RLS and sees ALL users' data
        user_id = _get_user_id()
        if user_id:
            cur.execute("SELECT set_config('request.jwt.claims', %s, true)",
                        (json.dumps({"sub": str(user_id)}),))
            cur.execute("SET LOCAL ROLE authenticated")

        result = _run_sql(cur, query)
        retry_note = None

        # ILIKE auto-retry: if the original query returned 0 rows and contains
        # a string equality (`col = 'x'`), silently retry with ILIKE. This
        # catches the "sector = 'Metal' returned empty because it's stored as
        # 'Metals & Mining'" class of bug without the model burning a round.
        if not result:
            retry_sql = _rewrite_string_equality_to_ilike(query)
            if retry_sql:
                try:
                    retry_rows = _run_sql(cur, retry_sql)
                    if retry_rows:
                        result = retry_rows
                        retry_note = (
                            "Original exact-match query returned 0 rows; "
                            "auto-retried with ILIKE fuzzy match."
                        )
                except Exception:
                    # Retry failure is non-fatal — fall through with empty result
                    pass

        text = json.dumps(to_jsonable(result), default=str, ensure_ascii=False)
        if len(text) > 8000:
            text = text[:8000] + "...(truncated)"

        payload = {
            "rows": json.loads(text) if len(text) <= 8000 else result[:50],
            "count": len(result),
            "row_count": len(result),
        }
        if retry_note:
            payload["_retry_note"] = retry_note
        return payload
    except Exception as exc:
        return {"error": f"SQL error: {exc}"}
    finally:
        try:
            close_db(conn)
        except Exception:
            pass


def _next_result_for_security(cur, security_id) -> dict | None:
    """Return the nearest upcoming results board-meeting for a stock as
    {date, days_left, purpose}, or None when no filing is on file. Same
    filter the position manager / watchlist use."""
    if not security_id:
        return None
    try:
        cur.execute(
            """
            SELECT meeting_date, purpose,
                   (meeting_date - CURRENT_DATE) AS days_left
            FROM forthcoming_results
            WHERE security_id = %s
              AND meeting_date >= CURRENT_DATE
              AND (purpose ILIKE %s OR raw_purpose ILIKE %s
                   OR raw_purpose ILIKE %s OR raw_purpose ILIKE %s)
            ORDER BY meeting_date ASC,
                     (purpose ILIKE %s) DESC,
                     (purpose ILIKE %s) DESC
            LIMIT 1
            """,
            (str(security_id),
             "%result%", "%financial result%", "%audited%", "%quarterly result%",
             "%financial result%", "%result%"),
        )
        row = cur.fetchone()
        if not row:
            return None
        md = row.get("meeting_date")
        return {
            "date": md.isoformat() if md and hasattr(md, "isoformat") else md,
            "days_left": row.get("days_left"),
            "purpose": row.get("purpose"),
        }
    except Exception:
        return None


def _exec_get_fundamentals(params: dict) -> dict:
    """Snapshot of fundamentals for one stock. Pulls the full overview row, the
    latest shareholding breakdown, and up to 5 peers in one round-trip so the
    model doesn't have to plan 3 separate queries."""
    query = (params.get("symbol") or "").strip()
    if not query:
        return {"error": "symbol is required"}

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()

        # Resolve to a canonical symbol via fundamentals_overview first (ILIKE so
        # "tatasteel" matches "TATASTEEL"). Fall back to stock_universe if the
        # stock has no fundamentals row yet.
        cur.execute(
            "SELECT * FROM fundamentals_overview "
            "WHERE symbol ILIKE %s OR nse_code ILIKE %s OR company_name ILIKE %s "
            "ORDER BY CASE WHEN symbol ILIKE %s THEN 0 ELSE 1 END "
            "LIMIT 1",
            (query, query, f"%{query}%", query),
        )
        overview = cur.fetchone()
        if not overview:
            return {
                "type": "fundamentals",
                "symbol": query,
                "error": f"No fundamentals snapshot for '{query}'. Try a different symbol or fall back to sql_query on stock_universe.",
            }
        overview = dict(overview)
        symbol = overview.get("symbol") or overview.get("nse_code")
        security_id = overview.get("security_id")

        # Latest shareholding quarter
        shareholding = None
        try:
            cur.execute(
                "SELECT period, period_end_date, promoter_percent, promoter_pledge_percent, "
                "fii_percent, dii_percent, mutual_fund_percent, public_percent, "
                "number_of_shareholders "
                "FROM shareholding_quarterly "
                "WHERE security_id = %s OR symbol = %s "
                "ORDER BY period_end_date DESC LIMIT 1",
                (security_id, symbol),
            )
            sh = cur.fetchone()
            if sh:
                shareholding = dict(sh)
        except Exception:
            shareholding = None

        # Top 5 peers by relevance
        peers = []
        try:
            cur.execute(
                "SELECT peer_symbol, peer_security_id, industry, relevance_rank "
                "FROM peers "
                "WHERE security_id = %s OR symbol = %s "
                "ORDER BY relevance_rank ASC NULLS LAST LIMIT 5",
                (security_id, symbol),
            )
            peers = [dict(r) for r in cur.fetchall()]
        except Exception:
            peers = []

        # Upcoming earnings — gives the model event-risk awareness when the
        # user asks "should I buy / hold / add X?" without needing a separate
        # tool call.
        next_result = _next_result_for_security(cur, security_id)

        return {
            "type": "fundamentals",
            "symbol": symbol,
            "security_id": security_id,
            "overview": to_jsonable(overview),
            "shareholding_latest": to_jsonable(shareholding),
            "peers": to_jsonable(peers),
            "next_result": to_jsonable(next_result),
        }
    except Exception as exc:
        return {"error": f"Fundamentals error: {exc}"}
    finally:
        close_db(conn)


def _exec_get_compare_stocks(params: dict) -> dict:
    """Side-by-side comparison for 2-4 stocks. Aggregates fundamentals +
    price context + growth metrics + user's current position into a
    single structured payload the model can format as a comparison
    table. Much cheaper than three separate get_stock_snapshot calls
    because we skip trade history / scoring / alerts (which aren't
    comparison-relevant) and issue one query per data source."""
    raw_symbols = params.get("symbols") or []
    if isinstance(raw_symbols, str):
        # Tolerate "INFY, TCS, WIPRO" comma-separated string too.
        raw_symbols = [s.strip() for s in raw_symbols.split(",") if s.strip()]
    if not isinstance(raw_symbols, list) or not raw_symbols:
        return {"error": "Provide 2-4 symbols to compare"}
    if len(raw_symbols) < 2:
        return {"error": "Need at least 2 symbols — use get_stock_snapshot for a single stock"}
    if len(raw_symbols) > 4:
        raw_symbols = raw_symbols[:4]   # silently cap; comparison of 5+ stops being glanceable

    uid = _get_user_id()
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()

        # Resolve each symbol to a canonical (symbol, security_id). Prefer
        # fundamentals_overview (most consistent), fall back to
        # stock_universe for anything without fundamentals coverage.
        resolved = []
        not_found = []
        for raw in raw_symbols:
            q = (raw or "").strip()
            if not q:
                continue
            cur.execute(
                "SELECT security_id, symbol, company_name, sector, industry "
                "FROM fundamentals_overview "
                "WHERE symbol ILIKE %s OR nse_code ILIKE %s OR company_name ILIKE %s "
                "ORDER BY CASE WHEN symbol ILIKE %s THEN 0 ELSE 1 END LIMIT 1",
                (q, q, f"%{q}%", q),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT security_id, symbol, company_name FROM stock_universe "
                    "WHERE symbol ILIKE %s OR company_name ILIKE %s "
                    "ORDER BY CASE WHEN symbol ILIKE %s THEN 0 ELSE 1 END LIMIT 1",
                    (q, f"%{q}%", q),
                )
                row = cur.fetchone()
            if not row:
                not_found.append(q)
                continue
            r = dict(row)
            resolved.append({
                "requested":     q,
                "symbol":        r.get("symbol"),
                "security_id":   r.get("security_id"),
                "company":       r.get("company_name"),
                "sector":        r.get("sector"),
                "industry":      r.get("industry"),
            })

        if not resolved:
            return {
                "type":      "compare_stocks",
                "stocks":    [],
                "not_found": not_found,
                "error":     f"No matching stocks for {raw_symbols}. Try NSE tickers.",
            }

        # Fetch all fundamentals rows for resolved security_ids in one query.
        sids = [s["security_id"] for s in resolved if s.get("security_id")]
        fundamentals_by_sid = {}
        if sids:
            placeholders = ",".join(["%s"] * len(sids))
            cur.execute(
                f"SELECT security_id, pe_ratio, pb_ratio, ev_to_ebitda, "
                f"       eps_ttm, roe, roce, roa, "
                f"       gross_profit_margin, operating_profit_margin, net_profit_margin, "
                f"       debt_to_equity, net_debt_to_equity, interest_coverage, current_ratio, "
                f"       promoter_holding_pct, dividend_yield, "
                f"       sales_growth_3yr_cagr, sales_growth_5yr_cagr, "
                f"       profit_growth_3yr_cagr, profit_growth_5yr_cagr, "
                f"       price_cagr_1yr, price_cagr_3yr, price_cagr_5yr, "
                f"       market_cap_cr, enterprise_value_cr, "
                f"       week_52_high, week_52_low, current_price "
                f"FROM fundamentals_overview "
                f"WHERE security_id IN ({placeholders})",
                sids,
            )
            for r in cur.fetchall():
                fundamentals_by_sid[r["security_id"]] = dict(r)

        # Stock daily summary (live-ish close + MAs)
        market_by_sid = {}
        if sids:
            placeholders = ",".join(["%s"] * len(sids))
            try:
                cur.execute(
                    f"SELECT security_id, prev_close, ma50, ma200, ema50, ema200, "
                    f"       high_52w, low_52w, close_5d, close_20d, close_60d, close_252d "
                    f"FROM stock_daily_summary "
                    f"WHERE security_id IN ({placeholders})",
                    sids,
                )
                for r in cur.fetchall():
                    market_by_sid[r["security_id"]] = dict(r)
            except Exception:
                market_by_sid = {}

        # User's positions in these stocks (any status — active or closed)
        position_by_sid = {}
        if uid and sids:
            placeholders = ",".join(["%s"] * len(sids))
            try:
                cur.execute(
                    f"SELECT security_id, stock_name, status, entry_price, "
                    f"       current_price, current_r_multiple, total_pnl_pct "
                    f"FROM positions "
                    f"WHERE user_id = %s AND security_id IN ({placeholders}) "
                    f"ORDER BY id DESC",
                    [uid] + sids,
                )
                for r in cur.fetchall():
                    # Keep only the most recent row per security_id
                    if r["security_id"] not in position_by_sid:
                        position_by_sid[r["security_id"]] = dict(r)
            except Exception:
                position_by_sid = {}

        # Assemble the per-stock rows
        stocks_out = []
        for s in resolved:
            sid = s.get("security_id")
            f = fundamentals_by_sid.get(sid) or {}
            m = market_by_sid.get(sid) or {}
            p = position_by_sid.get(sid)
            current = m.get("prev_close") or f.get("current_price")
            ma50 = m.get("ma50")
            ma200 = m.get("ma200")
            stocks_out.append({
                "symbol":         s["symbol"],
                "company":        s["company"],
                "sector":         s.get("sector"),
                "industry":       s.get("industry"),
                "current_price":  float(current) if current is not None else None,
                "market_cap_cr":  float(f.get("market_cap_cr")) if f.get("market_cap_cr") is not None else None,
                "fundamentals":   {
                    "pe":            f.get("pe_ratio"),
                    "pb":            f.get("pb_ratio"),
                    "ev_ebitda":     f.get("ev_to_ebitda"),
                    "roe":           f.get("roe"),
                    "roce":          f.get("roce"),
                    "roa":           f.get("roa"),
                    "opm":           f.get("operating_profit_margin"),
                    "npm":           f.get("net_profit_margin"),
                    "de":            f.get("debt_to_equity"),
                    "interest_cov":  f.get("interest_coverage"),
                    "current_ratio": f.get("current_ratio"),
                    "dividend_yld":  f.get("dividend_yield"),
                    "promoter_pct":  f.get("promoter_holding_pct"),
                    "eps_ttm":       f.get("eps_ttm"),
                },
                "growth":         {
                    "sales_3y_cagr":  f.get("sales_growth_3yr_cagr"),
                    "sales_5y_cagr":  f.get("sales_growth_5yr_cagr"),
                    "profit_3y_cagr": f.get("profit_growth_3yr_cagr"),
                    "profit_5y_cagr": f.get("profit_growth_5yr_cagr"),
                    "price_1y_cagr":  f.get("price_cagr_1yr"),
                    "price_3y_cagr":  f.get("price_cagr_3yr"),
                    "price_5y_cagr":  f.get("price_cagr_5yr"),
                },
                "price_context":  {
                    "ma50":         float(ma50) if ma50 is not None else None,
                    "ma200":        float(ma200) if ma200 is not None else None,
                    "above_ma50":   (current > ma50) if (current is not None and ma50 is not None) else None,
                    "above_ma200":  (current > ma200) if (current is not None and ma200 is not None) else None,
                    "high_52w":     float(m.get("high_52w") or f.get("week_52_high") or 0) or None,
                    "low_52w":      float(m.get("low_52w") or f.get("week_52_low") or 0) or None,
                },
                "user_position":  to_jsonable(p) if p else None,
            })

        return {
            "type":       "compare_stocks",
            "stocks":     to_jsonable(stocks_out),
            "not_found":  not_found,
            "count":      len(stocks_out),
        }
    except Exception as exc:
        return {"error": f"Compare stocks error: {exc}"}
    finally:
        close_db(conn)


def _exec_get_index_snapshot(params: dict) -> dict:
    """Snapshot from index_daily_summary for one NSE index. Fuzzy-matches the
    symbol (ILIKE) so 'metal' → 'NIFTY METAL', 'banknifty' → 'BANKNIFTY',
    etc. Falls back to a freshness refresh if the summary is stale, same
    lazy pattern as the sector routes."""
    query = (params.get("symbol") or "").strip()
    if not query:
        return {"error": "symbol is required"}

    # Kick a background refresh if >12h stale so the reader always sees
    # up-to-date numbers without waiting.
    try:
        from services.index_summary import trigger_background_refresh_if_stale
        trigger_background_refresh_if_stale()
    except Exception:
        pass

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        # Normalise some common user aliases to the stored symbol form.
        # Examples: "banknifty" (no space) → BANKNIFTY; "metal" → NIFTY METAL.
        # The ILIKE handles the fuzz; this just improves match precedence.
        alias_probe = f"%{query}%"
        cur.execute(
            """
            SELECT symbol, category, prev_close, ma50, ma200, high_52w, low_52w, ath,
                   close_5d, close_20d, close_60d, close_120d, close_252d,
                   return_5d, return_20d, return_60d, return_252d,
                   above_ma50, above_ma200, leadership_score,
                   computed_date, updated_at
            FROM index_daily_summary
            WHERE symbol ILIKE %s OR symbol ILIKE %s
            ORDER BY
                CASE
                    WHEN symbol ILIKE %s THEN 0                -- exact
                    WHEN symbol ILIKE %s THEN 1                -- prefix "NIFTY X"
                    ELSE 2
                END,
                category = 'sector' DESC,
                category = 'broad'  DESC
            LIMIT 1
            """,
            (alias_probe, f"NIFTY {query}%", query, f"NIFTY {query}%"),
        )
        row = cur.fetchone()
        if not row:
            return {"type": "index_snapshot", "symbol": query, "error": f"No index matches '{query}'."}
        row = dict(row)

        # Today's change from yesterday's close (close_5d is 5 trading days
        # ago, but candles_indices has finer granularity; query for previous
        # trading day explicitly so "performing today" is meaningful).
        today_change_pct = None
        today_close = None
        try:
            cur.execute(
                """
                SELECT date, close
                FROM candles_indices
                WHERE symbol = %s
                ORDER BY date DESC
                LIMIT 2
                """,
                (row["symbol"],),
            )
            recent = cur.fetchall()
            if len(recent) >= 1:
                today_close = float(recent[0]["close"]) if recent[0]["close"] is not None else None
            if len(recent) >= 2 and recent[0]["close"] and recent[1]["close"]:
                prev = float(recent[1]["close"])
                cur_c = float(recent[0]["close"])
                today_change_pct = ((cur_c - prev) / prev) * 100 if prev else None
        except Exception:
            pass

        return {
            "type": "index_snapshot",
            "symbol": row.get("symbol"),
            "category": row.get("category"),
            "today_close": today_close,
            "today_change_pct": today_change_pct,
            "prev_close": row.get("prev_close"),
            "ma50": row.get("ma50"),
            "ma200": row.get("ma200"),
            "above_ma50": row.get("above_ma50"),
            "above_ma200": row.get("above_ma200"),
            "high_52w": row.get("high_52w"),
            "low_52w": row.get("low_52w"),
            "ath": row.get("ath"),
            "return_5d": row.get("return_5d"),
            "return_20d": row.get("return_20d"),
            "return_60d": row.get("return_60d"),
            "return_252d": row.get("return_252d"),
            "leadership_score": row.get("leadership_score"),
            "computed_date": str(row.get("computed_date")) if row.get("computed_date") else None,
            "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None,
        }
    except Exception as exc:
        return {"error": f"Index snapshot error: {exc}"}
    finally:
        close_db(conn)


def _exec_get_stock_snapshot(params: dict) -> dict:
    """One-shot 360° view of a stock. Resolves symbol → security_id → pulls
    fundamentals, price/MA, 52w range, the user's position (if any), recent
    trade history in the stock, latest scoring submission, and active price
    alerts. Designed so the model doesn't have to fire five tool calls for
    a single "tell me about X" question.
    """
    query = (params.get("symbol") or "").strip()
    if not query:
        return {"error": "symbol is required"}

    uid = _get_user_id()
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()

        # 1. Resolve the symbol. Prefer fundamentals_overview (most reliable),
        #    fall back to stock_universe. We grab security_id + canonical
        #    symbol so every downstream lookup joins cleanly.
        cur.execute(
            """
            SELECT security_id, symbol, nse_code, company_name, sector, industry
            FROM fundamentals_overview
            WHERE symbol ILIKE %s OR nse_code ILIKE %s OR company_name ILIKE %s
            ORDER BY CASE WHEN symbol ILIKE %s THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (query, query, f"%{query}%", query),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                "SELECT security_id, symbol, company_name "
                "FROM stock_universe "
                "WHERE symbol ILIKE %s OR company_name ILIKE %s "
                "ORDER BY CASE WHEN symbol ILIKE %s THEN 0 ELSE 1 END "
                "LIMIT 1",
                (query, f"%{query}%", query),
            )
            row = cur.fetchone()
        if not row:
            return {"type": "stock_snapshot", "symbol": query, "error": f"No stock matches '{query}'."}
        base = dict(row)
        symbol = base.get("symbol") or base.get("nse_code")
        security_id = base.get("security_id")

        # 2. Fundamentals overview (skip if we already have it; else fetch)
        overview = None
        if "pe_ratio" in base or "roe" in base:
            # Came from fundamentals_overview — hydrate the rest
            cur.execute("SELECT * FROM fundamentals_overview WHERE security_id = %s", (security_id,))
            fo = cur.fetchone()
            if fo:
                overview = dict(fo)
        else:
            cur.execute("SELECT * FROM fundamentals_overview WHERE security_id = %s", (security_id,))
            fo = cur.fetchone()
            if fo:
                overview = dict(fo)

        # 3. Live-ish price snapshot from stock_daily_summary (prev_close is
        #    live-updated by the WebSocket triggers; MAs and 52w are refreshed
        #    daily).
        market = None
        try:
            cur.execute(
                "SELECT prev_close, ma50, ma200, ema20, ema50, ema200, "
                "       high_52w, low_52w, close_5d, close_20d, close_60d, "
                "       close_252d, liq_cr, is_etf, computed_date "
                "FROM stock_daily_summary WHERE security_id = %s",
                (security_id,),
            )
            m = cur.fetchone()
            if m:
                market = dict(m)
        except Exception:
            market = None

        # 4. User's position (if any) + recent sell events for context
        position = None
        if uid:
            try:
                cur.execute(
                    "SELECT stock_name, status, entry_price, quantity, stop_loss, "
                    "       current_price, current_r_multiple, defensive_status, "
                    "       trailing_mode, trailing_sl, bucket_sold_pct, "
                    "       exit_price, exit_date, total_pnl, total_pnl_pct, "
                    "       sell_history, created_at "
                    "FROM positions "
                    "WHERE user_id = %s AND security_id = %s "
                    "ORDER BY id DESC LIMIT 1",
                    (uid, security_id),
                )
                p = cur.fetchone()
                if p:
                    position = dict(p)
            except Exception:
                position = None

        # 5. Trade history in this stock across all FYs (closed trades only,
        #    last 10 ordered by recency). Uses journal_trades_computed (current
        #    FY) UNION with legacy tables, matched by symbol AND user_id —
        #    every one of these tables has per-user data and the Python
        #    backend runs as service_role (bypasses RLS), so explicit
        #    user_id filters are the only thing stopping cross-user leaks.
        history = []
        if uid:
            try:
                cur.execute(
                    """
                    (SELECT 'current' AS fy, symbol, month_label, realized_pl, realized_pl_pct
                     FROM legacy_trades WHERE symbol ILIKE %s AND user_id = %s)
                    UNION ALL
                    (SELECT 'FY24-25', symbol, month_label, realized_pl, realized_pl_pct
                     FROM legacy_trades_fy2425 WHERE symbol ILIKE %s AND user_id = %s)
                    UNION ALL
                    (SELECT 'FY23-24', symbol, month_label, realized_pl, realized_pl_pct
                     FROM legacy_trades_fy2324 WHERE symbol ILIKE %s AND user_id = %s)
                    UNION ALL
                    (SELECT 'FY22-23', symbol, month_label, realized_pl, realized_pl_pct
                     FROM legacy_trades_fy2223 WHERE symbol ILIKE %s AND user_id = %s)
                    UNION ALL
                    (SELECT 'FY21-22', symbol, month_label, realized_pl, realized_pl_pct
                     FROM legacy_trades_fy2122 WHERE symbol ILIKE %s AND user_id = %s)
                    ORDER BY 1 DESC
                    LIMIT 10
                    """,
                    (symbol, uid, symbol, uid, symbol, uid, symbol, uid, symbol, uid),
                )
                history = [dict(r) for r in cur.fetchall()]
            except Exception:
                history = []

        # 6. Latest scoring submission — scoped to the authenticated user.
        scoring = None
        if uid:
            try:
                cur.execute(
                    "SELECT final_score, rating, setup_type, extension_pct, sector, timestamp "
                    "FROM submissions "
                    "WHERE stock_name ILIKE %s AND user_id = %s "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (f"%{symbol}%", uid),
                )
                s = cur.fetchone()
                if s:
                    scoring = dict(s)
            except Exception:
                scoring = None

        # 7. Active price alerts for this security (per-user)
        alerts = []
        if uid:
            try:
                cur.execute(
                    "SELECT condition, threshold, last_price, active, triggered, "
                    "       triggered_at, notes, created_at "
                    "FROM price_alerts "
                    "WHERE user_id = %s AND security_id = %s AND active = true "
                    "ORDER BY created_at DESC",
                    (str(uid), security_id),
                )
                alerts = [dict(r) for r in cur.fetchall()]
            except Exception:
                alerts = []

        # 8. Sector indices this stock belongs to (for "is X in a leading sector")
        sector_indices = []
        try:
            cur.execute(
                "SELECT DISTINCT index_symbol, sector AS ic_sector "
                "FROM index_constituents "
                "WHERE security_id = %s",
                (security_id,),
            )
            sector_indices = [dict(r) for r in cur.fetchall()]
        except Exception:
            sector_indices = []

        return {
            "type":           "stock_snapshot",
            "symbol":         symbol,
            "security_id":    security_id,
            "company_name":   base.get("company_name"),
            "sector":         base.get("sector"),
            "industry":       base.get("industry"),
            "overview":       to_jsonable(overview),
            "market":         to_jsonable(market),
            "position":       to_jsonable(position),
            "trade_history":  to_jsonable(history),
            "latest_scoring": to_jsonable(scoring),
            "price_alerts":   to_jsonable(alerts),
            "sector_indices": to_jsonable(sector_indices),
        }
    except Exception as exc:
        return {"error": f"Stock snapshot error: {exc}"}
    finally:
        close_db(conn)


def _exec_get_positions_sector_membership(params: dict) -> dict:
    """Direct cross-reference: active positions ↔ sector indices, joined on
    security_id (the only reliable key between positions and
    index_constituents — stock_name / symbol strings silently drop matches
    because positions stores display names while index_constituents stores
    NSE tickers). Returns current leading sectors alongside each position's
    full sector-index membership plus a flag for which sit in leading ones.
    """
    try:
        limit = max(1, min(int(params.get("limit_sectors") or 5), 20))
    except (TypeError, ValueError):
        limit = 5

    uid = _get_user_id()
    if not uid:
        return {"error": "No authenticated user"}

    # Primary: current leading sectors from index_daily_summary
    leading_details = []
    leading_symbols: list[str] = []
    try:
        from services.index_summary import (
            get_leading_sectors as _svc_get_leaders,
            trigger_background_refresh_if_stale,
        )
        trigger_background_refresh_if_stale()
        leading_details = _svc_get_leaders(limit=limit)
        leading_symbols = [r.get("symbol") for r in leading_details if r.get("symbol")]
    except Exception as exc:
        print(f"[valvo-ai v5] sector_membership: leaders lookup failed: {exc}")
        leading_symbols = []

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.id, p.stock_name, p.security_id,
                   ARRAY_REMOVE(ARRAY_AGG(DISTINCT ic.index_symbol), NULL) AS all_indices,
                   ARRAY_REMOVE(ARRAY_AGG(DISTINCT ic.sector), NULL)       AS all_sectors
            FROM positions p
            LEFT JOIN index_constituents ic ON ic.security_id = p.security_id
            WHERE p.status = 'active' AND p.user_id = %s
            GROUP BY p.id, p.stock_name, p.security_id
            ORDER BY p.id DESC
            """,
            (uid,),
        )
        rows = []
        matches: list[dict] = []
        for r in cur.fetchall():
            name = r.get("stock_name")
            all_indices = list(r.get("all_indices") or [])
            all_sectors = list(r.get("all_sectors") or [])
            leading_memberships = [ix for ix in all_indices if ix in leading_symbols]
            for ix in leading_memberships:
                matches.append({"stock_name": name, "leading_index": ix})
            rows.append({
                "stock_name":                  name,
                "security_id":                 r.get("security_id"),
                "all_indices":                 all_indices,
                "all_sectors":                 all_sectors,
                "leading_sector_memberships":  leading_memberships,
            })

        return {
            "type":                  "positions_sector_membership",
            "leading_sectors":       leading_symbols,
            "leading_details":       to_jsonable(leading_details),
            "positions":             to_jsonable(rows),
            "matches_in_leading":    matches,
            "count_matching":        len(matches),
            "count_positions":       len(rows),
        }
    except Exception as exc:
        return {"error": f"Sector membership error: {exc}"}
    finally:
        close_db(conn)


def _exec_get_market_regime(params: dict) -> dict:
    """Read the latest global market regime entry. The DB table has no user_id
    column — it's a shared reference like leading_sectors. Previous per-user
    scoping silently errored for every call because the column never existed."""
    history = params.get("history") or 1
    try:
        history = max(1, min(int(history), 10))
    except (TypeError, ValueError):
        history = 1

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT regime, note, updated_at "
            "FROM market_regime_history "
            "ORDER BY updated_at DESC "
            "LIMIT %s",
            (history,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {"type": "market_regime", "regime": None, "note": "No regime data recorded yet."}
        latest = rows[0]
        return {
            "type": "market_regime",
            "regime": latest.get("regime"),
            "note": latest.get("note"),
            "updated_at": str(latest.get("updated_at")),
            "history": to_jsonable(rows) if history > 1 else None,
        }
    except Exception as exc:
        return {"error": f"Market regime error: {exc}"}
    finally:
        close_db(conn)


def _exec_get_leading_sectors(params: dict) -> dict:
    """Top sector indices ranked by a composite leadership score, derived
    from index_daily_summary (a pre-computed per-index snapshot of 20d/60d
    returns + MA50 alignment — refreshed lazily from candles_indices).

    The old manual leading_sectors table is used ONLY as a graceful fallback
    when the new summary is empty (first-ever run, or if the refresh is
    stuck). Once index_daily_summary has data, the manual table becomes
    effectively unused — the Settings UI entry is vestigial.
    """
    try:
        limit = max(1, min(int(params.get("limit") or 5), 20))
    except (TypeError, ValueError):
        limit = 5

    # Primary path: pre-computed sector leadership from index data.
    try:
        from services.index_summary import (
            get_leading_sectors as _svc_get_leaders,
            trigger_background_refresh_if_stale,
        )
        # Don't block the model on a refresh — just kick one off in the
        # background if the data is stale, and serve whatever's current.
        trigger_background_refresh_if_stale()
        leaders = _svc_get_leaders(limit=limit)
        if leaders:
            # Strip the NIFTY prefix for a cleaner sector name in the response
            # ("NIFTY METAL" → "Metal"). Keeps the raw symbol alongside so the
            # model can still JOIN against candles_indices if it needs detail.
            def _pretty(sym: str) -> str:
                s = (sym or "").strip().upper()
                if s.startswith("NIFTY "):
                    return s[6:].title()
                if s == "NIFTYIT":
                    return "IT"
                if s.startswith("NIFTY"):
                    return s[5:].title()
                return s.title()
            out = []
            for r in leaders:
                out.append({
                    "sector":           _pretty(r.get("symbol")),
                    "symbol":           r.get("symbol"),
                    "prev_close":       r.get("prev_close"),
                    "return_5d":        r.get("return_5d"),
                    "return_20d":       r.get("return_20d"),
                    "return_60d":       r.get("return_60d"),
                    "above_ma50":       r.get("above_ma50"),
                    "above_ma200":      r.get("above_ma200"),
                    "leadership_score": r.get("leadership_score"),
                    "updated_at":       str(r.get("updated_at")),
                })
            return {
                "type":      "leading_sectors",
                "source":    "index_daily_summary",
                "sectors":   [item["sector"] for item in out],
                "details":   to_jsonable(out),
                "ranked_by": "leadership_score = 50%·return_20d + 25%·return_60d + 25%·MA50-bonus",
            }
    except Exception as exc:
        print(f"[valvo-ai v5] leading_sectors primary path failed: {exc}")

    # Fallback: legacy manual leading_sectors table (Settings UI entry).
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT sectors, regime, note, updated_at "
            "FROM leading_sectors "
            "ORDER BY updated_at DESC "
            "LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return {
                "type": "leading_sectors",
                "source": "fallback",
                "sectors": [],
                "note": "index_daily_summary empty AND legacy leading_sectors empty. Trigger POST /api/sectors/refresh-summary to populate.",
            }
        row = dict(row)
        return {
            "type":       "leading_sectors",
            "source":     "legacy_manual_table",
            "sectors":    list(row.get("sectors") or []),
            "regime":     row.get("regime"),
            "note":       row.get("note"),
            "updated_at": str(row.get("updated_at")),
        }
    except Exception as exc:
        return {"error": f"Leading sectors error: {exc}"}
    finally:
        close_db(conn)


def _current_fy_label() -> str:
    """Indian FY label (April-March) for today, e.g. '2026-27'."""
    today = date.today()
    year = today.year if today.month >= 4 else today.year - 1
    end = str((year + 1) % 100).zfill(2)
    return f"{year}-{end}"


def _exec_get_analytics(params: dict) -> dict:
    endpoint = params.get("endpoint", "full")
    fy = params.get("fy") or _current_fy_label()
    uid = _get_user_id()

    try:
        if endpoint == "full":
            return _analytics_full(fy, uid)
        elif endpoint == "outliers":
            return _analytics_outliers(fy, uid)
        elif endpoint == "advanced-v2":
            return _analytics_advanced(fy, uid)
        else:
            return {"error": f"Unknown analytics endpoint: {endpoint}"}
    except Exception as exc:
        traceback.print_exc()
        return {"error": f"Analytics error: {exc}"}


def _resolve_user_fy(cur, uid, fy):
    """Resolve FY to table + optional user filter. Returns (table, where_clause, params, error)."""
    if not uid:
        return None, "", [], "No authenticated user"
    try:
        from services.user_analytics_service import resolve_fy, get_user_fy_list
        resolved = resolve_fy(cur, uid, fy)
        if not resolved.get("allowed"):
            # Be explicit so the AI doesn't editorialize ("data is in a
            # different table"). Tell it exactly what FYs ARE available
            # for this user so it can either suggest a switch or report
            # the absence honestly.
            available = []
            try:
                available = [f for f in get_user_fy_list(cur, uid) if f and f != "all"]
            except Exception:
                available = []
            avail_str = ", ".join(available) if available else "no FYs configured"
            return None, "", [], (
                f"You don't have any data for FY {fy}. Available FYs for "
                f"this account: {avail_str}."
            )
        tbl = resolved["table"]
        if resolved.get("user_filter"):
            return tbl, f" WHERE user_id = %s", [uid], None
        return tbl, "", [], None
    except Exception:
        # Fallback: if user_analytics_service not available, use legacy map with no filter
        tbl = _fy_trade_table(fy)
        return tbl, "", [], None


def _analytics_full(fy: str, uid=None) -> dict:
    """Return full analytics stats for a given FY by querying the trade table."""
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        tbl, where, wparams, err = _resolve_user_fy(cur, uid, fy)
        if err:
            return {"error": err}

        cur.execute(f"""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN realized_pl <= 0 THEN 1 ELSE 0 END) as losers,
                ROUND(SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END)::numeric * 100.0 /
                      NULLIF(COUNT(*), 0), 1) as win_rate,
                ROUND(SUM(realized_pl)::numeric) as net_pl,
                ROUND(SUM(CASE WHEN realized_pl > 0 THEN realized_pl ELSE 0 END)::numeric) as gross_profit,
                ROUND(ABS(SUM(CASE WHEN realized_pl <= 0 THEN realized_pl ELSE 0 END))::numeric) as gross_loss,
                ROUND(AVG(realized_pl_pct)::numeric, 2) as avg_return_pct,
                ROUND(MAX(realized_pl_pct)::numeric, 2) as best_return_pct,
                ROUND(MIN(realized_pl_pct)::numeric, 2) as worst_return_pct
            FROM {tbl}{where}
        """, wparams)
        summary = dict(cur.fetchone())

        wc = where.replace("WHERE", "AND") if where else ""
        cur.execute(f"""
            SELECT symbol, realized_pl, realized_pl_pct
            FROM {tbl}
            WHERE realized_pl > 0 {wc}
            ORDER BY realized_pl DESC
            LIMIT 5
        """, wparams)
        top_winners = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT symbol, realized_pl, realized_pl_pct
            FROM {tbl}
            WHERE realized_pl <= 0 {wc}
            ORDER BY realized_pl ASC
            LIMIT 5
        """, wparams)
        top_losers = [dict(r) for r in cur.fetchall()]

        gp = float(summary.get("gross_profit") or 0)
        gl = float(summary.get("gross_loss") or 1)
        summary["profit_factor"] = round(gp / gl, 2) if gl > 0 else None

        return {
            "type": "analytics_full",
            "fy": fy,
            "summary": to_jsonable(summary),
            "top_winners": to_jsonable(top_winners),
            "top_losers": to_jsonable(top_losers),
        }
    except Exception as exc:
        traceback.print_exc()
        return {"error": f"Analytics full error: {exc}"}
    finally:
        close_db(conn)


def _analytics_outliers(fy: str, uid=None) -> dict:
    """Return outlier/distribution data for a given FY."""
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        tbl, where, wparams, err = _resolve_user_fy(cur, uid, fy)
        if err:
            return {"error": err}

        # Distribution buckets by realized_pl_pct
        cur.execute(f"""
            WITH trades AS (
                SELECT realized_pl_pct as pct, realized_pl as pl FROM {tbl}{where}
            )
            SELECT
                CASE
                    WHEN pct < -10 THEN '< -10%'
                    WHEN pct < -5  THEN '-10% to -5%'
                    WHEN pct < 0   THEN '-5% to 0%'
                    WHEN pct < 5   THEN '0% to 5%'
                    WHEN pct < 10  THEN '5% to 10%'
                    WHEN pct < 20  THEN '10% to 20%'
                    WHEN pct < 50  THEN '20% to 50%'
                    ELSE '50%+'
                END as bucket,
                COUNT(*) as count,
                ROUND(SUM(pl)::numeric) as total_pl
            FROM trades
            GROUP BY 1
            ORDER BY MIN(pct)
        """, wparams)
        distribution = [dict(r) for r in cur.fetchall()]

        wc = where.replace("WHERE", "AND") if where else ""
        cur.execute(f"""
            SELECT symbol, realized_pl, realized_pl_pct,
                   ROUND((realized_pl_pct / 3.0)::numeric, 2) as r_multiple
            FROM {tbl}
            WHERE ABS(realized_pl_pct / 3.0) > 3 {wc}
            ORDER BY realized_pl_pct DESC
        """, wparams)
        outliers = [dict(r) for r in cur.fetchall()]

        return {
            "type": "analytics_outliers",
            "fy": fy,
            "distribution": to_jsonable(distribution),
            "outliers": to_jsonable(outliers),
        }
    except Exception as exc:
        traceback.print_exc()
        return {"error": f"Outlier analysis error: {exc}"}
    finally:
        close_db(conn)


def _analytics_advanced(fy: str, uid=None) -> dict:
    """Equity curve, drawdown, streaks, holding period."""
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        tbl, where, wparams, err = _resolve_user_fy(cur, uid, fy)
        if err:
            return {"error": err}
        # Get user-aware base capital
        base = _fy_base_capital(fy)
        if uid:
            try:
                from services.user_analytics_service import get_user_base_capital
                user_base = get_user_base_capital(cur, uid, fy)
                if user_base is not None:
                    base = int(user_base)
            except Exception:
                pass

        # Equity curve (cumulative P&L by trade order)
        cur.execute(f"""
            SELECT id, symbol, realized_pl,
                   SUM(realized_pl) OVER (ORDER BY id) as cumulative_pl
            FROM {tbl}{where}
            ORDER BY id
        """, wparams)
        curve_rows = cur.fetchall()
        equity_curve = []
        peak = 0.0
        max_dd = 0.0
        for r in curve_rows:
            cum = float(r["cumulative_pl"] or 0)
            equity = base + cum
            if cum > peak:
                peak = cum
            dd = ((peak - cum) / (base + peak) * 100) if (base + peak) > 0 else 0
            if dd > max_dd:
                max_dd = dd
            equity_curve.append({
                "id": r["id"],
                "symbol": r["symbol"],
                "equity": round(equity),
                "drawdown_pct": round(dd, 2),
            })

        # Win/loss streaks
        cur.execute(f"""
            SELECT id, symbol, (realized_pl > 0) as is_win
            FROM {tbl}{where}
            ORDER BY id
        """, wparams)
        streak_rows = cur.fetchall()
        max_win_streak = 0
        max_loss_streak = 0
        current_streak = 0
        current_type = None
        for r in streak_rows:
            w = r["is_win"]
            if w == current_type:
                current_streak += 1
            else:
                current_streak = 1
                current_type = w
            if w and current_streak > max_win_streak:
                max_win_streak = current_streak
            if not w and current_streak > max_loss_streak:
                max_loss_streak = current_streak

        return {
            "type": "analytics_advanced",
            "fy": fy,
            "base_capital": base,
            "equity_curve_sample": to_jsonable(equity_curve[-20:]),  # last 20 for brevity
            "total_trades_in_curve": len(equity_curve),
            "max_drawdown_pct": round(max_dd, 2),
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
        }
    except Exception as exc:
        traceback.print_exc()
        return {"error": f"Advanced analytics error: {exc}"}
    finally:
        close_db(conn)


def _exec_get_equity_curve(params: dict) -> dict:
    curve_type = params.get("type", "long-term")
    uid = _get_user_id()
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        # Build user-aware FY chain from user_fy_config
        fy_chain = []
        if uid:
            try:
                from services.user_analytics_service import get_user_fy_list, get_user_base_capital, LEGACY_FY_TABLES, JOURNAL_FY_TABLE
                user_fys = get_user_fy_list(cur, uid)
                for fy_label in user_fys:
                    if fy_label == "all":
                        continue
                    base = get_user_base_capital(cur, uid, fy_label)
                    if base is None:
                        continue
                    # All trade tables — legacy and journal — have user_id.
                    # Must filter regardless of source (was False for legacy,
                    # which caused cross-user leaks in the equity curve).
                    if fy_label in LEGACY_FY_TABLES:
                        tbl = LEGACY_FY_TABLES[fy_label]
                        fy_chain.append((fy_label, tbl, int(base), True))
                    else:
                        fy_chain.append((fy_label, JOURNAL_FY_TABLE, int(base), True))
            except Exception:
                pass
        if not fy_chain:
            return {"error": "No FY data configured for this user"}

        if curve_type == "long-term":
            yearly = []
            for item in fy_chain:
                fy_label, tbl, base = item[0], item[1], item[2]
                needs_user_filter = item[3] if len(item) > 3 else False
                uf = f" WHERE user_id = %s" if needs_user_filter else ""
                uf_params = [uid] if needs_user_filter else []
                cur.execute(f"""
                    SELECT
                        COUNT(*) as trades,
                        ROUND(SUM(realized_pl)::numeric) as net_pl,
                        ROUND(SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END)::numeric * 100.0 /
                              NULLIF(COUNT(*), 0), 1) as win_rate
                    FROM {tbl}{uf}
                """, uf_params)
                row = dict(cur.fetchone())
                net = float(row.get("net_pl") or 0)
                yearly.append({
                    "fy": fy_label,
                    "start_capital": base,
                    "net_pl": net,
                    "end_capital": round(base + net),
                    "return_pct": round(net / base * 100, 2) if base else 0,
                    "trades": row.get("trades"),
                    "win_rate": row.get("win_rate"),
                })
            return {"type": "equity_curve_long_term", "years": to_jsonable(yearly)}

        elif curve_type == "drawdown-deep":
            drawdowns = []
            for item in fy_chain:
                fy_label, tbl, base = item[0], item[1], item[2]
                needs_user_filter = item[3] if len(item) > 3 else False
                uf = f" WHERE user_id = %s" if needs_user_filter else ""
                uf_params = [uid] if needs_user_filter else []
                cur.execute(f"""
                    SELECT id, symbol, realized_pl,
                           SUM(realized_pl) OVER (ORDER BY id) as cum_pl
                    FROM {tbl}{uf}
                    ORDER BY id
                """, uf_params)
                rows = cur.fetchall()
                peak = 0.0
                max_dd = 0.0
                for r in rows:
                    cum = float(r["cum_pl"] or 0)
                    if cum > peak:
                        peak = cum
                    dd = ((peak - cum) / (base + peak) * 100) if (base + peak) > 0 else 0
                    if dd > max_dd:
                        max_dd = dd
                drawdowns.append({"fy": fy_label, "max_drawdown_pct": round(max_dd, 2)})
            return {"type": "drawdown_deep", "drawdowns": to_jsonable(drawdowns)}

        else:
            return {"error": f"Unknown equity curve type: {curve_type}"}
    except Exception as exc:
        traceback.print_exc()
        return {"error": f"Equity curve error: {exc}"}
    finally:
        close_db(conn)


def _exec_get_live_market(params: dict) -> dict:
    symbols = params.get("symbols") or []
    include_ma = params.get("include_ma", True)
    if not symbols:
        return {"error": "No symbols provided"}

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        results = []
        for sym in symbols[:10]:  # cap at 10
            sym_clean = sym.strip().upper()
            # Resolve the query to a canonical security_id. Tries in order:
            #   1. Exact symbol match     (e.g. "RELIANCE" → RELIANCE)
            #   2. Company-name match     (e.g. "NALCO" → NATIONALUM, where
            #                              stock_universe.company_name="NALCO")
            #   3. Substring symbol match (e.g. "TATA" → TATASTEEL, one of many)
            # Without step 2, colloquial tickers like "NALCO" failed the
            # lookup entirely (NSE ticker is NATIONALUM; symbol column has
            # NATIONALUM; company_name has "NALCO").
            cur.execute(
                "SELECT security_id, symbol, company_name FROM stock_universe "
                "WHERE symbol ILIKE %s LIMIT 1",
                (sym_clean,),
            )
            stock = cur.fetchone()
            if not stock:
                cur.execute(
                    "SELECT security_id, symbol, company_name FROM stock_universe "
                    "WHERE company_name ILIKE %s "
                    "ORDER BY CASE WHEN company_name ILIKE %s THEN 0 ELSE 1 END, is_active DESC "
                    "LIMIT 1",
                    (sym_clean, sym_clean),
                )
                stock = cur.fetchone()
            if not stock:
                cur.execute(
                    "SELECT security_id, symbol, company_name FROM stock_universe "
                    "WHERE symbol ILIKE %s OR company_name ILIKE %s "
                    "ORDER BY is_active DESC LIMIT 1",
                    (f"%{sym_clean}%", f"%{sym_clean}%"),
                )
                stock = cur.fetchone()
            if not stock:
                results.append({"symbol": sym_clean, "error": "Stock not found"})
                continue

            # Get last 20 trading days
            cur.execute(
                """
                SELECT date, close
                FROM candles_daily
                WHERE security_id = %s
                ORDER BY date DESC
                LIMIT 20
                """,
                (stock["security_id"],),
            )
            candles = cur.fetchall()
            if not candles:
                results.append({
                    "symbol": stock["symbol"],
                    "company": stock["company_name"],
                    "error": "No price data",
                })
                continue

            latest = candles[0]
            entry = {
                "symbol": stock["symbol"],
                "company": stock["company_name"],
                "security_id": stock["security_id"],
                "last_close": float(latest["close"]),
                "last_date": latest["date"].isoformat() if hasattr(latest["date"], "isoformat") else str(latest["date"]),
            }

            if include_ma and len(candles) >= 5:
                closes = [float(c["close"]) for c in candles]
                entry["ma5"] = round(sum(closes[:5]) / 5, 2)
                if len(candles) >= 10:
                    entry["ma10"] = round(sum(closes[:10]) / 10, 2)
                if len(candles) >= 20:
                    entry["ma20"] = round(sum(closes[:20]) / 20, 2)

            results.append(entry)

        return {"type": "live_market", "data": to_jsonable(results)}
    except Exception as exc:
        traceback.print_exc()
        return {"error": f"Live market error: {exc}"}
    finally:
        close_db(conn)


def _exec_get_positions(params: dict) -> dict:
    status = params.get("status", "active")
    uid = _get_user_id()
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        if uid:
            # Same forthcoming_results LATERAL join the REST API uses, so the
            # AI sees identical event-risk timing for every active position.
            base_select = """
                SELECT p.*,
                       nr.meeting_date AS next_result_date,
                       nr.purpose AS next_result_purpose,
                       CASE WHEN nr.meeting_date IS NOT NULL
                            THEN (nr.meeting_date - CURRENT_DATE)::int
                            ELSE NULL END AS next_result_days_left
                FROM positions p
                LEFT JOIN LATERAL (
                    SELECT fr.meeting_date, fr.purpose
                    FROM forthcoming_results fr
                    WHERE fr.security_id = p.security_id
                      AND fr.meeting_date >= CURRENT_DATE
                      AND (fr.purpose ILIKE %s OR fr.raw_purpose ILIKE %s
                           OR fr.raw_purpose ILIKE %s OR fr.raw_purpose ILIKE %s)
                    ORDER BY fr.meeting_date ASC,
                             (fr.purpose ILIKE %s) DESC,
                             (fr.purpose ILIKE %s) DESC
                    LIMIT 1
                ) nr ON TRUE
            """
            patterns = ("%result%", "%financial result%", "%audited%", "%quarterly result%",
                        "%financial result%", "%result%")
            if status == "all":
                cur.execute(base_select + " WHERE p.user_id = %s ORDER BY p.id DESC LIMIT 100",
                            patterns + (uid,))
            else:
                cur.execute(base_select + " WHERE p.status = %s AND p.user_id = %s ORDER BY p.id DESC LIMIT 100",
                            patterns + (status, uid))
        else:
            # No user context — return empty (never leak all users' data)
            return {"type": "positions", "status_filter": status, "count": 0, "positions": [], "warning": "No authenticated user — cannot load positions"}
        rows = [dict(r) for r in cur.fetchall()]
        # Pre-compute totals server-side. LLMs are unreliable at adding
        # numbers — even with per-row P&L visible they sum incorrectly
        # (Anlon=0, Balrampur=-27.60, Ather=-160.60, Gallantt=-215,
        # Garden Reach=+1058.95, Ramco=-86.40, NBCC=+8 → +577.35,
        # but Gemini repeatedly produced -1273.74 for the same input).
        # Returning the totals in the tool response means the AI can copy
        # them rather than risking arithmetic.
        totals = {"value": 0.0, "pl": 0.0, "cost": 0.0}
        for r in rows:
            try:
                qty = int(r.get("quantity") or 0)
                entry = float(r.get("entry_price") or 0)
                cmp_v = float(r.get("current_price") or entry)
                totals["value"] += cmp_v * qty
                totals["pl"] += (cmp_v - entry) * qty
                totals["cost"] += entry * qty
            except (TypeError, ValueError):
                pass
        totals["pl_pct"] = round(
            (totals["pl"] / totals["cost"] * 100) if totals["cost"] else 0, 2
        )
        for k in ("value", "pl", "cost"):
            totals[k] = round(totals[k], 2)

        return {
            "type": "positions",
            "status_filter": status,
            "count": len(rows),
            "totals": totals,
            "positions": to_jsonable(rows),
        }
    except Exception as exc:
        return {"error": f"Positions error: {exc}"}
    finally:
        close_db(conn)


def _exec_search_stock(params: dict) -> dict:
    query = (params.get("query") or "").strip()
    if not query:
        return {"error": "No search query provided"}

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT security_id, symbol, company_name, exchange, is_active
            FROM stock_universe
            WHERE symbol ILIKE %s OR company_name ILIKE %s
            ORDER BY
                CASE WHEN symbol ILIKE %s THEN 0 ELSE 1 END,
                company_name
            LIMIT 10
            """,
            (f"%{query}%", f"%{query}%", query),
        )
        rows = [dict(r) for r in cur.fetchall()]
        return {"type": "stock_search", "query": query, "results": to_jsonable(rows)}
    except Exception as exc:
        return {"error": f"Stock search error: {exc}"}
    finally:
        close_db(conn)


def _exec_confirm_action(params: dict) -> dict:
    pid = params.get("pending_action_id", "")
    if not pid:
        return {"error": "pending_action_id is required"}
    return to_jsonable(confirm_pending_action(pid))


def _exec_cancel_action(params: dict) -> dict:
    pid = params.get("pending_action_id", "")
    if not pid:
        return {"error": "pending_action_id is required"}
    return to_jsonable(cancel_pending_action(pid))


def _exec_v2_action(action_name: str, params: dict) -> dict:
    """Delegate to the v2 action runner (with confirmation flow + audit)."""
    return to_jsonable(run_action(action_name, params, request_text="valvo-ai-v5"))


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

_FY_TABLE_MAP = {
    "2020-21": "legacy_trades_fy2021",
    "2021-22": "legacy_trades_fy2122",
    "2022-23": "legacy_trades_fy2223",
    "2023-24": "legacy_trades_fy2324",
    "2024-25": "legacy_trades_fy2425",
    "2025-26": "legacy_trades",
    "2026-27": "journal_trades_computed",
}

_FY_CAPITAL_MAP = {
    "2020-21": 6_000_000,
    "2021-22": 9_075_419,
    "2022-23": 16_187_147,
    "2023-24": 18_028_240,
    "2024-25": 28_000_000,
    "2025-26": 50_000_000,
    "2026-27": 50_000_000,
}


def _fy_trade_table(fy: str) -> str:
    return _FY_TABLE_MAP.get(fy, "legacy_trades")


def _fy_base_capital(fy: str) -> int:
    return _FY_CAPITAL_MAP.get(fy, 50_000_000)


# ═══════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

_READ_TOOLS = [
    SQL_QUERY_TOOL,
    GET_ANALYTICS_TOOL,
    GET_EQUITY_CURVE_TOOL,
    GET_LIVE_MARKET_TOOL,
    GET_POSITIONS_TOOL,
    GET_POSITIONS_SECTOR_MEMBERSHIP_TOOL,
    GET_STOCK_SNAPSHOT_TOOL,
    GET_COMPARE_STOCKS_TOOL,
    GET_INDEX_SNAPSHOT_TOOL,
    GET_MARKET_REGIME_TOOL,
    GET_LEADING_SECTORS_TOOL,
    GET_FUNDAMENTALS_TOOL,
    SEARCH_STOCK_TOOL,
]

_META_TOOLS = [
    CONFIRM_ACTION_TOOL,
    CANCEL_ACTION_TOOL,
]

_EXECUTOR_MAP: dict[str, Any] = {
    "sql_query": _exec_sql_query,
    "get_analytics": _exec_get_analytics,
    "get_equity_curve": _exec_get_equity_curve,
    "get_live_market": _exec_get_live_market,
    "get_positions": _exec_get_positions,
    "get_positions_sector_membership": _exec_get_positions_sector_membership,
    "get_stock_snapshot": _exec_get_stock_snapshot,
    "get_compare_stocks": _exec_get_compare_stocks,
    "get_index_snapshot": _exec_get_index_snapshot,
    "get_market_regime": _exec_get_market_regime,
    "get_leading_sectors": _exec_get_leading_sectors,
    "get_fundamentals": _exec_get_fundamentals,
    "search_stock": _exec_search_stock,
    "confirm_action": _exec_confirm_action,
    "cancel_action": _exec_cancel_action,
}


def get_all_tool_definitions() -> list[dict]:
    """Return the full list of tool schemas for the Anthropic API call."""
    tools = list(_READ_TOOLS) + list(_META_TOOLS)
    # Add v2 action tools (create_position, record_sell, etc.)
    tools.extend(_v2_action_tool_defs())
    return tools


def execute_tool(name: str, params: dict) -> dict:
    """Execute a tool by name. Returns a dict result."""
    # Direct executor match
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
