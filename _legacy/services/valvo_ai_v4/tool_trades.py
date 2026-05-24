"""
Valvo AI v4 -- Semantic trade query tools.

Eliminates AI-generated SQL for 80%+ of trade questions.
The AI describes WHAT it wants; this module builds the SQL internally.
"""
from __future__ import annotations

import json
from datetime import date

from database.database import get_db, close_db
from services.valvo_ai_v2.utils import to_jsonable


# ═══════════════════════════════════════════════════════════════════════════
#  FY Mapping
# ═══════════════════════════════════════════════════════════════════════════

FY_TABLE_MAP = {
    "2020-21": "legacy_trades_fy2021",
    "2021-22": "legacy_trades_fy2122",
    "2022-23": "legacy_trades_fy2223",
    "2023-24": "legacy_trades_fy2324",
    "2024-25": "legacy_trades_fy2425",
    "2025-26": "legacy_trades",
    "2026-27": "journal_trades_computed",
}

MONTHLY_TABLE_MAP = {
    "2020-21": "legacy_monthly_fy2021",
    "2021-22": "legacy_monthly_fy2122",
    "2022-23": "legacy_monthly_fy2223",
    "2023-24": "legacy_monthly_fy2324",
    "2024-25": "legacy_monthly_fy2425",
    "2025-26": "legacy_monthly_summary",
}

ALL_FYS = list(FY_TABLE_MAP.keys())


def _current_fy() -> str:
    today = date.today()
    year = today.year if today.month >= 4 else today.year - 1
    end = str((year + 1) % 100).zfill(2)
    return f"{year}-{end}"


def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


def _get_user_stoploss_pct() -> float:
    """Get user's stoploss % for R-multiple calculation."""
    uid = _get_user_id()
    if not uid:
        return 4.0
    conn = get_db()
    if not conn:
        return 4.0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT stoploss_pct FROM user_profiles WHERE user_id = %s", (uid,),
        )
        row = cur.fetchone()
        return float(row["stoploss_pct"]) if row and row.get("stoploss_pct") else 4.0
    except Exception:
        return 4.0
    finally:
        close_db(conn)


def _resolve_fy_tables(fys: list[str], uid: str | None) -> list[tuple[str, str, bool]]:
    """Resolve FY labels to (fy_label, table_name, needs_user_filter) tuples."""
    target_fys = ALL_FYS if (not fys or fys == ["all"]) else [f for f in fys if f in FY_TABLE_MAP]

    result = []
    # Try dynamic resolution via user_analytics_service
    try:
        from services.user_analytics_service import resolve_fy
        if uid:
            conn = get_db()
            if conn:
                try:
                    cur = conn.cursor()
                    for fy_label in target_fys:
                        try:
                            resolved = resolve_fy(cur, uid, fy_label)
                            if resolved.get("allowed"):
                                result.append((
                                    fy_label,
                                    resolved["table"],
                                    bool(resolved.get("user_filter")),
                                ))
                        except Exception:
                            tbl = FY_TABLE_MAP.get(fy_label)
                            if tbl:
                                result.append((fy_label, tbl, tbl == "journal_trades_computed"))
                    return result
                finally:
                    close_db(conn)
    except ImportError:
        pass

    # Static fallback
    for fy_label in target_fys:
        tbl = FY_TABLE_MAP.get(fy_label)
        if tbl:
            result.append((fy_label, tbl, tbl == "journal_trades_computed"))
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  query_trades
# ═══════════════════════════════════════════════════════════════════════════

