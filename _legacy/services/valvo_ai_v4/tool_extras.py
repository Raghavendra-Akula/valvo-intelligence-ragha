"""
Valvo AI v4 -- Additional semantic tools.

scan_stocks, get_watchlist, get_journal_insights, compare_to_index
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from database.database import get_db, close_db
from services.valvo_ai_v2.utils import to_jsonable


def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


def _set_rls(cur, uid):
    if uid:
        cur.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": str(uid)}),),
        )
        cur.execute("SET LOCAL ROLE authenticated")


# ═══════════════════════════════════════════════════════════════════════════
#  scan_stocks — screener on stock_daily_summary
# ═══════════════════════════════════════════════════════════════════════════

def exec_scan_stocks(params: dict) -> dict:
    """
    Screen stocks using the pre-computed stock_daily_summary table.
    Supports preset filters and custom overrides.
    """
    preset = params.get("preset", "near_52w_high")
    min_liq_cr = params.get("min_liquidity_cr", 0.5)
    exclude_etf = params.get("exclude_etf", True)
    above_ma200 = params.get("above_ma200", True)
    above_ma50 = params.get("above_ma50", False)
    within_pct_of_high = params.get("within_pct_of_52w_high", 25)
    min_price = params.get("min_price")
    max_price = params.get("max_price")
    sector = (params.get("sector") or "").strip() or None
    sort_by = params.get("sort_by", "proximity_to_high")
    limit = min(params.get("limit") or 50, 200)

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor()
        uid = _get_user_id()
        _set_rls(cur, uid)

        wheres = []
        wparams: list = []

        # Base filters
        if exclude_etf:
            wheres.append("is_etf = false")
        if min_liq_cr:
            wheres.append("liq_cr >= %s")
            wparams.append(min_liq_cr)
        if above_ma200:
            wheres.append("prev_close > ma200")
            wheres.append("ma200 > 0")
        if above_ma50:
            wheres.append("prev_close > ma50")
            wheres.append("ma50 > 0")
        if within_pct_of_high:
            wheres.append("prev_close >= %s * high_52w / 100.0")
            wparams.append(100 - within_pct_of_high)
            wheres.append("high_52w > 0")
        if min_price is not None:
            wheres.append("prev_close >= %s")
            wparams.append(min_price)
        if max_price is not None:
            wheres.append("prev_close <= %s")
            wparams.append(max_price)

        # Preset-specific filters
        if preset == "breakout_candidates":
            # Near 52w high with strong volume
            wheres.append("prev_close >= 0.90 * high_52w")
            wheres.append("liq_cr >= 1.0")
        elif preset == "ma_crossover":
            # Price above both EMAs, EMA20 > EMA50
            wheres.append("ema20 > ema50")
            wheres.append("prev_close > ema20")
        elif preset == "beaten_down":
            # Below MA200, near 52w low
            wheres = [w for w in wheres if "ma200" not in w]  # remove above_ma200
            wheres.append("prev_close < ma200")
            wheres.append("prev_close <= 1.25 * low_52w")
            wheres.append("low_52w > 0")

        # Sector filter via index_constituents join
        if sector:
            wheres.append("""
                EXISTS (
                    SELECT 1 FROM index_constituents ic
                    WHERE ic.security_id = stock_daily_summary.security_id
                    AND LOWER(ic.sector) = LOWER(%s)
                )
            """)
            wparams.append(sector)

        where_sql = " AND ".join(wheres) if wheres else "TRUE"

        # Sort
        sort_col = {
            "proximity_to_high": "prev_close / NULLIF(high_52w, 0) DESC",
            "liquidity": "liq_cr DESC",
            "price": "prev_close DESC",
            "momentum_5d": "CASE WHEN close_5d > 0 THEN prev_close / close_5d ELSE 0 END DESC",
            "momentum_20d": "CASE WHEN close_20d > 0 THEN prev_close / close_20d ELSE 0 END DESC",
        }.get(sort_by, "prev_close / NULLIF(high_52w, 0) DESC")

        sql = f"""
            SELECT symbol, company_name, security_id,
                   ROUND(prev_close::numeric, 2) as price,
                   ROUND(high_52w::numeric, 2) as high_52w,
                   ROUND(low_52w::numeric, 2) as low_52w,
                   ROUND(ma50::numeric, 2) as ma50,
                   ROUND(ma200::numeric, 2) as ma200,
                   ROUND(liq_cr::numeric, 2) as liquidity_cr,
                   ROUND((prev_close / NULLIF(high_52w, 0) * 100)::numeric, 1) as pct_of_52w_high,
                   ROUND(ema20::numeric, 2) as ema20,
                   ROUND(ema50::numeric, 2) as ema50,
                   ROUND(ath::numeric, 2) as ath,
                   computed_date
            FROM stock_daily_summary
            WHERE {where_sql}
            ORDER BY {sort_col}
            LIMIT %s
        """
        cur.execute(sql, wparams + [limit])
        rows = [dict(r) for r in cur.fetchall()]

        return {
            "type": "stock_scan",
            "preset": preset,
            "count": len(rows),
            "stocks": to_jsonable(rows),
        }
    except Exception as exc:
        return {"error": f"Scan error: {exc}"}
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  get_watchlist — user's watchlists with items
# ═══════════════════════════════════════════════════════════════════════════

def exec_get_watchlist(params: dict) -> dict:
    """Fetch user's watchlists with items. Optionally enrich with live prices."""
    uid = _get_user_id()
    if not uid:
        return {"error": "No authenticated user"}

    watchlist_name = (params.get("name") or "").strip() or None
    include_prices = params.get("include_prices", False)

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor()
        _set_rls(cur, uid)

        # Fetch watchlists
        if watchlist_name:
            cur.execute(
                "SELECT id, name, pin_slot, color FROM watchlists "
                "WHERE user_id = %s AND LOWER(name) = LOWER(%s) ORDER BY sort_order",
                (uid, watchlist_name),
            )
        else:
            cur.execute(
                "SELECT id, name, pin_slot, color FROM watchlists "
                "WHERE user_id = %s ORDER BY sort_order",
                (uid,),
            )
        watchlists = [dict(r) for r in cur.fetchall()]

        if not watchlists:
            return {"type": "watchlists", "count": 0, "watchlists": []}

        # Fetch items for each watchlist
        wl_ids = [w["id"] for w in watchlists]
        cur.execute(
            "SELECT watchlist_id, symbol, company_name, security_id, notes, section_name "
            "FROM watchlist_items WHERE watchlist_id = ANY(%s) AND user_id = %s "
            "ORDER BY watchlist_id, sort_order",
            (wl_ids, uid),
        )
        items = [dict(r) for r in cur.fetchall()]

        # Optionally enrich with prices
        price_map = {}
        if include_prices and items:
            sec_ids = list({i["security_id"] for i in items if i.get("security_id")})
            if sec_ids:
                cur.execute(
                    "SELECT DISTINCT ON (security_id) security_id, close, date "
                    "FROM candles_daily WHERE security_id = ANY(%s) "
                    "ORDER BY security_id, date DESC",
                    (sec_ids,),
                )
                for r in cur.fetchall():
                    price_map[r["security_id"]] = {
                        "price": float(r["close"]),
                        "date": r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
                    }

        # Organize items under watchlists
        items_by_wl: dict[int, list] = {}
        for item in items:
            wl_id = item.pop("watchlist_id")
            if include_prices and item.get("security_id") in price_map:
                item.update(price_map[item["security_id"]])
            items_by_wl.setdefault(wl_id, []).append(item)

        for wl in watchlists:
            wl["items"] = items_by_wl.get(wl["id"], [])
            wl["item_count"] = len(wl["items"])

        return {
            "type": "watchlists",
            "count": len(watchlists),
            "watchlists": to_jsonable(watchlists),
        }
    except Exception as exc:
        return {"error": f"Watchlist error: {exc}"}
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  get_journal_insights — pattern analysis on journal data
# ═══════════════════════════════════════════════════════════════════════════

