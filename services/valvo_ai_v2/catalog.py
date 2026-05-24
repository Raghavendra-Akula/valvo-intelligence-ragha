from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from database.database import get_db

from .utils import DEFAULT_BASE_CAPITAL, compact_whitespace, current_fy_label, money_text, pct_text, to_jsonable


def _db():
    return get_db()


def _uid():
    """Flask request user_id, or None if missing context. Used to scope every
    per-user query in this module — the Python backend runs as service_role
    which bypasses RLS, so explicit WHERE user_id = %s is the only line of
    defense against cross-user reads."""
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


def _parse_fy_from_text(text: str | None):
    if not text:
        return None
    lowered = compact_whitespace(text).lower().replace(" ", "")
    alias_map = {
        "fy22-23": "2022-23",
        "fy23-24": "2023-24",
        "fy24-25": "2024-25",
        "fy25-26": "2025-26",
        "fy26-27": "2026-27",
    }
    for alias, fy in alias_map.items():
        if alias in lowered:
            return fy
    for fy in FY_TABLES:
        if fy in lowered:
            return fy
    return None


def _parse_year_window(text: str | None, default: int = 3):
    if not text:
        return default
    lowered = compact_whitespace(text).lower()
    for years in range(5, 1, -1):
        if f"last {years} year" in lowered or f"past {years} year" in lowered:
            return years
    return default


def _parse_period_from_text(text: str | None):
    if not text:
        return None
    lowered = compact_whitespace(text).lower()
    period_aliases = [
        ("full", "full"),
        ("all time", "full"),
        ("entire history", "full"),
        ("2 years", "2y"),
        ("last 2 years", "2y"),
        ("past 2 years", "2y"),
        ("1 year", "1y"),
        ("last year", "1y"),
        ("last 1 year", "1y"),
        ("past year", "1y"),
        ("6 months", "6m"),
        ("last 6 months", "6m"),
        ("past 6 months", "6m"),
        ("2 months", "2m"),
        ("last 2 months", "2m"),
        ("past 2 months", "2m"),
        ("1 month", "1m"),
        ("last month", "1m"),
        ("past month", "1m"),
    ]
    for alias, period in period_aliases:
        if alias in lowered:
            return period
    return None


def _parse_top_n_from_text(text: str | None, default: int = 5, minimum: int = 1, maximum: int = 10):
    if not text:
        return default
    match = re.search(r"\b(\d{1,2})\b", text)
    if not match:
        return default
    value = int(match.group(1))
    return max(minimum, min(value, maximum))


_STOCK_STOPWORDS = {
    "A",
    "ABOUT",
    "ADR",
    "AI",
    "ALL",
    "AND",
    "ARE",
    "ASK",
    "AT",
    "AVERAGE",
    "BIGGEST",
    "CAN",
    "CAP",
    "CHANGE",
    "CHART",
    "CHECK",
    "CLOSE",
    "CMP",
    "COMPARE",
    "CURRENT",
    "DATA",
    "DAY",
    "DEEP",
    "DO",
    "DOES",
    "EXPLAIN",
    "FOR",
    "FROM",
    "GET",
    "GIVE",
    "HAS",
    "HIGH",
    "HOW",
    "I",
    "IN",
    "INDUSTRY",
    "INSIGHTS",
    "IS",
    "IT",
    "ITS",
    "LAST",
    "LIQUIDITY",
    "LOW",
    "MA",
    "MARKET",
    "ME",
    "MOVING",
    "OF",
    "ON",
    "OR",
    "PRICE",
    "R",
    "RANGE",
    "RELATIVE",
    "RS",
    "SECTOR",
    "SHOW",
    "STOCK",
    "STRENGTH",
    "TELL",
    "THE",
    "THIS",
    "TO",
    "TODAY",
    "WEEK",
    "WHAT",
    "WHICH",
    "WITH",
    "YEAR",
}


def _stock_tokens(text: str | None):
    if not text:
        return []
    tokens = []
    seen = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9&.-]{1,24}", text):
        token = raw.strip().strip(".").strip(",").strip("?").strip("!").upper()
        if not token or token in _STOCK_STOPWORDS:
            continue
        if token.isdigit():
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _resolve_stock_reference(cur, symbol_hint: str | None = None, message: str | None = None):
    candidates = []
    for token in [symbol_hint, *(_stock_tokens(message) or [])]:
        cleaned = (token or "").strip().upper()
        if not cleaned or cleaned in candidates:
            continue
        candidates.append(cleaned)

    for candidate in candidates:
        cur.execute(
            """
            SELECT security_id, symbol, company_name
            FROM stock_universe
            WHERE is_active = true AND UPPER(symbol) = %s
            LIMIT 1
            """,
            (candidate,),
        )
        exact = cur.fetchone()
        if exact:
            return exact

    for candidate in candidates:
        if len(candidate) < 3:
            continue
        cur.execute(
            """
            SELECT security_id, symbol, company_name
            FROM stock_universe
            WHERE is_active = true AND company_name ILIKE %s
            ORDER BY LENGTH(company_name) ASC
            LIMIT 1
            """,
            (f"%{candidate}%",),
        )
        fuzzy = cur.fetchone()
        if fuzzy:
            return fuzzy
    return None


# Symbol fragments that flag non-regular equity share classes. A hit on any
# of these means the symbol is a DVR / partly-paid / bonds-equity / etc., not
# the common retail share. The resolver prefers regular equity when a fuzzy
# company_name match returns both kinds — Jain Irrigation Systems is the
# real-world case (JISLJALEQS regular vs JISLDVREQS DVR).
_NON_REGULAR_EQUITY_MARKERS = ("DVR", "_PP", "-PP", "PARTLY")


def _is_regular_equity(symbol: str | None) -> bool:
    s = (symbol or "").upper()
    return not any(marker in s for marker in _NON_REGULAR_EQUITY_MARKERS)


def resolve_stock_reference_strict(cur, symbol_hint: str | None = None):
    """Like _resolve_stock_reference, but refuses to silently pick a winner
    when the fuzzy path matches multiple rows — with one exception: if the
    ambiguity is purely between a regular equity and a DVR / partly-paid
    variant under the same company name, prefer the regular equity. That's
    what the user means 99% of the time; if they truly want the DVR they
    can pass the exact symbol (e.g. JISLDVREQS), which hits the exact-match
    path and bypasses the fuzzy logic entirely.

    Returns one of:
      - {"security_id": ..., "symbol": ..., "company_name": ...} — single match
      - {"ambiguous": True, "matches": [row, row, ...]} — >1 match, all
        regular equity (e.g. two unrelated companies both matching the hint)
      - None — no match
    """
    if not (symbol_hint or "").strip():
        return None
    cleaned = symbol_hint.strip().upper()

    # Exact symbol is always unambiguous — skip the multi-row path. This is
    # also how a user who specifically wants the DVR share gets it.
    cur.execute(
        """
        SELECT security_id, symbol, company_name
        FROM stock_universe
        WHERE is_active = true AND UPPER(symbol) = %s
        LIMIT 1
        """,
        (cleaned,),
    )
    exact = cur.fetchone()
    if exact:
        return exact

    if len(cleaned) < 3:
        return None

    # Fuzzy on company_name — fetch up to 5 to detect dual share classes etc.
    cur.execute(
        """
        SELECT security_id, symbol, company_name
        FROM stock_universe
        WHERE is_active = true AND company_name ILIKE %s
        ORDER BY LENGTH(company_name) ASC
        LIMIT 5
        """,
        (f"%{cleaned}%",),
    )
    rows = [dict(r) for r in (cur.fetchall() or [])]
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    # Multiple fuzzy matches. Prefer regular equity — drops DVR / partly-paid
    # variants unless the user asked for one by exact symbol (already handled
    # above). If exactly one regular-equity row survives, that wins.
    regular = [r for r in rows if _is_regular_equity(r.get("symbol"))]
    if len(regular) == 1:
        return regular[0]

    # Still ambiguous after the preference — two distinct regular listings
    # matching the same name fragment (or none if all were DVR-like). Caller
    # (create_position) surfaces this as ambiguous_stock_reference so the
    # LLM asks the user to pick by symbol.
    return {"ambiguous": True, "matches": rows}


def _resolve_index_symbol(cur, index_hint: str | None = None, message: str | None = None):
    candidates = []
    for token in [index_hint, compact_whitespace(message or "").upper()]:
        cleaned = (token or "").strip().upper()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)

    cur.execute(
        """
        SELECT DISTINCT symbol AS index_symbol
        FROM candles_indices
        WHERE symbol ILIKE 'NIFTY%%'
        ORDER BY symbol
        """
    )
    symbols = [row["index_symbol"] for row in cur.fetchall()]
    message_upper = compact_whitespace(message or "").upper()
    for symbol in symbols:
        if symbol.upper() in message_upper:
            return symbol
    for token in _stock_tokens(message):
        for symbol in symbols:
            if token in symbol.upper():
                return symbol
    for candidate in candidates:
        if not candidate:
            continue
        for symbol in symbols:
            if candidate == symbol.upper():
                return symbol
    return None


def _historical_trade_union_sql():
    return """
        SELECT '2022-23' AS fy, 1 AS fy_sort, id AS sort_id, month_label, symbol, quantity,
               buy_value::real AS buy_value, sell_value::real AS sell_value,
               realized_pl::real AS realized_pl, realized_pl_pct::real AS realized_pl_pct,
               impact_on_pf::real AS impact_on_pf, is_winner
        FROM legacy_trades_fy2223
        UNION ALL
        SELECT '2023-24' AS fy, 2 AS fy_sort, id AS sort_id, month_label, symbol, quantity,
               buy_value::real, sell_value::real,
               realized_pl::real, realized_pl_pct::real,
               impact_on_pf::real, is_winner
        FROM legacy_trades_fy2324
        UNION ALL
        SELECT '2024-25' AS fy, 3 AS fy_sort, id AS sort_id, month_label, symbol, quantity,
               buy_value::real, sell_value::real,
               realized_pl::real, realized_pl_pct::real,
               impact_on_pf::real, is_winner
        FROM legacy_trades_fy2425
        UNION ALL
        SELECT '2025-26' AS fy, 4 AS fy_sort, id AS sort_id, month_label, symbol, quantity,
               buy_value::real, sell_value::real,
               realized_pl::real, realized_pl_pct::real,
               impact_on_pf::real, is_winner
        FROM legacy_trades
        UNION ALL
        SELECT '2026-27' AS fy, 5 AS fy_sort, trade_no AS sort_id, month_label, symbol, initial_qty AS quantity,
               (entry_price * initial_qty)::real AS buy_value,
               ((entry_price * initial_qty) + realized_pl)::real AS sell_value,
               realized_pl::real, realized_pl_pct::real,
               NULL::real AS impact_on_pf, is_winner
        FROM journal_trades_computed
    """


def _base_capital(cur) -> float:
    """Return the user's base capital for the current FY.

    Source of truth: user_fy_config. Settings > Capital now writes there
    (see settings_routes update_settings) and the AI set_fy_base_capital
    action writes there. user_settings.base_capital is DEPRECATED — still
    read as a fallback for pre-consolidation rows, but new writes all go
    to user_fy_config. Same story for user_profiles.current_capital.
    """
    uid = _uid()
    if not uid:
        return DEFAULT_BASE_CAPITAL

    fy = current_fy_label()

    # 1. Source of truth: user_fy_config for the current FY.
    try:
        cur.execute(
            "SELECT base_capital FROM user_fy_config WHERE user_id = %s AND fy = %s",
            (uid, fy),
        )
        row = cur.fetchone()
        if row and row.get("base_capital"):
            return float(row["base_capital"])
    except Exception:
        pass

    # 2. Any FY the user has configured (newest) — covers the case where
    #    the user set a base for a prior FY but not the current one.
    try:
        cur.execute(
            """
            SELECT base_capital
            FROM user_fy_config
            WHERE user_id = %s AND base_capital IS NOT NULL
            ORDER BY fy DESC
            LIMIT 1
            """,
            (uid,),
        )
        row = cur.fetchone()
        if row and row.get("base_capital"):
            return float(row["base_capital"])
    except Exception:
        pass

    # 3. Legacy: user_settings.base_capital (deprecated, migration bandage).
    try:
        cur.execute("SELECT base_capital FROM user_settings WHERE user_id = %s", (uid,))
        row = cur.fetchone()
        if row and row.get("base_capital"):
            return float(row["base_capital"])
    except Exception:
        pass

    # 4. Legacy: trader profile's current_capital (deprecated).
    try:
        cur.execute(
            "SELECT current_capital FROM user_profiles WHERE user_id = %s",
            (uid,),
        )
        row = cur.fetchone()
        if row and row.get("current_capital"):
            return float(row["current_capital"])
    except Exception:
        pass

    return DEFAULT_BASE_CAPITAL


def _fetch_positions(cur, stock_name: str | None = None, include_closed: bool = False):
    filters = []
    params: list[Any] = []
    if not include_closed:
        filters.append("status = 'active'")
    if stock_name:
        filters.append("stock_name ILIKE %s")
        params.append(f"%{stock_name}%")
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    cur.execute(
        f"""
        SELECT *
        FROM positions
        {where_sql}
        ORDER BY entry_extension_pct DESC NULLS LAST, created_at DESC
        """,
        params,
    )
    return cur.fetchall()


def _position_card(row):
    entry = float(row.get("entry_price") or 0)
    cmp_value = float(row.get("current_price") or entry)
    quantity = int(row.get("quantity") or 0)
    stop_loss = float(row.get("stop_loss") or 0)
    risk_pct = float(row.get("risk_pct") or 4)
    risk_per_share = entry * (risk_pct / 100) if entry else 0
    ext = float(row.get("entry_extension_pct") or 0)
    if not ext and entry:
        ext = round(((cmp_value - entry) / entry) * 100, 1)
    r_multiple = float(row.get("current_r_multiple") or 0)
    if not r_multiple and risk_per_share:
        r_multiple = round((cmp_value - entry) / risk_per_share, 1)
    pnl_val = (cmp_value - entry) * quantity
    value = cmp_value * quantity
    five_ma = float(row.get("last_5ma_price") or 0)
    daily_sell_from = float(row.get("daily_sell_from") or 60)
    first_sell_zone = float(row.get("first_sell_zone") or 35)
    return {
        "id": row["id"],
        "name": row["stock_name"],
        "entry": round(entry, 2),
        "cmp": round(cmp_value, 2),
        "sl": round(stop_loss, 2),
        "qty": quantity,
        "ext": round(ext, 1),
        "r": round(r_multiple, 1),
        "pnl_val": round(pnl_val),
        "value": round(value),
        "five_ma": round(five_ma, 2),
        "defensive": row.get("defensive_status") or "safe",
        "bucket_sold": round(float(row.get("bucket_sold_pct") or 0)),
        "bucket_cap": round(float(row.get("bucket_cap") or 66)),
        "first_sell": bool(row.get("first_sell_done")),
        "first_sell_zone": first_sell_zone,
        "daily_sell_from": daily_sell_from,
        "regime": row.get("market_regime") or "bull",
        "ma_grade": row.get("ma_grade") or "?",
        "ma_followed": row.get("ma_followed") or "?",
        "trail_worthy": bool(row.get("qualifies_for_trailing")),
        "security_id": row.get("security_id"),
        "status": row.get("status"),
        "sell_history": to_jsonable(row.get("sell_history") or []),
        "journal_entries": to_jsonable(row.get("journal_entries") or []),
    }