def exec_query_trades(params: dict) -> dict:
    """
    Semantic trade query.  Handles FY mapping, UNION ALL, filtering,
    aggregation — everything the AI used to do with raw SQL.
    """
    uid = _get_user_id()
    sl_pct = _get_user_stoploss_pct()

    # ---- Parse params ----
    fys = params.get("fys") or [_current_fy()]
    symbol = (params.get("symbol") or "").strip().upper() or None
    sector = (params.get("sector") or "").strip() or None
    winners_only = params.get("winners_only", False)
    losers_only = params.get("losers_only", False)
    min_return_pct = params.get("min_return_pct")
    max_return_pct = params.get("max_return_pct")
    min_pl = params.get("min_pl")
    month = (params.get("month") or "").strip() or None
    sort_by = params.get("sort_by", "pl")
    sort_order = params.get("sort_order", "desc")
    limit = min(params.get("limit") or 50, 200)
    aggregate = params.get("aggregate", "none")
    metrics = params.get("metrics") or ["count", "sum_pl", "win_rate"]

    fy_tables = _resolve_fy_tables(fys, uid)
    if not fy_tables:
        return {"error": "No valid financial years found for this user."}

    # ---- Build UNION ALL subquery ----
    union_parts = []
    union_params: list = []
    sector_warning = None

    for fy_label, table, needs_user_filter in fy_tables:
        wheres: list[str] = []
        wparams: list = []

        if needs_user_filter and uid:
            wheres.append("user_id = %s")
            wparams.append(uid)

        if symbol:
            wheres.append("UPPER(symbol) = %s")
            wparams.append(symbol)

        if winners_only:
            wheres.append("is_winner = true")
        elif losers_only:
            wheres.append("is_winner = false")

        if min_return_pct is not None:
            wheres.append("realized_pl_pct >= %s")
            wparams.append(min_return_pct)

        if max_return_pct is not None:
            wheres.append("realized_pl_pct <= %s")
            wparams.append(max_return_pct)

        if min_pl is not None:
            wheres.append("realized_pl >= %s")
            wparams.append(min_pl)

        if month:
            wheres.append("month_label = %s")
            wparams.append(month)

        # Sector only exists in journal_trades_computed
        if sector and table == "journal_trades_computed":
            wheres.append("LOWER(sector) = LOWER(%s)")
            wparams.append(sector)
        elif sector and table != "journal_trades_computed":
            sector_warning = "Sector filter only applies to FY 2026-27 (journal). Legacy FY tables do not have sector data."

        where_sql = " AND ".join(wheres) if wheres else "TRUE"

        # Common columns across all trade tables
        cols = (
            f"'{fy_label}' as fy, symbol, month_label, "
            "realized_pl, realized_pl_pct, is_winner, quantity, buy_value, sell_value"
        )
        if table == "journal_trades_computed":
            cols += ", sector, trade_date, entry_type, rating"
        else:
            cols += ", NULL::varchar as sector, NULL::date as trade_date, NULL::varchar as entry_type, NULL::smallint as rating"

        union_parts.append(f"SELECT {cols} FROM {table} WHERE {where_sql}")
        union_params.extend(wparams)

    union_sql = " UNION ALL ".join(union_parts)

    # ---- Execute ----
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor()

        # RLS context
        if uid:
            cur.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(uid)}),),
            )
            cur.execute("SET LOCAL ROLE authenticated")

        if aggregate == "none":
            return _individual_trades(
                cur, union_sql, union_params, sort_by, sort_order, limit,
                sl_pct, fy_tables, sector_warning,
            )
        else:
            return _aggregated_trades(
                cur, union_sql, union_params, aggregate, metrics,
                sort_by, sort_order, limit, sl_pct, fy_tables, sector_warning,
            )
    except Exception as exc:
        return {"error": f"Trade query error: {exc}"}
    finally:
        close_db(conn)


def _individual_trades(
    cur, union_sql, union_params, sort_by, sort_order, limit,
    sl_pct, fy_tables, sector_warning,
) -> dict:
    sort_col = {
        "pl": "realized_pl",
        "return_pct": "realized_pl_pct",
        "r_multiple": "realized_pl_pct",
        "symbol": "symbol",
    }.get(sort_by, "realized_pl")
    order = "DESC" if sort_order == "desc" else "ASC"

    sql = f"""
        SELECT * FROM ({union_sql}) t
        ORDER BY {sort_col} {order}
        LIMIT %s
    """
    cur.execute(sql, union_params + [limit])
    rows = [dict(r) for r in cur.fetchall()]

    # Post-process: add R-multiple
    for row in rows:
        pct = float(row.get("realized_pl_pct") or 0)
        row["r_multiple"] = round(pct / sl_pct, 2) if sl_pct else None

    result = {
        "type": "trades",
        "count": len(rows),
        "fys_queried": [ft[0] for ft in fy_tables],
        "stoploss_pct_used": sl_pct,
        "trades": to_jsonable(rows),
    }
    if sector_warning:
        result["note"] = sector_warning
    return result


def _aggregated_trades(
    cur, union_sql, union_params, aggregate, metrics,
    sort_by, sort_order, limit, sl_pct, fy_tables, sector_warning,
) -> dict:
    group_col = {
        "monthly": "fy, month_label",
        "yearly": "fy",
        "sector": "sector",
        "symbol": "symbol",
    }.get(aggregate, "fy")

    metrics_sql = _build_metrics_sql(metrics, sl_pct)

    # Determine sort column for aggregate results
    agg_sort = _agg_sort_column(sort_by, metrics)

    sql = f"""
        SELECT {group_col}, {metrics_sql}
        FROM ({union_sql}) t
        GROUP BY {group_col}
        ORDER BY {agg_sort} {sort_order.upper()}
        LIMIT %s
    """
    cur.execute(sql, union_params + [limit])
    rows = [dict(r) for r in cur.fetchall()]

    result = {
        "type": f"trades_by_{aggregate}",
        "count": len(rows),
        "fys_queried": [ft[0] for ft in fy_tables],
        "stoploss_pct_used": sl_pct,
        "results": to_jsonable(rows),
    }
    if sector_warning:
        result["note"] = sector_warning
    return result