def exec_get_journal_insights(params: dict) -> dict:
    """
    Analyze patterns in journal_trades_computed.
    Answers questions like: "What setup type has my best win rate?"
    """
    uid = _get_user_id()
    if not uid:
        return {"error": "No authenticated user"}

    group_by = params.get("group_by", "entry_type")
    metrics = params.get("metrics") or ["count", "win_rate", "avg_return", "avg_r_multiple"]

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor()
        _set_rls(cur, uid)

        # Validate group_by column
        allowed_groups = {
            "entry_type": "entry_type",
            "setup": "setup->>'type'",
            "rating": "rating",
            "sector": "sector",
            "month": "month_label",
            "position_status": "position_status",
        }
        group_col = allowed_groups.get(group_by)
        if not group_col:
            return {"error": f"Invalid group_by. Allowed: {list(allowed_groups.keys())}"}

        # Load user stoploss for R-multiple
        sl_pct = 4.0
        try:
            cur.execute("SELECT stoploss_pct FROM user_profiles WHERE user_id = %s", (uid,))
            row = cur.fetchone()
            if row and row.get("stoploss_pct"):
                sl_pct = float(row["stoploss_pct"])
        except Exception:
            pass

        # Build metrics SQL
        metric_parts = []
        for m in metrics:
            if m == "count":
                metric_parts.append("COUNT(*) as trades")
            elif m == "win_rate":
                metric_parts.append(
                    "ROUND(SUM(CASE WHEN is_winner THEN 1 ELSE 0 END)::numeric * 100.0 / "
                    "NULLIF(COUNT(*), 0), 1) as win_rate"
                )
            elif m == "avg_return":
                metric_parts.append("ROUND(AVG(realized_pl_pct)::numeric, 2) as avg_return_pct")
            elif m == "sum_pl":
                metric_parts.append("ROUND(SUM(realized_pl)::numeric) as total_pl")
            elif m == "avg_r_multiple":
                metric_parts.append(
                    f"ROUND(AVG(realized_pl_pct / {sl_pct})::numeric, 2) as avg_r_multiple"
                )
            elif m == "profit_factor":
                metric_parts.append(
                    "ROUND("
                    "SUM(CASE WHEN realized_pl > 0 THEN realized_pl ELSE 0 END)::numeric / "
                    "NULLIF(ABS(SUM(CASE WHEN realized_pl <= 0 THEN realized_pl ELSE 0 END))::numeric, 0)"
                    ", 2) as profit_factor"
                )
        if not metric_parts:
            metric_parts.append("COUNT(*) as trades")

        sql = f"""
            SELECT {group_col} as group_label, {', '.join(metric_parts)}
            FROM journal_trades_computed
            WHERE user_id = %s AND realized_pl IS NOT NULL
            GROUP BY {group_col}
            HAVING COUNT(*) >= 2
            ORDER BY trades DESC
        """
        cur.execute(sql, (uid,))
        rows = [dict(r) for r in cur.fetchall()]

        # Also fetch totals
        total_sql = f"""
            SELECT {', '.join(metric_parts)}
            FROM journal_trades_computed
            WHERE user_id = %s AND realized_pl IS NOT NULL
        """
        cur.execute(total_sql, (uid,))
        totals = dict(cur.fetchone()) if cur.rowcount else {}

        return {
            "type": "journal_insights",
            "grouped_by": group_by,
            "count": len(rows),
            "groups": to_jsonable(rows),
            "totals": to_jsonable(totals),
        }
    except Exception as exc:
        return {"error": f"Journal insights error: {exc}"}
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  compare_to_index — portfolio vs benchmark
# ═══════════════════════════════════════════════════════════════════════════