def _summary(cards):
    total_value = sum(card["value"] for card in cards)
    total_pnl = sum(card["pnl_val"] for card in cards)
    invested = total_value - total_pnl
    return {
        "count": len(cards),
        "total_value": round(total_value),
        "total_pnl": round(total_pnl),
        "total_pnl_pct": round((total_pnl / invested) * 100, 1) if invested > 0 else 0,
        "avg_r": round(sum(card["r"] for card in cards) / len(cards), 1) if cards else 0,
        "green": sum(1 for card in cards if card["r"] > 0),
        "red": sum(1 for card in cards if card["r"] <= 0),
        "above_2r": sum(1 for card in cards if card["r"] >= 2),
    }


def read_portfolio_overview(args: dict):
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        cards = [_position_card(row) for row in _fetch_positions(cur)]
        if not cards:
            return {"type": "empty", "message": "No active positions"}
        return {"type": "positions", "summary": _summary(cards), "cards": cards}
    finally:
        conn.close()


def read_sell_actions(args: dict):
    # MIRROR WARNING: Frontend has parallel action logic in
    # Frontend/src/components/PositionManager.jsx → getActionRequired()
    # If you change verdict labels/conditions here, check the frontend mirror too.
    payload = read_portfolio_overview(args)
    if payload.get("type") == "empty":
        return payload
    action_cards = []
    for card in payload["cards"]:
        cls = "hold"
        verdict = "HOLD"
        verdict_color = "var(--vai-gn)"
        detail = "No forced action right now."
        urgency = 10
        if card["defensive"] in {"break", "warning"}:
            cls = "exit"
            verdict = "EXIT — 5MA Break"
            verdict_color = "var(--vai-rd)"
            detail = f"Price is below the 5MA at Rs {card['five_ma']}."
            urgency = 100
        elif card["ext"] >= card["daily_sell_from"] and not card["first_sell"]:
            cls = "exit"
            verdict = "SELL 35% — Overdue"
            verdict_color = "var(--vai-rd)"
            detail = f"Extension is {card['ext']}% without the first sell."
            urgency = 90
        elif card["ext"] >= card["first_sell_zone"] and not card["first_sell"]:
            cls = "sell"
            verdict = "SELL 30-35%"
            verdict_color = "var(--vai-og)"
            detail = f"First sell zone reached at {card['ext']}%."
            urgency = 70
        elif card["ext"] >= card["daily_sell_from"] and card["bucket_sold"] < card["bucket_cap"]:
            pct = min(10, max(card["bucket_cap"] - card["bucket_sold"], 0))
            cls = "sell"
            verdict = f"SELL {pct:.0f}%"
            verdict_color = "var(--vai-og)"
            detail = f"Daily trimming zone active at {card['ext']}% extension."
            urgency = 60
        metric_color = "var(--vai-gn)" if card["r"] >= 0 else "var(--vai-rd)"
        action_cards.append(
            {
                "name": card["name"],
                "verdict": verdict,
                "verdictColor": verdict_color,
                "cls": cls,
                "detail": detail,
                "metric": f"{pct_text(card['ext'], 0)} | {card['r']:+.1f}R",
                "metricColor": metric_color,
                "urgency": urgency,
                "ext": card["ext"],
                "r": card["r"],
                "bucket_sold": card["bucket_sold"],
                "bucket_cap": card["bucket_cap"],
                "cmp": card["cmp"],
                "five_ma": card["five_ma"],
            }
        )
    action_cards.sort(key=lambda item: item["urgency"], reverse=True)
    return {"type": "actions", "summary": _summary(payload["cards"]), "cards": action_cards}


def read_portfolio_risk(args: dict):
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        rows = _fetch_positions(cur)
        if not rows:
            return {"type": "empty", "message": "No active positions"}
        base_capital = _base_capital(cur)
        cards = []
        total_risk = 0.0
        total_value = 0.0
        for row in rows:
            card = _position_card(row)
            value = float(card["value"])
            total_value += value
            if not card["sl"]:
                risk_value = value
                no_sl = True
                sl_dist_pct = 100.0
            else:
                risk_value = max((card["cmp"] - card["sl"]) * card["qty"], 0)
                no_sl = False
                sl_dist_pct = round(((card["cmp"] - card["sl"]) / card["cmp"]) * 100, 1) if card["cmp"] else 0
            total_risk += risk_value
            cards.append(
                {
                    "name": card["name"],
                    "sl": card["sl"],
                    "risk_rupees": round(risk_value),
                    "sl_dist_pct": sl_dist_pct,
                    "pos_pct": round((value / base_capital) * 100, 1) if base_capital else 0,
                    "no_sl": no_sl,
                }
            )
        cards.sort(key=lambda item: item["risk_rupees"], reverse=True)
        stats = [
            {"label": "Total Risk", "val": money_text(total_risk), "sub": "Defined downside", "color": "var(--vai-rd)"},
            {"label": "Invested", "val": money_text(total_value), "sub": "Gross active capital", "color": "var(--vai-accent)"},
            {"label": "Cash", "val": money_text(base_capital - total_value), "sub": "Base capital minus active", "color": "var(--vai-gn)"},
        ]
        return {
            "type": "risk",
            "portfolio_size": base_capital,
            "total_risk": round(total_risk),
            "stats": stats,
            "cards": cards,
        }
    finally:
        conn.close()


def read_portfolio_rankings(args: dict):
    payload = read_portfolio_overview(args)
    if payload.get("type") == "empty":
        return payload
    sort_by = (args.get("sort_by") or "r").lower()
    cards = list(payload["cards"])
    if sort_by == "pnl":
        cards.sort(key=lambda item: item["pnl_val"], reverse=True)
        sort_label = "Ranked by P&L"
    elif sort_by == "extension":
        cards.sort(key=lambda item: item["ext"], reverse=True)
        sort_label = "Ranked by Extension"
    else:
        cards.sort(key=lambda item: item["r"], reverse=True)
        sort_label = "Ranked by R Multiple"
    ranked = []
    for index, card in enumerate(cards, start=1):
        ranked.append(
            {
                "rank": index,
                "name": card["name"],
                "r": card["r"],
                "ext": card["ext"],
                "pnl_val": card["pnl_val"],
                "defensive": card["defensive"],
            }
        )
    return {"type": "rankings", "sort_label": sort_label, "cards": ranked}


def read_portfolio_trailing(args: dict):
    payload = read_portfolio_overview(args)
    if payload.get("type") == "empty":
        return payload
    cards = []
    alerts = 0
    for card in payload["cards"]:
        action = "OK"
        if card["defensive"] in {"break", "warning"}:
            action = "EXIT"
            alerts += 1
        elif card["defensive"] in {"marginal"}:
            action = "WATCH"
            alerts += 1
        cards.append(
            {
                "name": card["name"],
                "cmp": card["cmp"],
                "five_ma": card["five_ma"],
                "sl": card["sl"],
                "action": action,
                "defensive": card["defensive"],
                "distance_pct": round(((card["cmp"] - card["five_ma"]) / card["five_ma"]) * 100, 1) if card["five_ma"] else None,
            }
        )
    return {"type": "trailing", "alerts": alerts, "total": len(cards), "cards": cards}


