from __future__ import annotations

import json
import re

from database.database import get_db

from .utils import to_jsonable


SQL_FALLBACK_TOOL = {
    "name": "sql_read_fallback",
    "description": (
        "Last-resort read-only SQL access for edge analysis. Use only when no typed catalog reader fits. "
        "Only public analytics tables are allowed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql_query": {
                "type": "string",
                "description": "Single SELECT or WITH query against allowed public tables.",
            }
        },
        "required": ["sql_query"],
    },
}


ALLOWED_TABLES = {
    "analytics_config",
    "candles_daily",
    "candles_indices",
    "explore_cache",
    "index_constituents",
    "journal_fund_months",
    "journal_settings",
    "journal_trades",
    "journal_trades_computed",
    "leading_sectors",
    "legacy_monthly_fy2021",
    "legacy_monthly_fy2122",
    "legacy_monthly_fy2223",
    "legacy_monthly_fy2324",
    "legacy_monthly_fy2425",
    "legacy_monthly_summary",
    "legacy_trades",
    "legacy_trades_fy2021",
    "legacy_trades_fy2122",
    "legacy_trades_fy2223",
    "legacy_trades_fy2324",
    "legacy_trades_fy2324_backup",
    "legacy_trades_fy2425",
    "market_regime_history",
    "nexus_analytics",
    "nexus_monthly",
    "nexus_trades",
    "position_daily_updates",
    "positions",
    "saved_scanners",
    "scanner_run_history",
    "screener_stocks",
    "stock_universe",
    "submissions",
    "user_settings",
    "watchlist_items",
    "watchlists",
}

FORBIDDEN_PATTERN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|commit|rollback|create|replace|copy)\b",
    re.IGNORECASE,
)
TABLE_PATTERN = re.compile(r"\b(?:from|join)\s+([a-zA-Z0-9_.]+)", re.IGNORECASE)


def execute_sql_fallback(sql_query: str):
    query = (sql_query or "").strip()
    if not query:
        return {"ok": False, "error": "sql_query is required"}

    if ";" in query.rstrip(";"):
        return {"ok": False, "error": "Only a single SQL statement is allowed"}

    if FORBIDDEN_PATTERN.search(query):
        return {"ok": False, "error": "Only read-only SQL is allowed"}

    normalized = query.lower()
    if not (normalized.startswith("select") or normalized.startswith("with")):
        return {"ok": False, "error": "Fallback only allows SELECT or WITH queries"}

    referenced = set()
    for match in TABLE_PATTERN.finditer(query):
        raw_name = match.group(1).split(".")[-1].strip('"')
        if raw_name:
            referenced.add(raw_name)

    blocked = sorted(name for name in referenced if name not in ALLOWED_TABLES)
    if blocked:
        return {
            "ok": False,
            "error": f"SQL fallback blocked these tables: {', '.join(blocked)}",
        }

    conn = get_db()
    if not conn:
        return {"ok": False, "error": "Database unavailable"}

    try:
        cur = conn.cursor()
        cur.execute("BEGIN READ ONLY")
        cur.execute("SET LOCAL statement_timeout = 4000")
        cur.execute(query)
        rows = cur.fetchall()
        data = [to_jsonable(row) for row in rows]
        truncated = False
        if len(data) > 75:
            data = data[:75]
            truncated = True
        return {
            "ok": True,
            "row_count": len(rows),
            "truncated": truncated,
            "data": data,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def sql_result_text(result: dict) -> str:
    if not result.get("ok"):
        return f"SQL fallback error: {result.get('error', 'unknown error')}"
    preview = result.get("data", [])
    if not preview:
        return "SQL fallback returned no rows."
    payload = json.dumps(preview[:3], ensure_ascii=True)
    suffix = " Result was truncated." if result.get("truncated") else ""
    return f"SQL fallback returned {result.get('row_count', len(preview))} rows. Preview: {payload}{suffix}"