def exec_compare_to_index(params: dict) -> dict:
    """
    Compare portfolio returns against an index (Nifty 50, Smallcap, etc.)
    for a given date range or financial year.
    """
    uid = _get_user_id()
    index_symbol = (params.get("index") or "Nifty 50").strip()
    fy = params.get("fy") or None
    days = params.get("days") or None

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor()
        _set_rls(cur, uid)

        # Determine date range
        if fy:
            parts = fy.split("-")
            start_year = int(parts[0])
            start_date = date(start_year, 4, 1)
            end_date = date(start_year + 1, 3, 31)
        elif days:
            end_date = date.today()
            start_date = end_date - timedelta(days=int(days))
        else:
            # Default: current FY
            today = date.today()
            start_year = today.year if today.month >= 4 else today.year - 1
            start_date = date(start_year, 4, 1)
            end_date = today

        # Get index returns
        cur.execute("""
            SELECT date, close FROM candles_indices
            WHERE LOWER(symbol) = LOWER(%s)
            AND date BETWEEN %s AND %s
            ORDER BY date
        """, (index_symbol, start_date, end_date))
        index_rows = cur.fetchall()

        if not index_rows:
            # Try partial match
            cur.execute("""
                SELECT DISTINCT symbol FROM candles_indices
                WHERE LOWER(symbol) LIKE LOWER(%s)
                LIMIT 5
            """, (f"%{index_symbol}%",))
            suggestions = [r["symbol"] for r in cur.fetchall()]
            return {
                "error": f"No data for index '{index_symbol}'.",
                "available_indices": suggestions,
            }

        idx_start = float(index_rows[0]["close"])
        idx_end = float(index_rows[-1]["close"])
        idx_return = round((idx_end - idx_start) / idx_start * 100, 2) if idx_start else 0

        # Get portfolio P&L for the same period (from trade tables)
        # Determine which FY tables overlap with the date range
        from .tool_trades import FY_TABLE_MAP, _resolve_fy_tables

        portfolio_pl = 0
        portfolio_trades = 0
        portfolio_winners = 0

        fy_tables = _resolve_fy_tables(["all"], uid)
        for fy_label, table, needs_user_filter in fy_tables:
            uf = " AND user_id = %s" if needs_user_filter and uid else ""
            uf_params = [uid] if needs_user_filter and uid else []

            # For legacy tables without trade_date, use FY boundaries
            fy_parts = fy_label.split("-")
            fy_start_year = int(fy_parts[0])
            fy_start = date(fy_start_year, 4, 1)
            fy_end = date(fy_start_year + 1, 3, 31)

            # Skip FYs outside our date range
            if fy_end < start_date or fy_start > end_date:
                continue

            try:
                cur.execute(f"""
                    SELECT COALESCE(SUM(realized_pl), 0) as pl,
                           COUNT(*) as cnt,
                           SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) as wins
                    FROM {table}
                    WHERE TRUE {uf}
                """, uf_params)
                row = cur.fetchone()
                portfolio_pl += float(row["pl"] or 0)
                portfolio_trades += int(row["cnt"] or 0)
                portfolio_winners += int(row["wins"] or 0)
            except Exception:
                continue

        # Get base capital for return calculation. Source of truth is
        # user_fy_config (current FY, then any FY as fallback). Legacy
        # user_profiles.current_capital is no longer written.
        base_capital = 5000000  # fallback
        if uid:
            try:
                from services.user_analytics_service import get_user_base_capital
                from datetime import date as _d
                _t = _d.today()
                _y = _t.year if _t.month >= 4 else _t.year - 1
                _fy = f"{_y}-{str((_y + 1) % 100).zfill(2)}"
                bc = get_user_base_capital(cur, uid, _fy)
                if bc is None:
                    cur.execute(
                        "SELECT base_capital FROM user_fy_config "
                        "WHERE user_id = %s AND base_capital IS NOT NULL "
                        "ORDER BY fy DESC LIMIT 1",
                        (uid,),
                    )
                    row = cur.fetchone()
                    if row and row.get("base_capital"):
                        bc = float(row["base_capital"])
                if bc is not None:
                    base_capital = float(bc)
            except Exception:
                pass

        portfolio_return = round(portfolio_pl / base_capital * 100, 2) if base_capital else 0
        win_rate = round(portfolio_winners / portfolio_trades * 100, 1) if portfolio_trades else 0

        return {
            "type": "index_comparison",
            "period": f"{start_date.isoformat()} to {end_date.isoformat()}",
            "index": {
                "symbol": index_symbol,
                "start_price": round(idx_start, 2),
                "end_price": round(idx_end, 2),
                "return_pct": idx_return,
            },
            "portfolio": {
                "net_pl": round(portfolio_pl),
                "return_pct": portfolio_return,
                "trades": portfolio_trades,
                "win_rate": win_rate,
            },
            "alpha": round(portfolio_return - idx_return, 2),
        }
    except Exception as exc:
        return {"error": f"Comparison error: {exc}"}
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  search_stock — fuzzy, multi-word, handles misspellings
# ═══════════════════════════════════════════════════════════════════════════