def read_position_detail(args: dict):
    stock_name = args.get("stock_name") or args.get("stock_context")
    if not stock_name:
        return {"type": "empty", "message": "No stock specified"}
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        rows = _fetch_positions(cur, stock_name=stock_name, include_closed=True)
        if not rows:
            return {"type": "empty", "message": f"No position found for {stock_name}"}
        row = rows[0]
        card = _position_card(row)
        return {
            "type": "single_stock",
            "card": {
                **card,
                "position_value": round(float(row.get("position_value") or 0)),
                "one_r_value": round(float(row.get("one_r_value") or 0)),
                "regime": row.get("market_regime") or "bull",
                "status": row.get("status"),
                "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            },
        }
    finally:
        conn.close()


def read_journal_summary(args: dict):
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH all_trades AS (
                SELECT symbol, realized_pl, realized_pl_pct, is_winner
                FROM (
                    """ + _historical_trade_union_sql() + """
                ) combined
            )
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) AS win_count,
                SUM(CASE WHEN NOT is_winner THEN 1 ELSE 0 END) AS loss_count,
                AVG(CASE WHEN is_winner THEN realized_pl_pct END) AS avg_winner_pct,
                AVG(CASE WHEN NOT is_winner THEN realized_pl_pct END) AS avg_loser_pct,
                SUM(realized_pl) AS total_pl
            FROM all_trades
            """
        )
        stats = cur.fetchone() or {}
        cur.execute(
            """
            WITH all_trades AS (
                SELECT symbol, realized_pl
                FROM (
                    """ + _historical_trade_union_sql() + """
                ) combined
            )
            SELECT symbol, realized_pl
            FROM all_trades
            ORDER BY realized_pl DESC
            LIMIT 1
            """
        )
        best = cur.fetchone() or {}
        cur.execute(
            """
            WITH all_trades AS (
                SELECT symbol, realized_pl
                FROM (
                    """ + _historical_trade_union_sql() + """
                ) combined
            )
            SELECT symbol, realized_pl
            FROM all_trades
            ORDER BY realized_pl ASC
            LIMIT 1
            """
        )
        worst = cur.fetchone() or {}
        total = int(stats.get("total_trades") or 0)
        wins = int(stats.get("win_count") or 0)
        losses = int(stats.get("loss_count") or 0)
        return {
            "type": "journal_stats",
            "stats": {
                "total_trades": total,
                "win_count": wins,
                "loss_count": losses,
                "win_rate": round((wins / total) * 100, 2) if total else 0,
                "avg_winner_pct": round(float(stats.get("avg_winner_pct") or 0), 2),
                "avg_loser_pct": round(float(stats.get("avg_loser_pct") or 0), 2),
                "total_pl": round(float(stats.get("total_pl") or 0)),
                "best_trade": best.get("symbol"),
                "worst_trade": worst.get("symbol"),
            },
        }
    finally:
        conn.close()


def read_streak_analysis(args: dict):
    requested_fy = args.get("fy") or _parse_fy_from_text(args.get("message"))
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        union_sql = _historical_trade_union_sql()
        params = []
        fy_filter = ""
        if requested_fy and requested_fy != "all":
            fy_filter = "WHERE fy = %s"
            params.append(requested_fy)
        cur.execute(
            f"""
            WITH all_trades AS (
                {union_sql}
            )
            SELECT fy, month_label, symbol, realized_pl, realized_pl_pct, is_winner
            FROM all_trades
            {fy_filter}
            ORDER BY fy_sort, sort_id
            """,
            params,
        )
        rows = [dict(row) for row in cur.fetchall()]
        if not rows:
            return {"type": "empty", "message": "No trade history available"}

        streaks = []
        trade_sequence = []
        current_type = None
        current_len = 0
        current_pl = 0.0
        current_names = []
        max_win = {"type": "W", "len": 0, "pl": 0, "names": [], "start": None, "end": None}
        max_loss = {"type": "L", "len": 0, "pl": 0, "names": [], "start": None, "end": None}

        def finalize_streak():
            nonlocal max_win, max_loss, current_type, current_len, current_pl, current_names
            if not current_type:
                return
            streak_record = {
                "type": current_type,
                "len": current_len,
                "pl": round(current_pl),
                "names": list(current_names),
                "start": current_names[0] if current_names else None,
                "end": current_names[-1] if current_names else None,
            }
            streaks.append(streak_record)
            if current_type == "W" and current_len > max_win["len"]:
                max_win = streak_record
            if current_type == "L" and current_len > max_loss["len"]:
                max_loss = streak_record

        for row in rows:
            streak_type = "W" if bool(row.get("is_winner")) else "L"
            pl_value = float(row.get("realized_pl") or 0)
            trade_sequence.append(
                {
                    "symbol": row.get("symbol"),
                    "is_winner": streak_type == "W",
                    "realized_pl": round(pl_value),
                    "realized_pl_pct": round(float(row.get("realized_pl_pct") or 0), 2),
                }
            )
            if streak_type == current_type:
                current_len += 1
                current_pl += pl_value
                current_names.append(row.get("symbol"))
            else:
                finalize_streak()
                current_type = streak_type
                current_len = 1
                current_pl = pl_value
                current_names = [row.get("symbol")]
        finalize_streak()

        after_2_losses = []
        after_3_wins = []
        for index in range(2, len(trade_sequence)):
            if not trade_sequence[index - 1]["is_winner"] and not trade_sequence[index - 2]["is_winner"]:
                after_2_losses.append(trade_sequence[index]["is_winner"])
            if index >= 3 and all(trade_sequence[index - offset]["is_winner"] for offset in [1, 2, 3]):
                after_3_wins.append(trade_sequence[index]["is_winner"])

        return {
            "type": "streak_analysis",
            "fy": requested_fy or "all",
            "trade_count": len(rows),
            "current": {"type": current_type, "len": current_len, "pl": round(current_pl), "last_symbol": current_names[-1] if current_names else None},
            "best_streak": max_win,
            "worst_streak": max_loss,
            "after_2_losses": {
                "sample_size": len(after_2_losses),
                "next_win_pct": round(sum(after_2_losses) / len(after_2_losses) * 100, 1) if after_2_losses else None,
            },
            "after_3_wins": {
                "sample_size": len(after_3_wins),
                "next_win_pct": round(sum(after_3_wins) / len(after_3_wins) * 100, 1) if after_3_wins else None,
            },
        }
    finally:
        conn.close()


def read_trade_highlights(args: dict):
    years = max(1, min(int(args.get("years") or 3), 5))
    requested_fy = args.get("fy") or _parse_fy_from_text(args.get("message"))
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        recent_fys = []
        for fy_label, config in FY_TABLES.items():
            cur.execute(f"SELECT COUNT(*) AS count FROM {config['trades']}")
            count = int((cur.fetchone() or {}).get("count") or 0)
            if count > 0:
                recent_fys.append(fy_label)
        recent_fys = recent_fys[-years:] or [fy for fy in list(FY_TABLES.keys())[-years:]]
        union_sql = """
            SELECT '2022-23' AS fy, month_label, symbol, quantity, buy_value::real AS buy_value, sell_value::real AS sell_value,
                   realized_pl::real AS realized_pl, realized_pl_pct::real AS realized_pl_pct, impact_on_pf::real AS impact_on_pf
            FROM legacy_trades_fy2223
            UNION ALL
            SELECT '2023-24' AS fy, month_label, symbol, quantity, buy_value::real, sell_value::real,
                   realized_pl::real, realized_pl_pct::real, impact_on_pf::real
            FROM legacy_trades_fy2324
            UNION ALL
            SELECT '2024-25' AS fy, month_label, symbol, quantity, buy_value::real, sell_value::real,
                   realized_pl::real, realized_pl_pct::real, impact_on_pf::real
            FROM legacy_trades_fy2425
            UNION ALL
            SELECT '2025-26' AS fy, month_label, symbol, quantity, buy_value::real, sell_value::real,
                   realized_pl::real, realized_pl_pct::real, impact_on_pf::real
            FROM legacy_trades
            UNION ALL
            SELECT '2026-27' AS fy, month_label, symbol, initial_qty AS quantity, (entry_price * initial_qty)::real AS buy_value,
                   ((entry_price * initial_qty) + realized_pl)::real AS sell_value,
                   realized_pl::real, realized_pl_pct::real, NULL::real AS impact_on_pf
            FROM journal_trades_computed
        """
        cur.execute(
            f"""
            WITH all_trades AS (
                {union_sql}
            )
            SELECT fy, month_label, symbol, quantity, buy_value, sell_value, realized_pl, realized_pl_pct, impact_on_pf
            FROM all_trades
            WHERE fy = ANY(%s)
            ORDER BY realized_pl DESC NULLS LAST
            LIMIT 1
            """,
            (recent_fys,),
        )
        best_recent = cur.fetchone()
        if not best_recent:
            return {"type": "empty", "message": "No trade history available"}

        target_fy = requested_fy or recent_fys[-1]
        cur.execute(
            f"""
            WITH all_trades AS (
                {union_sql}
            )
            SELECT fy, month_label, symbol, quantity, buy_value, sell_value, realized_pl, realized_pl_pct, impact_on_pf
            FROM all_trades
            WHERE fy = %s
            ORDER BY buy_value DESC NULLS LAST
            LIMIT 1
            """,
            (target_fy,),
        )
        biggest_fy = cur.fetchone()

        cur.execute(
            f"""
            WITH all_trades AS (
                {union_sql}
            )
            SELECT fy, month_label, symbol, quantity, buy_value, sell_value, realized_pl, realized_pl_pct, impact_on_pf
            FROM all_trades
            WHERE fy = ANY(%s)
            ORDER BY realized_pl DESC NULLS LAST
            LIMIT 5
            """,
            (recent_fys,),
        )
        top_recent = cur.fetchall() or []

        return {
            "type": "trade_highlights",
            "years": years,
            "recent_fys": recent_fys,
            "best_trade_last_years": to_jsonable(best_recent),
            "biggest_trade_fy": to_jsonable(biggest_fy) if biggest_fy else None,
            "target_fy": target_fy,
            "top_recent_trades": [to_jsonable(row) for row in top_recent],
        }
    finally:
        conn.close()


def read_trade_r_extremes(args: dict):
    requested_fy = args.get("fy") or _parse_fy_from_text(args.get("message"))
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        params: list[Any] = []
        fy_filter = ""
        if requested_fy and requested_fy != "all":
            fy_filter = "WHERE fy = %s"
            params.append(requested_fy)
        cur.execute(
            f"""
            WITH all_trades AS (
                SELECT fy, month_label, symbol, realized_pl, realized_pl_pct,
                       ROUND((realized_pl_pct / 3.0)::numeric, 2) AS r_multiple
                FROM (
                    {_historical_trade_union_sql()}
                ) combined
                {fy_filter}
            )
            SELECT fy, month_label, symbol, realized_pl, realized_pl_pct, r_multiple
            FROM all_trades
            ORDER BY r_multiple DESC NULLS LAST, realized_pl DESC NULLS LAST
            LIMIT 1
            """,
            params,
        )
        best_r = cur.fetchone()
        cur.execute(
            f"""
            WITH all_trades AS (
                SELECT fy, month_label, symbol, realized_pl, realized_pl_pct,
                       ROUND((realized_pl_pct / 3.0)::numeric, 2) AS r_multiple
                FROM (
                    {_historical_trade_union_sql()}
                ) combined
                {fy_filter}
            )
            SELECT fy, month_label, symbol, realized_pl, realized_pl_pct, r_multiple
            FROM all_trades
            ORDER BY r_multiple ASC NULLS LAST, realized_pl ASC NULLS LAST
            LIMIT 1
            """,
            params,
        )
        worst_r = cur.fetchone()
        if not best_r:
            return {"type": "empty", "message": "No trade history available"}
        return {
            "type": "trade_r_extremes",
            "fy": requested_fy or "all",
            "best_r_trade": to_jsonable(best_r),
            "worst_r_trade": to_jsonable(worst_r) if worst_r else None,
        }
    finally:
        conn.close()


def read_top_trade_winners(args: dict):
    requested_fy = args.get("fy") or _parse_fy_from_text(args.get("message"))
    limit = max(1, min(int(args.get("limit") or 5), 10))
    sort_by = (args.get("sort_by") or "pct").lower()
    sort_sql = "realized_pl_pct DESC NULLS LAST, realized_pl DESC NULLS LAST" if sort_by == "pct" else "realized_pl DESC NULLS LAST, realized_pl_pct DESC NULLS LAST"
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        params: list[Any] = []
        filters = ["realized_pl > 0"]
        if requested_fy and requested_fy != "all":
            filters.append("fy = %s")
            params.append(requested_fy)
        where_sql = f"WHERE {' AND '.join(filters)}"
        params.append(limit)
        cur.execute(
            f"""
            WITH all_trades AS (
                SELECT fy, month_label, symbol, quantity, buy_value, sell_value, realized_pl, realized_pl_pct
                FROM (
                    {_historical_trade_union_sql()}
                ) combined
            )
            SELECT fy, month_label, symbol, quantity, buy_value, sell_value, realized_pl, realized_pl_pct
            FROM all_trades
            {where_sql}
            ORDER BY {sort_sql}
            LIMIT %s
            """,
            params,
        )
        rows = [to_jsonable(row) for row in cur.fetchall()]
        if not rows:
            return {"type": "empty", "message": "No winning trade history available"}
        return {
            "type": "trade_winners",
            "fy": requested_fy or "all",
            "sort_by": sort_by,
            "count": len(rows),
            "cards": rows,
            "leader": rows[0],
        }
    finally:
        conn.close()


FY_TABLES = {
    "2020-21": {"trades": "legacy_trades_fy2021", "monthly": "legacy_monthly_fy2021", "base": 6_000_000},
    "2021-22": {"trades": "legacy_trades_fy2122", "monthly": "legacy_monthly_fy2122", "base": 9_075_419},
    "2022-23": {"trades": "legacy_trades_fy2223", "monthly": "legacy_monthly_fy2223", "base": 16_187_147},
    "2023-24": {"trades": "legacy_trades_fy2324", "monthly": "legacy_monthly_fy2324", "base": 18_028_240},
    "2024-25": {"trades": "legacy_trades_fy2425", "monthly": "legacy_monthly_fy2425", "base": 28_000_000},
    "2025-26": {"trades": "legacy_trades", "monthly": "legacy_monthly_summary", "base": 50_000_000},
    "2026-27": {"trades": "journal_trades_computed", "monthly": None, "base": 50_000_000},
}


def read_fy_summary(args: dict):
    fy = args.get("fy") or current_fy_label()
    config = FY_TABLES.get(fy, FY_TABLES["2025-26"])
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) AS winners,
                AVG(realized_pl_pct::real) AS avg_pl_pct,
                SUM(realized_pl::real) AS total_pl
            FROM {config['trades']}
            """
        )
        row = cur.fetchone() or {}
        total = int(row.get("total_trades") or 0)
        winners = int(row.get("winners") or 0)
        cur.execute(
            f"SELECT symbol, realized_pl::real AS realized_pl FROM {config['trades']} ORDER BY realized_pl DESC LIMIT 1"
        )
        best = cur.fetchone() or {}
        cur.execute(
            f"SELECT symbol, realized_pl::real AS realized_pl FROM {config['trades']} ORDER BY realized_pl ASC LIMIT 1"
        )
        worst = cur.fetchone() or {}
        total_pl = float(row.get("total_pl") or 0)
        base = float(config["base"])
        return {
            "type": "analytics_fy",
            "fy": fy,
            "summary": {
                "total_trades": total,
                "win_rate": round((winners / total) * 100, 2) if total else 0,
                "total_pl": round(total_pl),
                "return_pct": round((total_pl / base) * 100, 2) if base else 0,
                "avg_pl_pct": round(float(row.get("avg_pl_pct") or 0), 2),
                "best_trade": best.get("symbol"),
                "worst_trade": worst.get("symbol"),
            },
        }
    finally:
        conn.close()




def read_monthly_summary(args: dict):
    fy = args.get("fy") or current_fy_label()
    config = FY_TABLES.get(fy, FY_TABLES["2025-26"])
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        if config["monthly"]:
            cur.execute(
                f"""
                SELECT month_label, month_order, after_charges, win_rate, approx_trades
                FROM {config['monthly']}
                ORDER BY month_order
                """
            )
            rows = cur.fetchall()
        else:
            cur.execute(
                """
                SELECT
                    month_label,
                    ROW_NUMBER() OVER (ORDER BY MIN(trade_date)) AS month_order,
                    SUM(realized_pl::real) AS after_charges,
                    ROUND(AVG(CASE WHEN is_winner THEN 1.0 ELSE 0 END)::numeric * 100, 2) AS win_rate,
                    COUNT(*) AS approx_trades
                FROM journal_trades_computed
                GROUP BY month_label
                ORDER BY month_order
                """
            )
            rows = cur.fetchall()
        return {"type": "analytics_monthly", "fy": fy, "months": [to_jsonable(row) for row in rows]}
    finally:
        conn.close()


def read_journal_trade_book(args: dict):
    status = args.get("status") or "all"
    limit = max(1, min(int(args.get("limit") or 20), 60))
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        import psycopg2.extras
        from routes.journal_routes import PORTFOLIO_CAPITAL, _calc_fields, _serialize

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Scope both reads to the authenticated user. journal_settings and
        # journal_trades both have user_id columns — previously this read
        # every row in the table regardless of caller, leaking every other
        # user's journal verbatim to anyone who asked "show my journal".
        uid = _uid()
        if not uid:
            return {"type": "empty", "message": "Not authenticated"}

        # Base capital source of truth: user_fy_config.base_capital (per-FY).
        # journal_settings.portfolio_capital is deprecated — reading it served
        # stale 5Cr to every downstream calculation after Settings edits.
        portfolio_capital = PORTFOLIO_CAPITAL
        try:
            from services.user_analytics_service import get_user_base_capital
            bc = get_user_base_capital(cur, uid, current_fy_label())
            if bc and bc > 0:
                portfolio_capital = float(bc)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        cur.execute(
            "SELECT * FROM journal_trades WHERE user_id = %s ORDER BY trade_no DESC",
            (uid,),
        )
        all_rows = []
        for row in cur.fetchall():
            serialized = _serialize(row)
            calculated = _calc_fields(serialized, portfolio_capital=portfolio_capital)
            all_rows.append(calculated)

        if status == "open":
            filtered = [row for row in all_rows if row.get("position_status") == "Open"]
        elif status == "closed":
            filtered = [row for row in all_rows if row.get("position_status") == "Closed"]
        else:
            filtered = all_rows

        cards = []
        for row in filtered[:limit]:
            cards.append(
                {
                    "id": row.get("id"),
                    "trade_no": row.get("trade_no"),
                    "trade_date": row.get("trade_date"),
                    "symbol": row.get("symbol"),
                    "entry_type": row.get("entry_type"),
                    "self_rating": row.get("self_rating"),
                    "position_status": row.get("position_status"),
                    "gross_pl": row.get("gross_pl"),
                    "reward_risk": row.get("reward_risk"),
                    "holding_days": row.get("holding_days"),
                    "capital_at_risk_pct": row.get("capital_at_risk_pct"),
                }
            )

        return {
            "type": "journal_trade_book",
            "status": status,
            "count": len(filtered),
            "open_count": sum(1 for row in all_rows if row.get("position_status") == "Open"),
            "closed_count": sum(1 for row in all_rows if row.get("position_status") == "Closed"),
            "cards": to_jsonable(cards),
        }
    finally:
        conn.close()


def read_journal_settings_summary(args: dict):
    # Per-user scope — both journal_settings and journal_fund_months have
    # user_id UUID NOT NULL columns. Old code used id=1 / no-user-filter so
    # every caller saw/created id=1's rows. Rewritten to key everything
    # off _uid(). Refuses early if no auth context (bot / internal caller).
    year = int(args.get("year") or current_fy_label().split("-")[0])
    uid = _uid()
    if not uid:
        return {"type": "empty", "message": "Not authenticated"}
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        import psycopg2.extras

        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Seed a default journal_settings row for THIS user if none exists.
        # portfolio_capital is deprecated — real capital lives on
        # user_fy_config.base_capital, overlaid below.
        cur.execute(
            """
            INSERT INTO journal_settings (user_id) VALUES (%s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (uid,),
        )
        conn.commit()
        cur.execute("SELECT * FROM journal_settings WHERE user_id = %s", (uid,))
        settings = dict(cur.fetchone() or {})
        try:
            from services.user_analytics_service import get_user_base_capital
            bc = get_user_base_capital(cur, uid, current_fy_label())
            if bc and bc > 0:
                settings["portfolio_capital"] = float(bc)
        except Exception:
            pass

        # Per-user fund month seeding. ON CONFLICT clause targets the
        # composite (user_id, year, month) — if your journal_fund_months
        # unique-index is different, adjust the ON CONFLICT clause.
        for month in range(1, 13):
            cur.execute(
                """
                INSERT INTO journal_fund_months (user_id, year, month, added)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (uid, year, month, 50000000 if month == 1 and year == 2026 else 0),
            )
        conn.commit()
        cur.execute(
            "SELECT * FROM journal_fund_months WHERE user_id = %s AND year = %s ORDER BY month",
            (uid, year),
        )
        months = [dict(row) for row in cur.fetchall()]
        total_added = sum(float(row.get("added") or 0) for row in months)
        total_withdrawn = sum(float(row.get("withdrawn") or 0) for row in months)

        return {
            "type": "journal_settings_summary",
            "year": year,
            "settings": to_jsonable(settings),
            "fund_months": to_jsonable(months),
            "summary": {
                "total_added": round(total_added),
                "total_withdrawn": round(total_withdrawn),
                "net_added": round(total_added - total_withdrawn),
            },
        }
    finally:
        conn.close()