def _build_metrics_sql(metrics: list[str], sl_pct: float) -> str:
    parts = []
    for m in metrics:
        if m == "count":
            parts.append("COUNT(*) as trades")
        elif m == "sum_pl":
            parts.append("ROUND(SUM(realized_pl)::numeric) as total_pl")
        elif m == "win_rate":
            parts.append(
                "ROUND(SUM(CASE WHEN is_winner THEN 1 ELSE 0 END)::numeric * 100.0 / "
                "NULLIF(COUNT(*), 0), 1) as win_rate"
            )
        elif m == "avg_return":
            parts.append("ROUND(AVG(realized_pl_pct)::numeric, 2) as avg_return_pct")
        elif m == "best_trade":
            parts.append("ROUND(MAX(realized_pl_pct)::numeric, 2) as best_return_pct")
            parts.append("ROUND(MAX(realized_pl)::numeric) as best_pl")
        elif m == "worst_trade":
            parts.append("ROUND(MIN(realized_pl_pct)::numeric, 2) as worst_return_pct")
            parts.append("ROUND(MIN(realized_pl)::numeric) as worst_pl")
        elif m == "avg_r_multiple":
            parts.append(
                f"ROUND(AVG(realized_pl_pct / {sl_pct})::numeric, 2) as avg_r_multiple"
            )
        elif m == "profit_factor":
            parts.append(
                "ROUND("
                "SUM(CASE WHEN realized_pl > 0 THEN realized_pl ELSE 0 END)::numeric / "
                "NULLIF(ABS(SUM(CASE WHEN realized_pl <= 0 THEN realized_pl ELSE 0 END))::numeric, 0)"
                ", 2) as profit_factor"
            )
        elif m == "expectancy":
            parts.append(
                "ROUND(AVG(realized_pl)::numeric) as expectancy_per_trade"
            )
    if not parts:
        parts.append("COUNT(*) as trades")
    return ", ".join(parts)


def _agg_sort_column(sort_by: str, metrics: list[str]) -> str:
    if sort_by == "pl" and "sum_pl" in metrics:
        return "total_pl"
    if sort_by == "return_pct" and "avg_return" in metrics:
        return "avg_return_pct"
    if sort_by == "r_multiple" and "avg_r_multiple" in metrics:
        return "avg_r_multiple"
    if "sum_pl" in metrics:
        return "total_pl"
    if "count" in metrics:
        return "trades"
    return "1"


# ═══════════════════════════════════════════════════════════════════════════
#  query_monthly
# ═══════════════════════════════════════════════════════════════════════════

def exec_query_monthly(params: dict) -> dict:
    """Monthly P&L summaries from pre-computed legacy_monthly_* tables."""
    uid = _get_user_id()
    fys = params.get("fys") or [_current_fy()]

    if fys == ["all"]:
        target_fys = list(MONTHLY_TABLE_MAP.keys())
    else:
        target_fys = [fy for fy in fys if fy in MONTHLY_TABLE_MAP]

    if not target_fys:
        return {
            "error": (
                "No monthly tables for the requested FY(s). "
                "Monthly summaries exist for FY 2020-21 through 2025-26."
            ),
        }

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor()
        if uid:
            cur.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"sub": str(uid)}),),
            )
            cur.execute("SET LOCAL ROLE authenticated")

        union_parts = []
        for fy in target_fys:
            tbl = MONTHLY_TABLE_MAP[fy]
            union_parts.append(
                f"SELECT '{fy}' as fy, month_label, month_order, "
                f"net_pf_impact, after_charges, charges, "
                f"scripts_traded, approx_trades, win_rate, total_buy_value "
                f"FROM {tbl}"
            )

        sql = f"""
            SELECT * FROM ({' UNION ALL '.join(union_parts)}) t
            ORDER BY fy, month_order
        """
        cur.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]

        return {
            "type": "monthly_summary",
            "count": len(rows),
            "fys_queried": target_fys,
            "months": to_jsonable(rows),
        }
    except Exception as exc:
        return {"error": f"Monthly query error: {exc}"}
    finally:
        close_db(conn)