def exec_search_stock(params: dict) -> dict:
    """
    Search stocks using embedding similarity (primary) + ILIKE (fallback).
    Handles misspellings, informal names, partial matches — all via cosine similarity.
    """
    query = (params.get("query") or "").strip()
    if not query:
        return {"error": "No search query provided"}

    # Strategy 1: Embedding similarity (typo-proof, semantic)
    try:
        from .stock_embeddings import search_by_embedding
        emb_results = search_by_embedding(query, top_k=10)
        if emb_results and emb_results[0]["similarity"] >= 0.65:
            has_exact = any(r["symbol"].upper() == query.upper() for r in emb_results)
            return {
                "type": "stock_search",
                "query": query,
                "count": len(emb_results),
                "results": to_jsonable(emb_results),
                "method": "embedding",
                "note": None if has_exact else "No exact match — showing closest semantic matches",
            }
    except Exception as e:
        print(f"[search_stock] embedding search failed, falling back to ILIKE: {e}")

    # Strategy 2: ILIKE fallback (if embeddings unavailable)
    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT security_id, symbol, company_name
            FROM stock_universe
            WHERE symbol ILIKE %s OR company_name ILIKE %s
            ORDER BY
                CASE WHEN symbol ILIKE %s THEN 0
                     WHEN symbol ILIKE %s THEN 1
                     ELSE 2 END,
                company_name
            LIMIT 10
            """,
            (f"%{query}%", f"%{query}%", query, f"{query}%"),
        )
        results = [dict(r) for r in cur.fetchall()]

        # If no ILIKE matches, try splitting words
        if not results and " " in query:
            words = [w for w in query.split() if len(w) >= 3]
            if words:
                conditions = " AND ".join(["company_name ILIKE %s"] * len(words))
                cur.execute(
                    f"SELECT security_id, symbol, company_name FROM stock_universe WHERE {conditions} LIMIT 10",
                    [f"%{w}%" for w in words],
                )
                results = [dict(r) for r in cur.fetchall()]

        return {
            "type": "stock_search",
            "query": query,
            "count": len(results),
            "results": to_jsonable(results),
            "method": "ilike",
        }
    except Exception as exc:
        return {"error": f"Stock search error: {exc}"}
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  get_live_market — uses stock_daily_summary (covers ALL stocks)
# ═══════════════════════════════════════════════════════════════════════════

def exec_get_live_market(params: dict) -> dict:
    """
    Get market data for stocks using stock_daily_summary (pre-computed daily).
    Covers ALL active stocks. No dependency on candles_daily.
    """
    symbols = params.get("symbols") or []
    if not symbols:
        return {"error": "No symbols provided"}

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor()
        uid = _get_user_id()
        _set_rls(cur, uid)

        results = []
        for sym in symbols[:10]:
            sym_clean = sym.strip().upper()

            # Look up in stock_daily_summary (has everything)
            cur.execute("""
                SELECT s.symbol, s.company_name, s.security_id,
                       COALESCE(d.live_close, d.prev_close) AS prev_close,
                       d.high_52w, d.low_52w,
                       d.ma50, d.ma200, d.ema20, d.ema50, d.ema200,
                       d.ath, d.liq_cr, d.computed_date,
                       d.close_5d, d.close_20d, d.close_60d
                FROM stock_daily_summary d
                JOIN stock_universe s ON s.security_id = d.security_id
                WHERE s.symbol ILIKE %s AND s.is_active = true
                LIMIT 1
            """, (sym_clean,))
            row = cur.fetchone()

            # Try partial match if exact fails
            if not row:
                cur.execute("""
                    SELECT s.symbol, s.company_name, s.security_id,
                           COALESCE(d.live_close, d.prev_close) AS prev_close,
                           d.high_52w, d.low_52w,
                           d.ma50, d.ma200, d.ema20, d.ema50, d.ema200,
                           d.ath, d.liq_cr, d.computed_date,
                           d.close_5d, d.close_20d, d.close_60d
                    FROM stock_daily_summary d
                    JOIN stock_universe s ON s.security_id = d.security_id
                    WHERE s.symbol ILIKE %s AND s.is_active = true
                    LIMIT 1
                """, (f"%{sym_clean}%",))
                row = cur.fetchone()

            if not row:
                # Stock exists in universe but not in daily summary?
                cur.execute(
                    "SELECT symbol, company_name FROM stock_universe WHERE symbol ILIKE %s LIMIT 1",
                    (f"%{sym_clean}%",),
                )
                stock = cur.fetchone()
                if stock:
                    results.append({
                        "symbol": stock["symbol"],
                        "company": stock["company_name"],
                        "note": "Stock found but no market data yet. Try search_stock for more info.",
                        "suggestion": "Use sql_query to check candles_daily or check if this is a recently listed stock.",
                    })
                else:
                    results.append({
                        "symbol": sym_clean,
                        "note": f"No stock matching '{sym_clean}'. Use search_stock to find the correct symbol.",
                    })
                continue

            entry = {
                "symbol": row["symbol"],
                "company": row["company_name"],
                "security_id": row["security_id"],
                "price": round(float(row["prev_close"] or 0), 2),
                "price_date": str(row["computed_date"]) if row["computed_date"] else None,
                "high_52w": round(float(row["high_52w"] or 0), 2),
                "low_52w": round(float(row["low_52w"] or 0), 2),
                "ath": round(float(row["ath"] or 0), 2),
                "ma50": round(float(row["ma50"] or 0), 2),
                "ma200": round(float(row["ma200"] or 0), 2),
                "ema20": round(float(row["ema20"] or 0), 2),
                "ema50": round(float(row["ema50"] or 0), 2),
                "ema200": round(float(row["ema200"] or 0), 2),
                "liquidity_cr": round(float(row["liq_cr"] or 0), 2),
            }

            # Compute proximity to 52w high
            if entry["high_52w"] > 0:
                entry["pct_from_52w_high"] = round(
                    (entry["price"] / entry["high_52w"] - 1) * 100, 1
                )

            # Short-term momentum from stored closes
            if row["close_5d"] and row["close_5d"] > 0:
                entry["change_5d_pct"] = round(
                    (float(row["prev_close"] or 0) / float(row["close_5d"]) - 1) * 100, 1
                )
            if row["close_20d"] and row["close_20d"] > 0:
                entry["change_20d_pct"] = round(
                    (float(row["prev_close"] or 0) / float(row["close_20d"]) - 1) * 100, 1
                )

            results.append(entry)

        return {"type": "live_market", "count": len(results), "data": to_jsonable(results)}
    except Exception as exc:
        return {"error": f"Live market error: {exc}"}
    finally:
        close_db(conn)
# ═══════════════════════════════════════════════════════════════════════════

def exec_get_fundamentals(params: dict) -> dict:
    """
    Fetch fundamental data for a stock symbol.
    Wraps the internal fundamentals endpoints — no HTTP round-trip.
    """
    symbol = (params.get("symbol") or "").strip().upper()
    if not symbol:
        return {"error": "symbol is required"}

    sections = params.get("sections") or ["overview"]
    limit_quarters = min(params.get("limit_quarters") or 8, 20)
    limit_years = min(params.get("limit_years") or 5, 12)

    conn = get_db()
    if not conn:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor()
        uid = _get_user_id()
        _set_rls(cur, uid)

        # Resolve symbol → security_id
        cur.execute(
            "SELECT security_id, symbol, company_name FROM stock_universe "
            "WHERE symbol ILIKE %s AND is_active = true LIMIT 1",
            (symbol,),
        )
        stock = cur.fetchone()
        if not stock:
            return {"error": f"Stock '{symbol}' not found"}

        sid = stock["security_id"]
        result = {
            "type": "fundamentals",
            "symbol": stock["symbol"],
            "company_name": stock["company_name"],
        }

        # Determine consolidated vs standalone preference
        is_cons = True
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM financials_quarterly WHERE security_id = %s AND is_consolidated = true) as has_cons",
            (sid,),
        )
        row = cur.fetchone()
        if row and not row["has_cons"]:
            is_cons = False

        if "overview" in sections:
            result["overview"] = _fundamentals_overview(cur, sid, symbol, is_cons)

        if "quarterly" in sections:
            result["quarterly"] = _fundamentals_quarterly(cur, sid, is_cons, limit_quarters)

        if "annual" in sections:
            result["annual"] = _fundamentals_annual(cur, sid, is_cons, limit_years)

        if "shareholding" in sections:
            result["shareholding"] = _fundamentals_shareholding(cur, sid)

        if "peers" in sections:
            result["peers"] = _fundamentals_peers(cur, sid, symbol)

        return to_jsonable(result)
    except Exception as exc:
        return {"error": f"Fundamentals error: {exc}"}
    finally:
        close_db(conn)


def _fundamentals_overview(cur, sid, symbol, is_cons):
    """Key ratios and metrics snapshot."""
    # Try pre-computed overview first
    cur.execute(
        "SELECT * FROM fundamentals_overview WHERE security_id = %s",
        (sid,),
    )
    row = cur.fetchone()
    if row:
        overview = {k: _fmt(v) if isinstance(v, (int, float)) else v for k, v in dict(row).items() if k != "security_id"}
        # fundamentals_overview.current_price is refreshed nightly and goes stale intraday.
        # Overwrite with the latest close from candles_daily.
        cur.execute(
            "SELECT close, date FROM candles_daily WHERE security_id = %s ORDER BY date DESC LIMIT 1",
            (sid,),
        )
        candle = cur.fetchone()
        if candle and candle["close"] is not None:
            fresh_price = float(candle["close"])
            overview["current_price"] = _fmt(fresh_price)
            overview["price_date"] = str(candle["date"])
            total_shares_cr = overview.get("total_shares_cr")
            if total_shares_cr:
                overview["market_cap_cr"] = _fmt(float(total_shares_cr) * fresh_price)
        return overview

    # Fallback: compute from quarterly TTM
    cur.execute("""
        SELECT ROUND(SUM(revenue_cr)::numeric) as revenue_ttm_cr,
               ROUND(SUM(net_profit_cr)::numeric) as net_profit_ttm_cr,
               ROUND(SUM(net_profit_cr)::numeric * 100.0 / NULLIF(SUM(revenue_cr), 0)::numeric, 1) as net_margin,
               ROUND(SUM(operating_profit_cr)::numeric * 100.0 / NULLIF(SUM(revenue_cr), 0)::numeric, 1) as opm
        FROM (
            SELECT revenue_cr, net_profit_cr, operating_profit_cr
            FROM financials_quarterly
            WHERE security_id = %s AND is_consolidated = %s
            ORDER BY period_end_date DESC LIMIT 4
        ) q
    """, (sid, is_cons))
    ttm = cur.fetchone()
    return dict(ttm) if ttm else {}


def _fundamentals_quarterly(cur, sid, is_cons, limit):
    """Recent quarterly P&L."""
    cur.execute("""
        SELECT period, period_end_date,
               ROUND(revenue_cr::numeric) as revenue_cr,
               ROUND(operating_profit_cr::numeric) as operating_profit_cr,
               ROUND(opm_percent::numeric, 1) as opm_pct,
               ROUND(net_profit_cr::numeric) as net_profit_cr,
               ROUND(eps::numeric, 2) as eps
        FROM financials_quarterly
        WHERE security_id = %s AND is_consolidated = %s
        ORDER BY period_end_date DESC LIMIT %s
    """, (sid, is_cons, limit))
    return [dict(r) for r in cur.fetchall()]


def _fundamentals_annual(cur, sid, is_cons, limit):
    """Annual financials with ratios."""
    cur.execute("""
        SELECT fiscal_year, period_end_date,
               ROUND(revenue_cr::numeric) as revenue_cr,
               ROUND(net_profit_cr::numeric) as net_profit_cr,
               ROUND(eps::numeric, 2) as eps,
               ROUND(roe::numeric, 1) as roe,
               ROUND(roce::numeric, 1) as roce,
               ROUND(debt_to_equity::numeric, 2) as debt_to_equity,
               ROUND(operating_cashflow_cr::numeric) as operating_cf_cr,
               ROUND(free_cashflow_cr::numeric) as free_cf_cr,
               ROUND(dividend_per_share::numeric, 2) as dps
        FROM financials_annual
        WHERE security_id = %s AND is_consolidated = %s
        ORDER BY period_end_date DESC LIMIT %s
    """, (sid, is_cons, limit))
    return [dict(r) for r in cur.fetchall()]


def _fundamentals_shareholding(cur, sid):
    """Last 4 quarters of shareholding pattern."""
    cur.execute("""
        SELECT period, period_end_date,
               ROUND(promoter_percent::numeric, 2) as promoter_pct,
               ROUND(fii_percent::numeric, 2) as fii_pct,
               ROUND(dii_percent::numeric, 2) as dii_pct,
               ROUND(public_percent::numeric, 2) as public_pct,
               ROUND(promoter_pledge_percent::numeric, 2) as pledge_pct
        FROM shareholding_quarterly
        WHERE security_id = %s
        ORDER BY period_end_date DESC LIMIT 4
    """, (sid,))
    return [dict(r) for r in cur.fetchall()]


def _fundamentals_peers(cur, sid, symbol):
    """Industry peers."""
    cur.execute("""
        SELECT p.peer_security_id, su.symbol, su.company_name, p.relevance_rank
        FROM peers p
        JOIN stock_universe su ON su.security_id = p.peer_security_id
        WHERE p.security_id = %s
        ORDER BY p.relevance_rank
        LIMIT 10
    """, (sid,))
    rows = cur.fetchall()
    if rows:
        return [dict(r) for r in rows]

    # Fallback: same industry from bse_company_master
    cur.execute("""
        SELECT bm2.symbol, bm2.company_name
        FROM bse_company_master bm1
        JOIN bse_company_master bm2 ON bm2.industry = bm1.industry AND bm2.symbol != bm1.symbol
        WHERE bm1.symbol = %s
        LIMIT 10
    """, (symbol,))
    return [dict(r) for r in cur.fetchall()]


def _fmt(v):
    if v is None:
        return None
    if isinstance(v, float):
        return round(v, 2)
    return v