def read_trade_stats_period(args: dict):
    requested_period = args.get("period") or _parse_period_from_text(args.get("message")) or "1y"
    period_map = {"1m": 30, "2m": 60, "6m": 180, "1y": 365, "2y": 730, "full": 99999}
    period_order = ["1m", "2m", "6m", "1y", "2y", "full"]
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        active_period = requested_period if requested_period in period_order else "1y"
        active_days = period_map[active_period]
        all_trades_cte = """
            WITH all_trades AS (
                SELECT symbol, realized_pl::real AS realized_pl, realized_pl_pct::real AS realized_pl_pct,
                       TO_DATE(month_label, 'Month YYYY') AS trade_month
                FROM legacy_trades_fy2223
                UNION ALL
                SELECT symbol, realized_pl::real, realized_pl_pct::real, TO_DATE(month_label, 'Month YYYY')
                FROM legacy_trades_fy2324
                UNION ALL
                SELECT symbol, realized_pl::real, realized_pl_pct::real, TO_DATE(month_label, 'Month YYYY')
                FROM legacy_trades_fy2425
                UNION ALL
                SELECT symbol, realized_pl::real, realized_pl_pct::real, TO_DATE(month_label, 'Month YYYY')
                FROM legacy_trades
                UNION ALL
                SELECT symbol, realized_pl::real, realized_pl_pct::real,
                       COALESCE(trade_date, TO_DATE(month_label, 'Month YYYY'))
                FROM journal_trades_computed
            )
        """

        from datetime import datetime, timedelta

        start_index = period_order.index(active_period)
        for period in period_order[start_index:]:
            days = period_map[period]
            if days >= 99999:
                cur.execute(all_trades_cte + " SELECT COUNT(*) AS cnt FROM all_trades")
            else:
                cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
                cur.execute(all_trades_cte + " SELECT COUNT(*) AS cnt FROM all_trades WHERE trade_month >= %s", (cutoff,))
            count = int((cur.fetchone() or {}).get("cnt") or 0)
            active_period = period
            active_days = days
            if count > 0:
                break

        if active_days >= 99999:
            date_filter = ""
            params: tuple[Any, ...] = ()
        else:
            cutoff = (datetime.now() - timedelta(days=active_days)).strftime("%Y-%m-%d")
            date_filter = "WHERE trade_month >= %s"
            params = (cutoff,)

        cur.execute(
            all_trades_cte
            + f"""
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS win_count,
                SUM(CASE WHEN realized_pl <= 0 THEN 1 ELSE 0 END) AS loss_count,
                ROUND((SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100), 2) AS win_rate,
                ROUND(AVG(CASE WHEN realized_pl > 0 THEN realized_pl_pct END)::numeric, 2) AS avg_winner_pct,
                ROUND(AVG(CASE WHEN realized_pl <= 0 THEN realized_pl_pct END)::numeric, 2) AS avg_loser_pct,
                ROUND(SUM(realized_pl)::numeric) AS total_pl
            FROM all_trades
            {date_filter}
            """,
            params,
        )
        stats = cur.fetchone() or {}
        return {
            "type": "trade_stats_period",
            "period": active_period,
            "total_trades": int(stats.get("total_trades") or 0),
            "win_count": int(stats.get("win_count") or 0),
            "loss_count": int(stats.get("loss_count") or 0),
            "win_rate": float(stats.get("win_rate") or 0),
            "avg_winner_pct": float(stats.get("avg_winner_pct") or 0),
            "avg_loser_pct": float(stats.get("avg_loser_pct") or 0),
            "total_pl": round(float(stats.get("total_pl") or 0)),
        }
    finally:
        conn.close()


def read_analytics_overview(args: dict):
    fy = args.get("fy") or _parse_fy_from_text(args.get("message")) or "all"
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        base_capital = sum(float(config["base"]) for config in FY_TABLES.values() if config["monthly"]) if fy == "all" else float(FY_TABLES.get(fy, FY_TABLES["2025-26"])["base"])
        params: list[Any] = []
        trade_filter = ""
        if fy != "all":
            trade_filter = "WHERE fy = %s"
            params.append(fy)
        cur.execute(
            f"""
            WITH all_trades AS (
                SELECT fy, month_label, symbol, realized_pl, realized_pl_pct,
                       ROUND((realized_pl_pct / 3.0)::numeric, 2) AS r_multiple
                FROM (
                    {_historical_trade_union_sql()}
                ) combined
                {trade_filter}
            )
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN realized_pl <= 0 THEN 1 ELSE 0 END) AS losses,
                ROUND((SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100), 2) AS win_rate,
                ROUND(SUM(realized_pl)::numeric) AS total_pl,
                ROUND(AVG(CASE WHEN realized_pl > 0 THEN realized_pl_pct END)::numeric, 2) AS avg_winner_pct,
                ROUND(AVG(CASE WHEN realized_pl <= 0 THEN realized_pl_pct END)::numeric, 2) AS avg_loser_pct,
                ROUND(AVG(CASE WHEN realized_pl > 0 THEN r_multiple END)::numeric, 2) AS avg_winner_r,
                ROUND(AVG(CASE WHEN realized_pl <= 0 THEN r_multiple END)::numeric, 2) AS avg_loser_r,
                ROUND(AVG(r_multiple)::numeric, 3) AS expectancy_r,
                ROUND((SUM(CASE WHEN realized_pl > 0 THEN realized_pl ELSE 0 END) /
                    ABS(NULLIF(SUM(CASE WHEN realized_pl <= 0 THEN realized_pl ELSE 0 END), 0)))::numeric, 2) AS profit_factor,
                ROUND((AVG(CASE WHEN realized_pl > 0 THEN realized_pl END) /
                    ABS(NULLIF(AVG(CASE WHEN realized_pl <= 0 THEN realized_pl END), 0)))::numeric, 2) AS payoff_ratio,
                COUNT(*) FILTER (WHERE r_multiple >= 1) AS trades_above_1r,
                COUNT(*) FILTER (WHERE r_multiple >= 2) AS trades_above_2r,
                COUNT(*) FILTER (WHERE r_multiple >= 5) AS trades_above_5r,
                ROUND(MAX(realized_pl_pct)::numeric, 2) AS best_move_pct,
                ROUND(MIN(realized_pl_pct)::numeric, 2) AS worst_move_pct
            FROM all_trades
            """,
            params,
        )
        stats = cur.fetchone() or {}

        cur.execute(
            f"""
            WITH all_trades AS (
                SELECT fy, month_label, symbol, buy_value, realized_pl, realized_pl_pct
                FROM (
                    {_historical_trade_union_sql()}
                ) combined
                {trade_filter}
            )
            SELECT fy, month_label, symbol, buy_value, realized_pl, realized_pl_pct
            FROM all_trades
            ORDER BY realized_pl DESC NULLS LAST
            LIMIT 5
            """,
            params,
        )
        top_winners = [to_jsonable(row) for row in cur.fetchall()]

        cur.execute(
            f"""
            WITH all_trades AS (
                SELECT fy, month_label, symbol, buy_value, realized_pl, realized_pl_pct
                FROM (
                    {_historical_trade_union_sql()}
                ) combined
                {trade_filter}
            )
            SELECT fy, month_label, symbol, buy_value, realized_pl, realized_pl_pct
            FROM all_trades
            ORDER BY realized_pl ASC NULLS LAST
            LIMIT 5
            """,
            params,
        )
        top_losers = [to_jsonable(row) for row in cur.fetchall()]

        cur.execute(
            f"""
            WITH winners AS (
                SELECT realized_pl
                FROM (
                    SELECT realized_pl
                    FROM (
                        {_historical_trade_union_sql()}
                    ) combined
                    {trade_filter}
                ) filtered
                WHERE realized_pl > 0
            )
            SELECT ROUND((SUM(realized_pl) / NULLIF((SELECT SUM(realized_pl) FROM winners), 0) * 100)::numeric, 1) AS pct
            FROM (
                SELECT realized_pl
                FROM winners
                ORDER BY realized_pl DESC
                LIMIT 5
            ) top_winners
            """,
            params,
        )
        top5_concentration = float((cur.fetchone() or {}).get("pct") or 0)

        cur.execute(
            f"""
            WITH ranked AS (
                SELECT realized_pl, ROW_NUMBER() OVER (ORDER BY realized_pl DESC) AS rn
                FROM (
                    SELECT realized_pl
                    FROM (
                        {_historical_trade_union_sql()}
                    ) combined
                    {trade_filter}
                ) filtered
            )
            SELECT ROUND(SUM(realized_pl)::numeric) AS total_without_top3
            FROM ranked
            WHERE rn > 3
            """,
            params,
        )
        pl_without_top3 = round(float((cur.fetchone() or {}).get("total_without_top3") or 0))

        monthly_rows: list[dict[str, Any]] = []
        if fy == "all":
            cur.execute(
                """
                SELECT fy, month_label, month_order, after_charges, win_rate, approx_trades
                FROM (
                    SELECT '2020-21' AS fy, month_label, month_order, after_charges::real AS after_charges,
                           win_rate::real AS win_rate, approx_trades
                    FROM legacy_monthly_fy2021
                    UNION ALL
                    SELECT '2021-22', month_label, month_order + 100, after_charges::real,
                           win_rate::real, approx_trades
                    FROM legacy_monthly_fy2122
                    UNION ALL
                    SELECT '2022-23', month_label, month_order + 100, after_charges::real,
                           win_rate::real, approx_trades
                    FROM legacy_monthly_fy2223
                    UNION ALL
                    SELECT '2023-24', month_label, month_order + 200, after_charges::real,
                           win_rate::real, approx_trades
                    FROM legacy_monthly_fy2324
                    UNION ALL
                    SELECT '2024-25', month_label, month_order + 300, after_charges::real,
                           win_rate::real, approx_trades
                    FROM legacy_monthly_fy2425
                    UNION ALL
                    SELECT '2025-26', month_label, month_order + 400, after_charges::real,
                           win_rate::real, approx_trades
                    FROM legacy_monthly_summary
                ) monthly
                ORDER BY month_order
                """
            )
            monthly_rows = [dict(row) for row in cur.fetchall()]
        elif fy == "2026-27":
            cur.execute(
                """
                SELECT
                    %s AS fy,
                    month_label,
                    ROW_NUMBER() OVER (ORDER BY MIN(trade_date)) AS month_order,
                    ROUND((SUM(realized_pl::real) / 50000000.0 * 100)::numeric, 2) AS after_charges,
                    ROUND(AVG(CASE WHEN is_winner THEN 1.0 ELSE 0 END)::numeric * 100, 2) AS win_rate,
                    COUNT(*) AS approx_trades
                FROM journal_trades_computed
                GROUP BY month_label, month
                ORDER BY month_order
                """,
                (fy,),
            )
            monthly_rows = [dict(row) for row in cur.fetchall()]
        else:
            monthly_table = FY_TABLES.get(fy, FY_TABLES["2025-26"]).get("monthly")
            if monthly_table:
                cur.execute(
                    f"""
                    SELECT %s AS fy, month_label, month_order, after_charges::real AS after_charges,
                           win_rate::real AS win_rate, approx_trades
                    FROM {monthly_table}
                    ORDER BY month_order
                    """,
                    (fy,),
                )
                monthly_rows = [dict(row) for row in cur.fetchall()]

        best_month = max(monthly_rows, key=lambda row: float(row.get("after_charges") or 0)) if monthly_rows else None
        worst_month = min(monthly_rows, key=lambda row: float(row.get("after_charges") or 0)) if monthly_rows else None

        total_pl = round(float(stats.get("total_pl") or 0))
        return_pct = round((total_pl / base_capital) * 100, 2) if base_capital else 0
        return {
            "type": "analytics_overview",
            "fy": fy,
            "summary": {
                "total_trades": int(stats.get("total_trades") or 0),
                "wins": int(stats.get("wins") or 0),
                "losses": int(stats.get("losses") or 0),
                "win_rate": float(stats.get("win_rate") or 0),
                "total_pl": total_pl,
                "return_pct": return_pct,
                "avg_winner_pct": float(stats.get("avg_winner_pct") or 0),
                "avg_loser_pct": float(stats.get("avg_loser_pct") or 0),
                "avg_winner_r": float(stats.get("avg_winner_r") or 0),
                "avg_loser_r": float(stats.get("avg_loser_r") or 0),
                "expectancy_r": float(stats.get("expectancy_r") or 0),
                "profit_factor": float(stats.get("profit_factor") or 0),
                "payoff_ratio": float(stats.get("payoff_ratio") or 0),
                "trades_above_1r": int(stats.get("trades_above_1r") or 0),
                "trades_above_2r": int(stats.get("trades_above_2r") or 0),
                "trades_above_5r": int(stats.get("trades_above_5r") or 0),
                "best_move_pct": float(stats.get("best_move_pct") or 0),
                "worst_move_pct": float(stats.get("worst_move_pct") or 0),
                "top5_concentration_pct": top5_concentration,
                "pl_without_top3": pl_without_top3,
            },
            "best_trade": top_winners[0] if top_winners else None,
            "worst_trade": top_losers[0] if top_losers else None,
            "best_month": to_jsonable(best_month) if best_month else None,
            "worst_month": to_jsonable(worst_month) if worst_month else None,
            "top_winners": top_winners,
            "top_losers": top_losers,
        }
    finally:
        conn.close()


