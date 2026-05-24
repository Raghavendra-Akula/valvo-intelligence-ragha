"""
Valvo AI v3 -- Tool definitions and executors.

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
        "Execute a read-only SQL query against the PostgreSQL database. "
        "Use for any data question. Returns up to 100 rows. "
        "Only SELECT and WITH statements are allowed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A read-only PostgreSQL query (SELECT/WITH only).",
            },
        },
        "required": ["query"],
    },
}

GET_ANALYTICS_TOOL = {
    "name": "get_analytics",
    "description": (
        "Get pre-computed analytics. "
        "'full' = complete stats (win rate, P&L, profit factor, top winners/losers, monthly trend). "
        "'outliers' = outlier analysis with distribution. "
        "'advanced-v2' = equity curve, drawdown, streaks, holding period."
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
                "description": "Financial year, e.g. '2025-26'. Defaults to '2025-26'.",
            },
        },
        "required": ["endpoint"],
    },
}

GET_EQUITY_CURVE_TOOL = {
    "name": "get_equity_curve",
    "description": (
        "Get equity curve or drawdown analysis across all FYs. "
        "'long-term' = portfolio value over time. "
        "'drawdown-deep' = drawdown periods with recovery analysis."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["long-term", "drawdown-deep"],
                "description": "Type of equity/drawdown data to retrieve.",
            },
        },
        "required": ["type"],
    },
}

GET_LIVE_MARKET_TOOL = {
    "name": "get_live_market",
    "description": (
        "Get latest price, 5/10/20-day moving averages for given stock symbols. "
        "Queries candles_daily table."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of NSE stock symbols (e.g. ['RELIANCE', 'TCS']).",
            },
            "include_ma": {
                "type": "boolean",
                "description": "Whether to include 5/10/20-day moving averages. Defaults to true.",
            },
        },
        "required": ["symbols"],
    },
}

GET_POSITIONS_TOOL = {
    "name": "get_positions",
    "description": (
        "Get portfolio positions with current P&L, R-multiples, defensive status."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "closed", "all"],
                "description": "Filter by position status. Defaults to 'active'.",
            },
        },
    },
}

SEARCH_STOCK_TOOL = {
    "name": "search_stock",
    "description": (
        "Search for stock by name or symbol. Returns security_id, symbol, company name, exchange."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Stock name or symbol to search for.",
            },
        },
        "required": ["query"],
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

        cur.execute(query)
        rows = cur.fetchmany(100)
        result = [dict(r) for r in rows]
        # Compact JSON output, truncate if too large
        text = json.dumps(to_jsonable(result), default=str, ensure_ascii=False)
        if len(text) > 8000:
            text = text[:8000] + "...(truncated)"
        return {"rows": json.loads(text) if len(text) <= 8000 else result[:50], "count": len(result), "row_count": len(result)}
    except Exception as exc:
        return {"error": f"SQL error: {exc}"}
    finally:
        try:
            close_db(conn)
        except Exception:
            pass


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
    """Resolve FY to table + optional user filter. Returns (table, where_clause, params)."""
    if not uid:
        return None, "", [], "No authenticated user"
    try:
        from services.user_analytics_service import resolve_fy
        resolved = resolve_fy(cur, uid, fy)
        if not resolved.get("allowed"):
            return None, "", [], f"FY {fy} not available for this user"
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
                    if fy_label in LEGACY_FY_TABLES:
                        tbl = LEGACY_FY_TABLES[fy_label]
                        fy_chain.append((fy_label, tbl, int(base), False))
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
            # Find security_id
            cur.execute(
                "SELECT security_id, symbol, company_name FROM stock_universe WHERE symbol ILIKE %s LIMIT 1",
                (sym_clean,),
            )
            stock = cur.fetchone()
            if not stock:
                # Try partial match
                cur.execute(
                    "SELECT security_id, symbol, company_name FROM stock_universe WHERE symbol ILIKE %s LIMIT 1",
                    (f"%{sym_clean}%",),
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
            if status == "all":
                cur.execute("SELECT * FROM positions WHERE user_id = %s ORDER BY id DESC LIMIT 100", (uid,))
            else:
                cur.execute("SELECT * FROM positions WHERE status = %s AND user_id = %s ORDER BY id DESC LIMIT 100", (status, uid))
        else:
            # No user context — return empty (never leak all users' data)
            return {"type": "positions", "status_filter": status, "count": 0, "positions": [], "warning": "No authenticated user — cannot load positions"}
        rows = [dict(r) for r in cur.fetchall()]
        return {"type": "positions", "status_filter": status, "count": len(rows), "positions": to_jsonable(rows)}
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
    return to_jsonable(run_action(action_name, params, request_text="valvo-ai-v3"))


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