def read_drawdown_analysis(args: dict):
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        fy_config = [
            {"fy": "2022-23", "label": "FY 22-23", "start_capital": 16187147, "monthly_table": "legacy_monthly_fy2223"},
            {"fy": "2023-24", "label": "FY 23-24", "start_capital": 18028240, "monthly_table": "legacy_monthly_fy2324"},
            {"fy": "2024-25", "label": "FY 24-25", "start_capital": 28000000, "monthly_table": "legacy_monthly_fy2425"},
            {"fy": "2025-26", "label": "FY 25-26", "start_capital": 50000000, "monthly_table": "legacy_monthly_summary"},
        ]
        month_dates = ["04-30", "05-31", "06-30", "07-31", "08-31", "09-30", "10-31", "11-30", "12-31", "01-31", "02-28", "03-31"]

        timeline = []
        for config in fy_config:
            base = float(config["start_capital"])
            start_year = int(config["fy"].split("-")[0])
            cur.execute(
                f"""
                SELECT month_label, month_order, after_charges, net_pf_impact
                FROM {config['monthly_table']}
                ORDER BY month_order
                """
            )
            months = cur.fetchall()
            running = base
            timeline.append(
                {
                    "date": f"{start_year}-04-01",
                    "month_label": f"Start {config['label']}",
                    "fy": config["fy"],
                    "pf_value": round(base),
                    "month_pct": 0,
                    "month_pl": 0,
                    "is_start": True,
                }
            )
            for month in months:
                pct = float(month.get("after_charges") or 0)
                month_pl = round((pct / 100) * running)
                running += month_pl
                order = int(month.get("month_order") or 1)
                year = start_year if order <= 9 else start_year + 1
                timeline.append(
                    {
                        "date": f"{year}-{month_dates[order - 1]}",
                        "month_label": month.get("month_label"),
                        "fy": config["fy"],
                        "pf_value": round(running),
                        "month_pct": round(pct, 2),
                        "month_pl": month_pl,
                    }
                )

        peak_value = 0
        for index, point in enumerate(timeline):
            if point["pf_value"] > peak_value:
                peak_value = point["pf_value"]
            dd_pct = round(((point["pf_value"] - peak_value) / peak_value) * 100, 2) if peak_value else 0
            timeline[index]["peak_value"] = peak_value
            timeline[index]["dd_pct"] = dd_pct
            timeline[index]["dd_amount"] = round(point["pf_value"] - peak_value)

        cur.execute(
            """
            WITH monthly_close AS (
                SELECT date_trunc('month', date) AS month,
                       (ARRAY_AGG(close ORDER BY date DESC))[1] AS close_val
                FROM candles_indices
                WHERE symbol = 'NIFTY SMALLCAP 100' AND date >= '2022-04-01'
                GROUP BY date_trunc('month', date)
            )
            SELECT month::date, close_val,
                   ((close_val / LAG(close_val) OVER (ORDER BY month)) - 1)::numeric * 100 AS pct
            FROM monthly_close
            ORDER BY month
            """
        )
        market_map = {}
        for row in cur.fetchall():
            market_map[str(row["month"])[:7]] = round(float(row["pct"]), 2) if row["pct"] is not None else None

        for index, point in enumerate(timeline):
            market_pct = market_map.get(point["date"][:7])
            timeline[index]["sc100_pct"] = market_pct
            timeline[index]["relative_pct"] = round(point["month_pct"] - market_pct, 2) if market_pct is not None else None

        dd_periods = []
        in_drawdown = False
        start_idx = None
        trough_idx = None
        trough_pct = 0
        for index, point in enumerate(timeline):
            if point["dd_pct"] < 0:
                if not in_drawdown:
                    in_drawdown = True
                    start_idx = index
                    trough_idx = index
                    trough_pct = point["dd_pct"]
                elif point["dd_pct"] < trough_pct:
                    trough_idx = index
                    trough_pct = point["dd_pct"]
            elif in_drawdown and start_idx is not None and trough_idx is not None:
                dd_periods.append(
                    {
                        "start_month": timeline[start_idx]["month_label"],
                        "trough_month": timeline[trough_idx]["month_label"],
                        "recovery_month": point["month_label"],
                        "max_dd_pct": round(trough_pct, 2),
                        "max_dd_amount": timeline[trough_idx]["dd_amount"],
                        "dd_months": trough_idx - start_idx + 1,
                        "recovery_months": index - trough_idx,
                        "total_months": index - start_idx,
                        "fy": timeline[start_idx]["fy"],
                    }
                )
                in_drawdown = False
                start_idx = None
                trough_idx = None
        if in_drawdown and start_idx is not None and trough_idx is not None:
            dd_periods.append(
                {
                    "start_month": timeline[start_idx]["month_label"],
                    "trough_month": timeline[trough_idx]["month_label"],
                    "recovery_month": None,
                    "max_dd_pct": round(trough_pct, 2),
                    "max_dd_amount": timeline[trough_idx]["dd_amount"],
                    "dd_months": trough_idx - start_idx + 1,
                    "recovery_months": None,
                    "total_months": len(timeline) - start_idx,
                    "fy": timeline[start_idx]["fy"],
                    "ongoing": True,
                }
            )

        data_months = [point for point in timeline if not point.get("is_start")]
        negative_months = [point for point in data_months if point["month_pct"] < 0]
        positive_months = [point for point in data_months if point["month_pct"] >= 0]
        max_dd_point = min(data_months, key=lambda point: point.get("dd_pct", 0), default=None)
        max_negative_streak = 0
        current_negative = 0
        for point in data_months:
            if point["month_pct"] < 0:
                current_negative += 1
                max_negative_streak = max(max_negative_streak, current_negative)
            else:
                current_negative = 0

        underperformance = [
            {
                "month": point["month_label"],
                "you": point["month_pct"],
                "market": point.get("sc100_pct"),
            }
            for point in data_months
            if point["month_pct"] < 0 and (point.get("sc100_pct") or 0) > 0
        ]
        outperformance = [
            {
                "month": point["month_label"],
                "you": point["month_pct"],
                "market": point.get("sc100_pct"),
            }
            for point in data_months
            if point["month_pct"] > 0 and (point.get("sc100_pct") or 0) < 0
        ]

        avg_recovery = round(
            sum(period.get("recovery_months") or 0 for period in dd_periods if period.get("recovery_months") is not None)
            / max(len([period for period in dd_periods if period.get("recovery_months") is not None]), 1),
            1,
        )
        return {
            "type": "drawdown_analysis",
            "summary": {
                "total_months": len(data_months),
                "negative_months": len(negative_months),
                "positive_months": len(positive_months),
                "max_dd_pct": round(float(max_dd_point.get("dd_pct") or 0), 2) if max_dd_point else 0,
                "max_dd_month": max_dd_point.get("month_label") if max_dd_point else None,
                "max_dd_amount": round(float(max_dd_point.get("dd_amount") or 0)) if max_dd_point else 0,
                "max_consecutive_negative": max_negative_streak,
                "avg_recovery_months": avg_recovery,
                "dd_periods_count": len(dd_periods),
                "underperformance_months": len(underperformance),
                "outperformance_months": len(outperformance),
            },
            "top_periods": sorted(dd_periods, key=lambda period: period.get("max_dd_pct") or 0)[:5],
            "underperformance": underperformance[:5],
            "outperformance": outperformance[:5],
            "timeline": [to_jsonable(point) for point in data_months[-18:]],
        }
    finally:
        conn.close()


def read_trade_history_snapshot(args: dict):
    fy = args.get("fy") or _parse_fy_from_text(args.get("message")) or "2025-26"
    limit = max(1, min(int(args.get("limit") or 12), 40))
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        params: list[Any] = []
        fy_filter = ""
        if fy != "all":
            fy_filter = "WHERE fy = %s"
            params.append(fy)
        cur.execute(
            f"""
            WITH all_trades AS (
                SELECT fy, fy_sort, sort_id, month_label, symbol, quantity, buy_value, sell_value,
                       realized_pl, realized_pl_pct, impact_on_pf, is_winner
                FROM (
                    {_historical_trade_union_sql()}
                ) combined
                {fy_filter}
            )
            SELECT fy, month_label, symbol, quantity, buy_value, sell_value,
                   realized_pl, realized_pl_pct, impact_on_pf, is_winner
            FROM all_trades
            ORDER BY fy_sort DESC, sort_id DESC
            LIMIT %s
            """,
            params + [limit],
        )
        cards = [to_jsonable(row) for row in cur.fetchall()]

        cur.execute(
            f"""
            WITH all_trades AS (
                SELECT fy, month_label, realized_pl
                FROM (
                    {_historical_trade_union_sql()}
                ) combined
                {fy_filter}
            )
            SELECT COUNT(*) AS total_trades, ROUND(SUM(realized_pl)::numeric) AS total_pl
            FROM all_trades
            """,
            params,
        )
        summary = cur.fetchone() or {}
        return {
            "type": "trade_history",
            "fy": fy,
            "count": int(summary.get("total_trades") or 0),
            "total_pl": round(float(summary.get("total_pl") or 0)),
            "cards": cards,
        }
    finally:
        conn.close()


def read_equity_curve_snapshot(args: dict):
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        fy_config = [
            {"fy": "2022-23", "label": "FY 2022-23", "start_capital": 16187147, "monthly_table": "legacy_monthly_fy2223"},
            {"fy": "2023-24", "label": "FY 2023-24", "start_capital": 18028240, "monthly_table": "legacy_monthly_fy2324"},
            {"fy": "2024-25", "label": "FY 2024-25", "start_capital": 28000000, "monthly_table": "legacy_monthly_fy2425"},
            {"fy": "2025-26", "label": "FY 2025-26", "start_capital": 50000000, "monthly_table": "legacy_monthly_summary"},
        ]

        amount_points = []
        fy_summaries = []
        final_cumm_pct = 0.0
        for index, config in enumerate(fy_config):
            base = float(config["start_capital"])
            cur.execute(
                f"""
                SELECT month_label, month_order, after_charges
                FROM {config['monthly_table']}
                ORDER BY month_order
                """
            )
            months = cur.fetchall()
            running_amount = base
            multiplier = 1.0
            for month in months:
                pct = float(month.get("after_charges") or 0)
                month_pl = round((pct / 100) * running_amount)
                running_amount += month_pl
                multiplier *= (1 + pct / 100)
                amount_points.append(
                    {
                        "label": month.get("month_label"),
                        "fy": config["fy"],
                        "amount": round(running_amount),
                        "month_pct": round(pct, 2),
                    }
                )
            final_cumm_pct += round((multiplier - 1) * 100, 2)
            fy_summaries.append(
                {
                    "fy": config["fy"],
                    "label": config["label"],
                    "start_capital": round(base),
                    "end_capital": round(running_amount),
                    "net_return_pct": round((multiplier - 1) * 100, 2),
                }
            )

        return {
            "type": "equity_curve",
            "final_amount": amount_points[-1]["amount"] if amount_points else 0,
            "peak_amount": max((point["amount"] for point in amount_points), default=0),
            "final_cumm_pct": round(final_cumm_pct, 2),
            "fy_summaries": fy_summaries,
            "recent_points": amount_points[-12:],
        }
    finally:
        conn.close()


def read_live_monitor(args: dict):
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, stock_name, security_id, entry_price, risk_pct, quantity, stop_loss,
                   bucket_sold_pct, market_regime, ma_followed, ma_grade
            FROM positions
            WHERE status = 'active'
            """
        )
        positions = [dict(row) for row in cur.fetchall()]
        if not positions:
            return {"type": "empty", "message": "No active positions"}

        from services.market_data_service import bulk_refresh_positions

        live_data = bulk_refresh_positions(positions)
        cards = []
        for position in positions:
            security_id = str(position.get("security_id") or "")
            market = live_data.get(security_id, {})
            entry = float(position.get("entry_price") or 0)
            close = float(market.get("close") or 0)
            risk_pct = float(position.get("risk_pct") or 4)
            risk_per_share = entry * (risk_pct / 100) if entry else 0
            extension = round(((close - entry) / entry) * 100, 2) if entry else 0
            r_multiple = round((close - entry) / risk_per_share, 2) if risk_per_share else 0
            cards.append(
                {
                    "id": position["id"],
                    "stock_name": position["stock_name"],
                    "current_price": close,
                    "entry_price": entry,
                    "extension_pct": extension,
                    "r_multiple": r_multiple,
                    "ma5": market.get("ma5"),
                    "ma10": market.get("ma10"),
                    "ma20": market.get("ma20"),
                    "volume": market.get("volume"),
                    "unrealised_pnl": round((close - entry) * float(position.get("quantity") or 0)),
                    "regime": position.get("market_regime"),
                    "ma_grade": position.get("ma_grade"),
                }
            )
        return {"type": "live_monitor", "count": len(cards), "source": "supabase_websocket", "cards": to_jsonable(cards)}
    finally:
        conn.close()


def read_outlier_analysis(args: dict):
    fy = args.get("fy") or _parse_fy_from_text(args.get("message")) or "2025-26"
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        if fy == "all":
            table_sql = """(
                SELECT '2020-21' AS fy, symbol, month_label, realized_pl::real AS realized_pl, realized_pl_pct::real AS realized_pl_pct
                FROM legacy_trades_fy2021
                UNION ALL
                SELECT '2021-22', symbol, month_label, realized_pl::real, realized_pl_pct::real
                FROM legacy_trades_fy2122
                UNION ALL
                SELECT '2022-23', symbol, month_label, realized_pl::real, realized_pl_pct::real
                FROM legacy_trades_fy2223
                UNION ALL
                SELECT '2023-24', symbol, month_label, realized_pl::real, realized_pl_pct::real
                FROM legacy_trades_fy2324
                UNION ALL
                SELECT '2024-25', symbol, month_label, realized_pl::real, realized_pl_pct::real
                FROM legacy_trades_fy2425
                UNION ALL
                SELECT '2025-26', symbol, month_label, realized_pl::real, realized_pl_pct::real
                FROM legacy_trades
                UNION ALL
                SELECT '2026-27', symbol, month_label, realized_pl::real, realized_pl_pct::real
                FROM journal_trades_computed
            ) combined"""
        else:
            table_sql = FY_TABLES.get(fy, FY_TABLES["2025-26"])["trades"]

        cur.execute(
            f"""
            WITH all_trades AS (
                SELECT symbol, month_label, realized_pl AS pl, realized_pl_pct AS move_pct,
                       ROUND((realized_pl_pct / 3.0)::numeric, 2) AS r_multiple
                FROM {table_sql}
            )
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN pl > 0 THEN 1 ELSE 0 END) AS winners,
                SUM(CASE WHEN pl <= 0 THEN 1 ELSE 0 END) AS losers,
                ROUND(SUM(CASE WHEN pl > 0 THEN pl ELSE 0 END)::numeric) AS gross_profit,
                ROUND(ABS(SUM(CASE WHEN pl <= 0 THEN pl ELSE 0 END))::numeric) AS gross_loss,
                COUNT(*) FILTER (WHERE pl > 0 AND move_pct > 5) AS outlier_5p,
                COUNT(*) FILTER (WHERE pl > 0 AND r_multiple > 2) AS outlier_2r,
                ROUND(COALESCE(SUM(pl) FILTER (WHERE pl > 0 AND move_pct > 5), 0)::numeric) AS outlier_5p_pl
            FROM all_trades
            """
        )
        summary = cur.fetchone() or {}

        cur.execute(
            f"""
            WITH winners AS (
                SELECT symbol, month_label, realized_pl AS pl, realized_pl_pct AS move_pct,
                       ROUND((realized_pl_pct / 3.0)::numeric, 2) AS r_multiple
                FROM {table_sql}
                WHERE realized_pl > 0
            )
            SELECT symbol, month_label, ROUND(pl::numeric) AS pl,
                   ROUND(move_pct::numeric, 2) AS move_pct, r_multiple
            FROM winners
            ORDER BY pl DESC
            LIMIT 10
            """
        )
        top_winners = [to_jsonable(row) for row in cur.fetchall()]

        cur.execute(
            f"""
            WITH losers AS (
                SELECT symbol, month_label, realized_pl AS pl, ABS(realized_pl_pct) AS move_pct,
                       ROUND(ABS(realized_pl_pct / 3.0)::numeric, 2) AS r_multiple
                FROM {table_sql}
                WHERE realized_pl <= 0
            )
            SELECT symbol, month_label, ROUND(pl::numeric) AS pl,
                   ROUND(move_pct::numeric, 2) AS move_pct, r_multiple
            FROM losers
            ORDER BY pl ASC
            LIMIT 10
            """
        )
        top_losers = [to_jsonable(row) for row in cur.fetchall()]

        cur.execute(
            f"""
            WITH winners AS (
                SELECT realized_pl AS pl
                FROM {table_sql}
                WHERE realized_pl > 0
            )
            SELECT rn AS n, ROUND(SUM(pl) OVER (ORDER BY rn)::numeric) AS cum_pl
            FROM (
                SELECT pl, ROW_NUMBER() OVER (ORDER BY pl DESC) AS rn
                FROM winners
            ) ranked
            WHERE rn <= 10
            """
        )
        concentration = [to_jsonable(row) for row in cur.fetchall()]

        return {
            "type": "outlier_analysis",
            "fy": fy,
            "summary": {
                "total_trades": int(summary.get("total") or 0),
                "winners": int(summary.get("winners") or 0),
                "losers": int(summary.get("losers") or 0),
                "gross_profit": round(float(summary.get("gross_profit") or 0)),
                "gross_loss": round(float(summary.get("gross_loss") or 0)),
                "outlier_5p": int(summary.get("outlier_5p") or 0),
                "outlier_2r": int(summary.get("outlier_2r") or 0),
                "outlier_5p_pl": round(float(summary.get("outlier_5p_pl") or 0)),
            },
            "top_winners": top_winners,
            "top_losers": top_losers,
            "concentration": concentration,
        }
    finally:
        conn.close()



def read_explore_insights(args: dict):
    symbol = (args.get("symbol") or "").upper().strip()
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        if not symbol:
            stock = _resolve_stock_reference(cur, message=args.get("message"))
            symbol = stock.get("symbol") if stock else ""
        if not symbol:
            return {"type": "empty", "message": "No stock symbol provided"}
        cur.execute(
            """
            SELECT ai_insights, cache_date
            FROM explore_cache
            WHERE symbol = %s
            ORDER BY cache_date DESC
            LIMIT 1
            """,
            (symbol,),
        )
        row = cur.fetchone()
        if not row or not row.get("ai_insights"):
            return {"type": "empty", "message": f"No cached explore insights for {symbol}"}
        return {
            "type": "explore_insights",
            "symbol": symbol,
            "cache_date": to_jsonable(row.get("cache_date")),
            "insights": to_jsonable(row.get("ai_insights")),
        }
    finally:
        conn.close()


def read_regime_summary(args: dict):
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute("SELECT regime, note, updated_at FROM market_regime_history ORDER BY updated_at DESC LIMIT 1")
        regime = cur.fetchone() or {}
        cur.execute("SELECT sectors, note, updated_at FROM leading_sectors ORDER BY updated_at DESC LIMIT 1")
        leading = cur.fetchone() or {}
        # user_settings is per-user; the old WHERE id = 1 always returned
        # the oldest user's row. Scoped to the authenticated caller now.
        settings = {}
        uid = _uid()
        if uid:
            cur.execute(
                "SELECT display_name, base_capital FROM user_settings WHERE user_id = %s",
                (uid,),
            )
            settings = cur.fetchone() or {}
        return {
            "type": "regime_summary",
            "regime": regime.get("regime") or "bull",
            "note": regime.get("note") or "",
            "updated_at": regime.get("updated_at").isoformat() if regime.get("updated_at") else None,
            "leading_sectors": to_jsonable(leading.get("sectors") or []),
            "leading_note": leading.get("note") or "",
            "display_name": settings.get("display_name") or "Rohit",
            "base_capital": float(settings.get("base_capital") or DEFAULT_BASE_CAPITAL),
        }
    finally:
        conn.close()


def read_saved_scanners(args: dict):
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        # saved_scanners is per-user (user_id UUID column). Previously
        # unfiltered — would return every user's scanner definitions,
        # filters and run history to any caller who hit this snapshot.
        uid = _uid()
        if not uid:
            return {"type": "saved_scanners_snapshot", "count": 0, "cards": []}
        cur.execute(
            """
            SELECT id, name, description, pinned, last_run_at, last_run_count, filters
            FROM saved_scanners
            WHERE user_id = %s
            ORDER BY pinned DESC, last_run_at DESC NULLS LAST, created_at DESC
            """,
            (uid,),
        )
        rows = cur.fetchall()
        return {"type": "saved_scanners_snapshot", "count": len(rows), "cards": [to_jsonable(row) for row in rows]}
    finally:
        conn.close()


def read_sector_snapshot(args: dict):
    days = max(2, min(int(args.get("days") or 20), 120))
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH ordered AS (
                SELECT
                    symbol,
                    close,
                    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM candles_indices
                WHERE symbol ILIKE 'NIFTY%%'
            ),
            latest AS (
                SELECT symbol, close AS latest_close FROM ordered WHERE rn = 1
            ),
            anchor AS (
                SELECT symbol, close AS anchor_close FROM ordered WHERE rn = %s
            )
            SELECT
                latest.symbol,
                latest.latest_close,
                anchor.anchor_close,
                ROUND(((latest.latest_close - anchor.anchor_close) / NULLIF(anchor.anchor_close, 0) * 100)::numeric, 2) AS pct_change
            FROM latest
            JOIN anchor USING (symbol)
            ORDER BY pct_change DESC NULLS LAST
            LIMIT 20
            """,
            (days,),
        )
        rows = cur.fetchall()
        cards = [to_jsonable(row) for row in rows]
        return {"type": "sector_snapshot", "days": days, "cards": cards}
    finally:
        conn.close()


def read_index_constituents_snapshot(args: dict):
    index_symbol = (args.get("index_symbol") or "").strip()
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        resolved_index = _resolve_index_symbol(cur, index_hint=index_symbol, message=args.get("message"))
        if not resolved_index:
            return {"type": "empty", "message": "No matching index or sector found"}
        cur.execute(
            """
            WITH resolved_constituents AS (
                SELECT
                    ic.stock_symbol,
                    ic.stock_name,
                    ic.sector,
                    ic.weightage,
                    COALESCE(
                        su_symbol.security_id,
                        su_isin.security_id,
                        NULLIF(BTRIM(ic.security_id), ''),
                        NULL
                    ) AS resolved_security_id
                FROM index_constituents ic
                LEFT JOIN LATERAL (
                    SELECT su.security_id
                    FROM stock_universe su
                    WHERE su.symbol = ic.stock_symbol
                      AND su.is_active = true
                    LIMIT 1
                ) su_symbol ON TRUE
                LEFT JOIN LATERAL (
                    SELECT su.security_id
                    FROM stock_universe su
                    WHERE ic.isin IS NOT NULL
                      AND BTRIM(ic.isin) <> ''
                      AND su.isin = ic.isin
                      AND su.is_active = true
                    LIMIT 1
                ) su_isin ON TRUE
                WHERE ic.index_symbol = %s
            ),
            target_sids AS (
                SELECT DISTINCT resolved_security_id AS security_id
                FROM resolved_constituents
                WHERE resolved_security_id IS NOT NULL
                  AND resolved_security_id <> 'UNMAPPED'
            ),
            latest_2 AS (
                SELECT cd.security_id, cd.close, cd.date,
                       ROW_NUMBER() OVER (PARTITION BY cd.security_id ORDER BY cd.date DESC) as rn
                FROM candles_daily cd
                INNER JOIN target_sids t ON cd.security_id = t.security_id
                WHERE cd.date >= CURRENT_DATE - 10
            ),
            latest_prices AS (
                SELECT security_id,
                    MAX(CASE WHEN rn = 1 THEN close END) as close,
                    MAX(CASE WHEN rn = 1 THEN date END) as date,
                    ROUND(((MAX(CASE WHEN rn = 1 THEN close END) - MAX(CASE WHEN rn = 2 THEN close END))
                        / NULLIF(MAX(CASE WHEN rn = 2 THEN close END), 0) * 100)::numeric, 2) as day_change
                FROM latest_2 WHERE rn <= 2
                GROUP BY security_id
            ),
            week_ago AS (
                SELECT DISTINCT ON (cd.security_id)
                    cd.security_id,
                    cd.close AS week_ago_close
                FROM candles_daily cd
                INNER JOIN target_sids t ON cd.security_id = t.security_id
                WHERE cd.date >= CURRENT_DATE - 14
                  AND cd.date <= CURRENT_DATE - 4
                ORDER BY cd.security_id, cd.date DESC
            ),
            ma_data AS (
                SELECT security_id,
                    AVG(close) FILTER (WHERE rn <= 5) AS ma5,
                    AVG(close) FILTER (WHERE rn <= 10) AS ma10,
                    AVG(close) FILTER (WHERE rn <= 20) AS ma20,
                    AVG(close) FILTER (WHERE rn <= 50) AS ma50
                FROM (
                    SELECT cd.security_id, cd.close,
                        ROW_NUMBER() OVER (PARTITION BY cd.security_id ORDER BY cd.date DESC) AS rn
                    FROM candles_daily cd
                    INNER JOIN target_sids t ON cd.security_id = t.security_id
                    WHERE cd.date >= CURRENT_DATE - 90
                ) sub
                WHERE rn <= 50
                GROUP BY security_id
            )
            SELECT
                rc.stock_symbol,
                rc.stock_name,
                rc.sector,
                rc.weightage,
                rc.resolved_security_id AS security_id,
                lp.close AS cmp,
                lp.day_change,
                lp.date AS price_date,
                ROUND(((lp.close - wa.week_ago_close) / NULLIF(wa.week_ago_close, 0) * 100)::numeric, 2) AS week_change,
                md.ma5,
                md.ma10,
                md.ma20,
                md.ma50,
                CASE
                    WHEN lp.close > md.ma5 AND lp.close > md.ma10 AND lp.close > md.ma20 THEN 'uptrend'
                    WHEN lp.close < md.ma5 AND lp.close < md.ma10 AND lp.close < md.ma20 THEN 'downtrend'
                    ELSE 'mixed'
                END AS ma_trend
            FROM resolved_constituents rc
            LEFT JOIN latest_prices lp ON rc.resolved_security_id = lp.security_id
            LEFT JOIN week_ago wa ON rc.resolved_security_id = wa.security_id
            LEFT JOIN ma_data md ON rc.resolved_security_id = md.security_id
            WHERE rc.resolved_security_id IS NOT NULL
              AND rc.resolved_security_id <> 'UNMAPPED'
            ORDER BY rc.weightage DESC NULLS LAST, rc.stock_symbol
            """,
            (resolved_index,),
        )
        rows = [to_jsonable(row) for row in cur.fetchall()]
        if not rows:
            return {"type": "empty", "message": f"No constituent data found for {resolved_index}"}
        uptrend_count = sum(1 for row in rows if row.get("ma_trend") == "uptrend")
        downtrend_count = sum(1 for row in rows if row.get("ma_trend") == "downtrend")
        mixed_count = len(rows) - uptrend_count - downtrend_count
        ranked = sorted(rows, key=lambda item: float(item.get("week_change") or 0), reverse=True)
        return {
            "type": "index_constituents_snapshot",
            "index_symbol": resolved_index,
            "count": len(rows),
            "summary": {
                "uptrend_count": uptrend_count,
                "downtrend_count": downtrend_count,
                "mixed_count": mixed_count,
                "avg_week_change": round(sum(float(row.get("week_change") or 0) for row in rows) / len(rows), 2) if rows else 0,
            },
            "leaders": ranked[:5],
            "laggards": list(reversed(ranked[-5:])),
            "cards": ranked[:12],
        }
    finally:
        conn.close()



def read_scoring_snapshot(args: dict):
    limit = max(1, min(int(args.get("limit") or 12), 25))
    min_score = args.get("min_score")
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        if min_score is not None:
            cur.execute(
                """
                SELECT symbol, stock_name, sector, final_score, rating, setup_type, traded, timestamp
                FROM submissions
                WHERE final_score >= %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (float(min_score), limit),
            )
        else:
            cur.execute(
                """
                SELECT symbol, stock_name, sector, final_score, rating, setup_type, traded, timestamp
                FROM submissions
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (limit,),
            )
        rows = cur.fetchall()
        return {"type": "scoring_snapshot", "count": len(rows), "cards": [to_jsonable(row) for row in rows]}
    finally:
        conn.close()


def read_screener_snapshot(args: dict):
    limit = max(1, min(int(args.get("limit") or 20), 40))
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT scan_date
            FROM screener_stocks
            ORDER BY scan_date DESC
            LIMIT 1
            """
        )
        latest = cur.fetchone()
        if not latest:
            return {"type": "empty", "message": "No screener data available"}
        cur.execute(
            """
            SELECT symbol, name, sector, pchange, scan_date
            FROM screener_stocks
            WHERE scan_date = %s
            ORDER BY pchange DESC NULLS LAST
            LIMIT %s
            """,
            (latest["scan_date"], limit),
        )
        rows = cur.fetchall()
        return {
            "type": "screener_snapshot",
            "scan_date": latest["scan_date"].isoformat() if latest.get("scan_date") else None,
            "cards": [to_jsonable(row) for row in rows],
        }
    finally:
        conn.close()


def read_explore_stock(args: dict):
    symbol = (args.get("symbol") or "").strip().upper()
    if not symbol:
        symbol = ""
    conn = _db()
    if not conn:
        return {"type": "empty", "message": "Database unavailable"}
    try:
        cur = conn.cursor()
        stock = _resolve_stock_reference(cur, symbol_hint=symbol, message=args.get("message"))
        if not stock:
            missing_ref = symbol or compact_whitespace(args.get("message") or "") or "that stock"
            return {"type": "empty", "message": f"No stock found for {missing_ref}"}
        cur.execute(
            """
            WITH arrayed AS (
                SELECT
                    ARRAY_AGG(close ORDER BY date DESC) AS cl,
                    ARRAY_AGG(high ORDER BY date DESC) AS hi,
                    ARRAY_AGG(low ORDER BY date DESC) AS lo,
                    ARRAY_AGG(volume ORDER BY date DESC) AS vol,
                    ARRAY_AGG(volume * close ORDER BY date DESC) AS turnover,
                    ARRAY_AGG(date ORDER BY date DESC) AS dates,
                    ARRAY_AGG(ABS(high - low) / NULLIF(close, 0) * 100 ORDER BY date DESC) AS ranges
                FROM candles_daily
                WHERE security_id = %s AND date >= CURRENT_DATE - 400
            ),
            sc100 AS (
                SELECT ARRAY_AGG(close ORDER BY date DESC) AS sc_cl
                FROM candles_indices
                WHERE symbol = 'NIFTY SMALLCAP 100' AND date >= CURRENT_DATE - 400
            )
            SELECT
                a.cl[1] AS cmp,
                a.cl[2] AS prev_close,
                a.dates[1] AS last_date,
                (SELECT MAX(v) FROM UNNEST(a.hi[1:252]) v) AS high_52w,
                (SELECT MIN(v) FROM UNNEST(a.lo[1:252]) v) AS low_52w,
                (SELECT AVG(v) FROM UNNEST(a.cl[1:5]) v) AS ma5_val,
                (SELECT AVG(v) FROM UNNEST(a.cl[1:10]) v) AS ma10_val,
                (SELECT AVG(v) FROM UNNEST(a.cl[1:20]) v) AS ma20_val,
                (SELECT AVG(v) FROM UNNEST(a.cl[1:50]) v) AS ma50_val,
                (SELECT AVG(v) FROM UNNEST(a.cl[1:200]) v) AS ma200_val,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:5]) v), 0) AS above_ma5,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:10]) v), 0) AS above_ma10,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:20]) v), 0) AS above_ma20,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:50]) v), 0) AS above_ma50,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:200]) v), 0) AS above_ma200,
                ROUND(((SELECT AVG(v) FROM UNNEST(a.turnover[1:20]) v) / 10000000.0)::numeric, 1) AS liq_cr,
                ROUND(((SELECT AVG(v) FROM UNNEST(a.ranges[1:20]) v))::numeric, 2) AS adr,
                CASE WHEN a.cl[2] > 0 THEN ROUND(((a.cl[1] - a.cl[2]) / a.cl[2] * 100)::numeric, 2) END AS day_change,
                CASE WHEN a.cl[6] > 0 AND sc.sc_cl[6] > 0 THEN
                    ROUND((((a.cl[1] / a.cl[6] - 1) - (sc.sc_cl[1] / sc.sc_cl[6] - 1)) * 100)::numeric, 2)
                END AS rs_1w,
                CASE WHEN a.cl[64] > 0 AND sc.sc_cl[64] > 0 THEN
                    ROUND((((a.cl[1] / a.cl[64] - 1) - (sc.sc_cl[1] / sc.sc_cl[64] - 1)) * 100)::numeric, 2)
                END AS rs_3m,
                CASE WHEN a.cl[127] > 0 AND sc.sc_cl[127] > 0 THEN
                    ROUND((((a.cl[1] / a.cl[127] - 1) - (sc.sc_cl[1] / sc.sc_cl[127] - 1)) * 100)::numeric, 2)
                END AS rs_6m,
                array_length(a.cl, 1) AS total_candles
            FROM arrayed a CROSS JOIN sc100 sc
            """,
            (stock["security_id"],),
        )
        tech = cur.fetchone() or {}
        if not tech:
            return {"type": "empty", "message": f"No price data available for {stock['symbol']}"}
        for key, value in list(tech.items()):
            if value is not None and hasattr(value, "__float__"):
                tech[key] = float(value)
        if tech.get("last_date"):
            tech["last_date"] = str(tech["last_date"])

        cur.execute(
            """
            SELECT index_symbol
            FROM index_constituents
            WHERE stock_symbol = %s
              AND index_symbol NOT IN (
                  'NIFTY', 'NIFTY NEXT 50', 'NIFTY MID100 FREE',
                  'NIFTY SMALLCAP 100', 'NIFTY MICROCAP250',
                  'NIFTY 500', 'NIFTY MIDSMALLCAP 400'
              )
            LIMIT 1
            """,
            (stock["symbol"],),
        )
        sector_row = cur.fetchone()
        sector = sector_row["index_symbol"].replace("NIFTY ", "") if sector_row else None

        # Unified market cap from shares_outstanding × latest price
        from services.market_cap import get_market_cap
        market_cap_cr = get_market_cap(stock["symbol"], conn=conn) or 5000
        market_cap_label = (
            "Large Cap" if market_cap_cr >= 80000 else
            "Mid Cap" if market_cap_cr >= 12000 else
            "Small Cap" if market_cap_cr >= 5000 else
            "Micro Cap"
        )

        sector_health = None
        if sector_row:
            cur.execute(
                """
                WITH idx_arr AS (
                    SELECT
                        (ARRAY_AGG(close ORDER BY date DESC))[1] AS latest,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:5]) v) AS sma5,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:10]) v) AS sma10,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:20]) v) AS sma20,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:50]) v) AS sma50,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:200]) v) AS sma200
                    FROM candles_indices
                    WHERE symbol = %s AND date >= CURRENT_DATE - 400
                )
                SELECT latest,
                    latest > sma5 AS above5,
                    latest > sma10 AS above10,
                    latest > sma20 AS above20,
                    latest > sma50 AS above50,
                    latest > sma200 AS above200
                FROM idx_arr
                """,
                (sector_row["index_symbol"],),
            )
            health = cur.fetchone()
            if health:
                score = sum(1 for key in ["above5", "above10", "above20", "above50", "above200"] if health.get(key))
                sector_health = {
                    "score": score,
                    "above": {key: bool(health.get(key)) for key in ["above5", "above10", "above20", "above50", "above200"]},
                    "verdict": "Strong" if score >= 4 else "Healthy" if score >= 3 else "Mixed" if score >= 2 else "Weak",
                }

        cached_fundamentals = {}
        try:
            from services.market_data_service import get_yahoo_cached

            cached_fundamentals = get_yahoo_cached(stock["symbol"]) or {}
        except Exception:
            cached_fundamentals = {}

        cmp_value = float(tech.get("cmp") or 0)
        high_52w = float(tech.get("high_52w") or 0)
        low_52w = float(tech.get("low_52w") or 0)
        pct_from_high = round(((cmp_value - high_52w) / high_52w) * 100, 1) if high_52w else None
        pct_from_low = round(((cmp_value - low_52w) / low_52w) * 100, 1) if low_52w else None
        range_position = round(((cmp_value - low_52w) / (high_52w - low_52w)) * 100, 1) if high_52w and low_52w and high_52w != low_52w else None

        liq_cr = float(tech.get("liq_cr") or 0)
        liq_verdict = (
            "Institutional Grade" if liq_cr >= 50 else
            "Moderate" if liq_cr >= 10 else
            "Low - risky at current portfolio size"
        )
        mas_above = sum(1 for key in ["above_ma5", "above_ma10", "above_ma20", "above_ma50", "above_ma200"] if tech.get(key))
        ma_verdict = (
            "All Aligned" if mas_above == 5 else
            "Strong" if mas_above >= 4 else
            "Mixed" if mas_above >= 2 else
            "Weak"
        )

        return {
            "type": "explore_stock",
            "symbol": stock["symbol"],
            "company_name": stock["company_name"],
            "security_id": stock["security_id"],
            "cmp": cmp_value,
            "day_change": tech.get("day_change"),
            "last_date": tech.get("last_date"),
            "sector": cached_fundamentals.get("sector") or sector,
            "industry": cached_fundamentals.get("industry"),
            "market_cap_cr": cached_fundamentals.get("market_cap_cr") or market_cap_cr,
            "market_cap_label": market_cap_label,
            "mas": {
                "ma5": {"value": round(float(tech.get("ma5_val") or 0), 2), "above": bool(tech.get("above_ma5"))},
                "ma10": {"value": round(float(tech.get("ma10_val") or 0), 2), "above": bool(tech.get("above_ma10"))},
                "ma20": {"value": round(float(tech.get("ma20_val") or 0), 2), "above": bool(tech.get("above_ma20"))},
                "ma50": {"value": round(float(tech.get("ma50_val") or 0), 2), "above": bool(tech.get("above_ma50"))},
                "ma200": {"value": round(float(tech.get("ma200_val") or 0), 2), "above": bool(tech.get("above_ma200"))},
            },
            "mas_above_count": mas_above,
            "ma_verdict": ma_verdict,
            "high_52w": high_52w or None,
            "low_52w": low_52w or None,
            "pct_from_high": pct_from_high,
            "pct_from_low": pct_from_low,
            "range_position": range_position,
            "liquidity_cr": liq_cr,
            "liquidity_verdict": liq_verdict,
            "adr": tech.get("adr"),
            "rs": {
                "rs_1w": tech.get("rs_1w"),
                "rs_3m": tech.get("rs_3m"),
                "rs_6m": tech.get("rs_6m"),
            },
            "sector_health": sector_health,
            "cached_fundamentals": bool(cached_fundamentals),
        }
    finally:
        conn.close()


@dataclass(frozen=True)
class ReaderDefinition:
    name: str
    domain: str
    description: str
    input_schema: dict
    handler: Callable[[dict], dict]


READERS = {
    "read_portfolio_overview": ReaderDefinition(
        name="read_portfolio_overview",
        domain="portfolio",
        description="Canonical portfolio overview of active positions.",
        input_schema={"type": "object", "properties": {}},
        handler=read_portfolio_overview,
    ),
    "read_sell_actions": ReaderDefinition(
        name="read_sell_actions",
        domain="portfolio",
        description="Sell and exit decisions based on extension and 5MA status.",
        input_schema={"type": "object", "properties": {}},
        handler=read_sell_actions,
    ),
    "read_portfolio_risk": ReaderDefinition(
        name="read_portfolio_risk",
        domain="portfolio",
        description="Portfolio risk, stop-loss coverage, and downside concentration.",
        input_schema={"type": "object", "properties": {}},
        handler=read_portfolio_risk,
    ),
    "read_portfolio_rankings": ReaderDefinition(
        name="read_portfolio_rankings",
        domain="portfolio",
        description="Rank positions by R multiple, P&L, or extension.",
        input_schema={
            "type": "object",
            "properties": {"sort_by": {"type": "string", "enum": ["r", "pnl", "extension"]}},
        },
        handler=read_portfolio_rankings,
    ),
    "read_portfolio_trailing": ReaderDefinition(
        name="read_portfolio_trailing",
        domain="portfolio",
        description="5MA trailing and defensive status for active positions.",
        input_schema={"type": "object", "properties": {}},
        handler=read_portfolio_trailing,
    ),
    "read_position_detail": ReaderDefinition(
        name="read_position_detail",
        domain="portfolio",
        description="Single-position deep dive for a named stock.",
        input_schema={
            "type": "object",
            "properties": {"stock_name": {"type": "string"}},
            "required": ["stock_name"],
        },
        handler=read_position_detail,
    ),
    "read_journal_summary": ReaderDefinition(
        name="read_journal_summary",
        domain="journal",
        description="Historical win rate, total P&L, and best/worst trade summary.",
        input_schema={"type": "object", "properties": {}},
        handler=read_journal_summary,
    ),
    "read_journal_trade_book": ReaderDefinition(
        name="read_journal_trade_book",
        domain="journal",
        description="Current-year editable journal trades with calculated fields like gross P&L, R-multiple, holding days, and capital at risk.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        handler=read_journal_trade_book,
    ),
    "read_journal_settings_summary": ReaderDefinition(
        name="read_journal_settings_summary",
        domain="journal",
        description="Journal settings and yearly fund-month entries used by the editable journal pages.",
        input_schema={
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
            },
        },
        handler=read_journal_settings_summary,
    ),
    "read_trade_stats_period": ReaderDefinition(
        name="read_trade_stats_period",
        domain="journal",
        description="Historical trading stats for a rolling period like 1 month, 6 months, 1 year, 2 years, or full history.",
        input_schema={
            "type": "object",
            "properties": {
                "period": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        handler=read_trade_stats_period,
    ),
    "read_streak_analysis": ReaderDefinition(
        name="read_streak_analysis",
        domain="journal",
        description="Longest winning streak, longest losing streak, and current streak from historical trades.",
        input_schema={
            "type": "object",
            "properties": {
                "fy": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        handler=read_streak_analysis,
    ),
    "read_trade_highlights": ReaderDefinition(
        name="read_trade_highlights",
        domain="journal",
        description="Best trade over the recent years and biggest trade for a chosen financial year.",
        input_schema={
            "type": "object",
            "properties": {
                "years": {"type": "integer"},
                "fy": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        handler=read_trade_highlights,
    ),
    "read_trade_r_extremes": ReaderDefinition(
        name="read_trade_r_extremes",
        domain="journal",
        description="Highest and lowest R-multiple trades across the full historical dataset or within a chosen FY.",
        input_schema={
            "type": "object",
            "properties": {
                "fy": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        handler=read_trade_r_extremes,
    ),
    "read_top_trade_winners": ReaderDefinition(
        name="read_top_trade_winners",
        domain="journal",
        description="Top historical winning trades ranked by percentage gain or absolute P&L.",
        input_schema={
            "type": "object",
            "properties": {
                "fy": {"type": "string"},
                "limit": {"type": "integer"},
                "sort_by": {"type": "string", "enum": ["pct", "pl"]},
                "message": {"type": "string"},
            },
        },
        handler=read_top_trade_winners,
    ),
    "read_trade_history_snapshot": ReaderDefinition(
        name="read_trade_history_snapshot",
        domain="journal",
        description="FY-aware trade history preview from the same legacy and current-year trade sources used on the history page.",
        input_schema={
            "type": "object",
            "properties": {
                "fy": {"type": "string"},
                "limit": {"type": "integer"},
                "message": {"type": "string"},
            },
        },
        handler=read_trade_history_snapshot,
    ),
    "read_fy_summary": ReaderDefinition(
        name="read_fy_summary",
        domain="analytics",
        description="FY-level summary built from the historical legacy trade tables.",
        input_schema={
            "type": "object",
            "properties": {"fy": {"type": "string", "description": "Example: 2025-26"}},
            "required": ["fy"],
        },
        handler=read_fy_summary,
    ),
    "read_analytics_overview": ReaderDefinition(
        name="read_analytics_overview",
        domain="analytics",
        description="Full analytics summary including expectancy, profit factor, payoff ratio, best and worst trade, return, and monthly highlights.",
        input_schema={
            "type": "object",
            "properties": {
                "fy": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        handler=read_analytics_overview,
    ),
    "read_monthly_summary": ReaderDefinition(
        name="read_monthly_summary",
        domain="analytics",
        description="Monthly P&L and win-rate breakdown for a financial year.",
        input_schema={
            "type": "object",
            "properties": {"fy": {"type": "string", "description": "Example: 2025-26"}},
            "required": ["fy"],
        },
        handler=read_monthly_summary,
    ),
    "read_drawdown_analysis": ReaderDefinition(
        name="read_drawdown_analysis",
        domain="analytics",
        description="Cross-FY drawdown story including max drawdown, recovery, negative-month streaks, and market context.",
        input_schema={"type": "object", "properties": {}},
        handler=read_drawdown_analysis,
    ),
    "read_equity_curve_snapshot": ReaderDefinition(
        name="read_equity_curve_snapshot",
        domain="analytics",
        description="Long-term multi-FY equity curve summary with FY-wise start, end, and compounded returns.",
        input_schema={"type": "object", "properties": {}},
        handler=read_equity_curve_snapshot,
    ),
    "read_outlier_analysis": ReaderDefinition(
        name="read_outlier_analysis",
        domain="analytics",
        description="Winner concentration, fat-tail outliers, and distribution of big winners and losers for a financial year or all history.",
        input_schema={
            "type": "object",
            "properties": {
                "fy": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        handler=read_outlier_analysis,
    ),
    "read_regime_summary": ReaderDefinition(
        name="read_regime_summary",
        domain="settings",
        description="Current market regime, leading sectors, and profile settings.",
        input_schema={"type": "object", "properties": {}},
        handler=read_regime_summary,
    ),
    "read_saved_scanners": ReaderDefinition(
        name="read_saved_scanners",
        domain="screener",
        description="Saved scanner presets and their last run metadata.",
        input_schema={"type": "object", "properties": {}},
        handler=read_saved_scanners,
    ),
    "read_sector_snapshot": ReaderDefinition(
        name="read_sector_snapshot",
        domain="sectoral",
        description="Sector index performance snapshot over a chosen lookback.",
        input_schema={"type": "object", "properties": {"days": {"type": "integer"}}},
        handler=read_sector_snapshot,
    ),
    "read_index_constituents_snapshot": ReaderDefinition(
        name="read_index_constituents_snapshot",
        domain="sectoral",
        description="Constituent stocks for an index or sector with price, weekly change, and MA-trend breadth.",
        input_schema={
            "type": "object",
            "properties": {
                "index_symbol": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        handler=read_index_constituents_snapshot,
    ),
    "read_live_monitor": ReaderDefinition(
        name="read_live_monitor",
        domain="portfolio",
        description="Live monitor snapshot with current price, extension, R-multiple, live moving averages, and unrealised P&L for active positions.",
        input_schema={"type": "object", "properties": {}},
        handler=read_live_monitor,
    ),
    "read_scoring_snapshot": ReaderDefinition(
        name="read_scoring_snapshot",
        domain="scoring",
        description="Recent scored setups from the scoring engine.",
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}, "min_score": {"type": "number"}}},
        handler=read_scoring_snapshot,
    ),
    "read_screener_snapshot": ReaderDefinition(
        name="read_screener_snapshot",
        domain="screener",
        description="Latest screener scan snapshot and top movers.",
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        handler=read_screener_snapshot,
    ),
    "read_explore_stock": ReaderDefinition(
        name="read_explore_stock",
        domain="explore",
        description="Single-stock deep dive using stock_universe and candles_daily.",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        handler=read_explore_stock,
    ),
    "read_explore_insights": ReaderDefinition(
        name="read_explore_insights",
        domain="explore",
        description="Cached AI-generated explore insights for a stock from the explore page.",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "message": {"type": "string"},
            },
        },
        handler=read_explore_insights,
    ),
}


def get_reader_tools():
    tools = []
    for definition in READERS.values():
        tools.append(
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
            }
        )
    return tools


def run_reader(name: str, args: dict | None = None):
    definition = READERS.get(name)
    if not definition:
        return {"type": "error", "error": f"Unknown catalog reader: {name}"}
    args = args or {}
    return to_jsonable(definition.handler(args))


def get_catalog_overview():
    domains = {}
    for definition in READERS.values():
        domains.setdefault(definition.domain, []).append(
            {"name": definition.name, "description": definition.description}
        )
    return {"domains": domains}


READER_HINTS = {
    "read_portfolio_overview": {
        "keywords": ("portfolio", "positions", "overview", "holdings", "open trades"),
        "page_contexts": {"position-manager", "valvo-ai", "dashboard", "valvo-ai-v2"},
    },
    "read_sell_actions": {
        "keywords": ("sell", "trim", "exit", "5ma break", "what should i sell"),
    },
    "read_portfolio_risk": {
        "keywords": ("risk", "exposure", "capital at risk", "stop loss coverage", "downside"),
    },
    "read_portfolio_rankings": {
        "keywords": ("rank", "ranking", "top performer", "worst performer"),
    },
    "read_portfolio_trailing": {
        "keywords": ("trail", "5ma", "defensive", "moving average status"),
    },
    "read_position_detail": {
        "keywords": ("position", "stock", "holding"),
    },
    "read_journal_summary": {
        "keywords": ("historical win rate", "trade history", "track record", "journal summary"),
    },
    "read_journal_trade_book": {
        "keywords": ("journal trades", "open journal trades", "closed journal trades", "editable trades", "current year trades"),
    },
    "read_journal_settings_summary": {
        "keywords": ("journal settings", "fund months", "portfolio capital", "tax rate"),
    },
    "read_trade_stats_period": {
        "keywords": ("last month", "last 6 months", "last year", "past year", "recent win rate", "rolling"),
    },
    "read_streak_analysis": {
        "keywords": ("streak", "consecutive", "after 2 losses", "after 3 wins"),
    },
    "read_trade_highlights": {
        "keywords": ("best trade", "biggest trade", "largest trade", "last 3 years", "past 3 years"),
    },
    "read_trade_r_extremes": {
        "keywords": ("r multiple", "r-multiple", "biggest r trade", "highest r trade", "best r trade", "largest r trade"),
    },
    "read_top_trade_winners": {
        "keywords": ("top winners", "biggest winners", "winning past trades", "past winners", "% winners", "percent winners", "top winning trades"),
    },
    "read_trade_history_snapshot": {
        "keywords": ("trade history page", "recent trades", "history preview", "fy trades"),
    },
    "read_fy_summary": {
        "keywords": ("fy", "financial year", "year summary"),
    },
    "read_analytics_overview": {
        "keywords": (
            "expectancy",
            "profit factor",
            "payoff ratio",
            "average winner",
            "avg winner",
            "average loser",
            "avg loser",
            "best trade",
            "worst trade",
            "return pct",
            "above 2r",
            "above 5r",
        ),
    },
    "read_monthly_summary": {
        "keywords": ("best month", "worst month", "monthly", "month wise", "monthwise", "monthly breakdown"),
    },
    "read_drawdown_analysis": {
        "keywords": ("drawdown", "recovery", "negative month", "losing month", "underperform", "outperform market"),
    },
    "read_equity_curve_snapshot": {
        "keywords": ("equity curve", "long term equity", "cumulative growth", "capital added"),
    },
    "read_outlier_analysis": {
        "keywords": ("outlier", "fat tail", "distribution", "concentration", "top 5 winners", "without top 3"),
    },
    "read_regime_summary": {
        "keywords": ("regime", "leading sectors"),
    },
    "read_sector_snapshot": {
        "keywords": ("sector", "breadth", "rotation"),
    },
    "read_index_constituents_snapshot": {
        "keywords": ("constituents", "members", "stocks in", "sector members", "index members"),
    },
    "read_live_monitor": {
        "keywords": ("live monitor", "live positions", "real time positions", "current price now"),
    },
    "read_saved_scanners": {
        "keywords": ("saved scanner", "scanner preset", "saved scan"),
    },
    "read_screener_snapshot": {
        "keywords": ("screener", "scan", "movers"),
    },
    "read_scoring_snapshot": {
        "keywords": ("score", "scoring"),
    },
    "read_explore_stock": {
        "keywords": ("stock deep dive", "explore stock", "52 week high", "52 week low", "adr", "liquidity", "market cap", "relative strength", "ma20", "ma50", "ma200"),
    },
    "read_explore_insights": {
        "keywords": ("stock insights", "explore insights", "recent results", "sectoral tailwind", "risk factors"),
    },
}


def _build_reader_args(name: str, message: str, stock_context: str | None = None):
    fy = _parse_fy_from_text(message)
    args: dict[str, Any] = {}
    if name == "read_position_detail" and stock_context:
        args["stock_name"] = stock_context
    if name == "read_trade_highlights":
        args.update({"years": _parse_year_window(message), "fy": fy, "message": message})
    elif name == "read_trade_r_extremes":
        args.update({"fy": fy, "message": message})
    elif name == "read_top_trade_winners":
        lowered = message.lower()
        sort_by = "pct" if any(token in lowered for token in ["%", "percent", "pct"]) else "pl"
        args.update({"fy": fy, "limit": _parse_top_n_from_text(message, default=5), "sort_by": sort_by, "message": message})
    elif name == "read_streak_analysis":
        args.update({"fy": fy, "message": message})
    elif name in {"read_analytics_overview", "read_outlier_analysis"}:
        args.update({"fy": fy or ("all" if "all time" in message.lower() or "last 3 years" in message.lower() else None), "message": message})
    elif name == "read_fy_summary":
        args.update({"fy": fy or current_fy_label()})
    elif name == "read_monthly_summary":
        args.update({"fy": fy or current_fy_label()})
    elif name == "read_trade_history_snapshot":
        args.update({"fy": fy or "2025-26", "limit": 12, "message": message})
    elif name == "read_trade_stats_period":
        args.update({"period": _parse_period_from_text(message) or "1y", "message": message})
    elif name == "read_journal_trade_book":
        if "open" in message.lower():
            args.update({"status": "open"})
        elif "closed" in message.lower():
            args.update({"status": "closed"})
        else:
            args.update({"status": "all"})
        args.update({"limit": 20})
    elif name == "read_journal_settings_summary":
        args.update({"year": int(current_fy_label().split("-")[0])})
    elif name == "read_explore_stock":
        symbol = stock_context or ""
        args.update({"symbol": symbol, "message": message})
    elif name == "read_explore_insights":
        symbol = stock_context or ""
        args.update({"symbol": symbol, "message": message})
    elif name == "read_index_constituents_snapshot":
        args.update({"message": message})
    return {key: value for key, value in args.items() if value is not None}


def suggest_reader_candidates(message: str, page_context: str | None = None, stock_context: str | None = None, limit: int = 4):
    msg = compact_whitespace(message).lower()
    candidates: dict[str, dict[str, Any]] = {}

    def add(name: str, score: int, reason: str):
        args = _build_reader_args(name, message, stock_context=stock_context)
        existing = candidates.get(name)
        if existing and existing["score"] >= score:
            return
        candidates[name] = {"name": name, "args": args, "score": score, "reason": reason}

    if stock_context:
        add("read_position_detail", 120, "stock context")

    if any(token in msg for token in ["streak", "consecutive", "after 2 losses", "after 3 wins"]):
        add("read_streak_analysis", 120, "streak analytics")
        add("read_analytics_overview", 70, "analytics backup")

    if any(token in msg for token in ["drawdown", "recovery", "worst drawdown", "negative month", "losing month", "underperform", "outperform market"]):
        add("read_drawdown_analysis", 120, "drawdown analytics")
        add("read_monthly_summary", 70, "monthly backup")
        add("read_equity_curve_snapshot", 68, "equity backup")

    if any(token in msg for token in ["outlier", "fat tail", "distribution", "concentration", "top 5 winners", "without top 3", "big winners"]):
        add("read_outlier_analysis", 120, "outlier analytics")
        add("read_analytics_overview", 75, "analytics backup")

    if any(token in msg for token in ["equity curve", "cumulative growth", "capital added", "long term equity"]):
        add("read_equity_curve_snapshot", 120, "equity curve")
        add("read_drawdown_analysis", 72, "drawdown backup")

    if any(token in msg for token in ["expectancy", "profit factor", "payoff ratio", "average winner", "avg winner", "average loser", "avg loser", "best trade ever", "worst trade ever", "above 2r", "above 5r"]):
        add("read_analytics_overview", 120, "core analytics")

    if any(token in msg for token in ["best month", "worst month", "monthly", "month wise", "monthwise", "monthly breakdown", "monthly pnl"]):
        add("read_monthly_summary", 115, "monthly analytics")
        add("read_analytics_overview", 70, "analytics backup")

    if any(token in msg for token in ["journal trades", "open journal trades", "closed journal trades", "editable trades", "current year trades"]):
        add("read_journal_trade_book", 118, "journal book")

    if any(token in msg for token in ["journal settings", "fund months", "portfolio capital", "tax rate"]):
        add("read_journal_settings_summary", 118, "journal settings")

    if any(token in msg for token in ["best trade", "worst trade", "biggest trade", "largest trade"]):
        if any(token in msg for token in ["last 3 years", "last 2 years", "past 3 years", "past 2 years", "fy", "financial year"]):
            add("read_trade_highlights", 125, "trade highlights")
        add("read_analytics_overview", 95, "trade analytics")
        add("read_journal_summary", 65, "journal backup")

    if any(token in msg for token in ["top winners", "biggest winners", "winning past trades", "past winners", "% winners", "percent winners", "top winning trades"]):
        add("read_top_trade_winners", 128, "historical winners")
        add("read_analytics_overview", 84, "analytics backup")

    if any(token in msg for token in ["r multiple", "r-multiple", "biggest r trade", "highest r trade", "best r trade", "largest r trade"]) or (" r " in f" {msg} " and "trade" in msg):
        add("read_trade_r_extremes", 132, "r multiple trade")
        add("read_analytics_overview", 82, "analytics backup")

    if any(token in msg for token in ["recent trades", "trade history page", "history preview", "fy trades"]):
        add("read_trade_history_snapshot", 116, "trade history")

    if any(token in msg for token in ["last month", "last 6 months", "last year", "past year", "2 years", "1 year", "recent win rate", "rolling"]):
        add("read_trade_stats_period", 110, "period analytics")
        add("read_analytics_overview", 70, "analytics backup")

    if any(token in msg for token in ["live monitor", "live positions", "real time positions", "current price now"]):
        add("read_live_monitor", 116, "live monitor")


    if any(token in msg for token in ["stock insights", "explore insights", "recent results", "sectoral tailwind", "risk factors"]):
        add("read_explore_insights", 110, "explore insights")
        add("read_explore_stock", 70, "explore backup")

    if any(token in msg for token in ["52 week", "52w", "adr", "liquidity", "market cap", "industry", "relative strength", "range position", "from high", "from low", "ma20", "ma50", "ma200", "ma 20", "ma 50", "ma 200"]):
        add("read_explore_stock", 112 if (stock_context or _stock_tokens(message)) else 78, "stock technicals")

    if any(token in msg for token in ["constituents", "members", "stocks in", "sector members", "index members"]):
        add("read_index_constituents_snapshot", 114, "index constituents")
        add("read_sector_snapshot", 72, "sector backup")

    for name, hint in READER_HINTS.items():
        for keyword in hint.get("keywords", ()):
            if keyword in msg:
                add(name, 50 + min(len(keyword), 18), f"keyword:{keyword}")
        if page_context and page_context in hint.get("page_contexts", set()):
            add(name, 30, f"context:{page_context}")

    if page_context in {"position-manager", "dashboard"} and not candidates:
        add("read_portfolio_overview", 40, f"context:{page_context}")
    if any(token in msg for token in ["portfolio", "positions", "overview", "holdings"]):
        add("read_portfolio_overview", 90, "portfolio question")
    if any(token in msg for token in ["sell", "trim", "exit", "5ma break"]):
        add("read_sell_actions", 100, "sell question")
    if any(token in msg for token in ["risk", "exposure", "capital at risk", "stop loss coverage"]):
        add("read_portfolio_risk", 100, "risk question")
    if any(token in msg for token in ["rank", "ranking", "top performer", "worst performer"]):
        add("read_portfolio_rankings", 90, "ranking question")
    if any(token in msg for token in ["trail", "5ma", "defensive", "moving average status"]):
        add("read_portfolio_trailing", 90, "trailing question")
    if any(token in msg for token in ["win rate", "track record", "trade history", "historical performance"]) and "recent" not in msg:
        add("read_journal_summary", 92, "journal summary")
    if any(token in msg for token in ["regime", "leading sectors"]):
        add("read_regime_summary", 95, "regime question")
    if any(token in msg for token in ["sector", "breadth", "rotation"]):
        add("read_sector_snapshot", 80, "sector question")
    if any(token in msg for token in ["score", "scoring"]):
        add("read_scoring_snapshot", 80, "scoring question")
    if any(token in msg for token in ["scanner preset", "saved scanner", "saved scanners", "saved scan"]):
        add("read_saved_scanners", 80, "saved screener question")
    if any(token in msg for token in ["screener", "scan", "movers"]):
        add("read_screener_snapshot", 75, "screener question")
    if any(token in msg for token in ["stock deep dive", "explore stock", "52 week high"]) and len(msg.split()) <= 12:
        add("read_explore_stock", 95, "explore question")

    ranked = sorted(candidates.values(), key=lambda item: (-item["score"], item["name"]))
    return ranked[:limit]


def suggest_primary_reader(message: str, page_context: str | None = None, stock_context: str | None = None):
    candidates = suggest_reader_candidates(message, page_context=page_context, stock_context=stock_context, limit=1)
    return candidates[0] if candidates else None
