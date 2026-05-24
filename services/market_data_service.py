"""
market_data_service.py — Supabase WebSocket-powered market data
Primary and ONLY data source: candles_daily + stock_universe (Supabase PostgreSQL)
Updated every ~10s via websocket VM

Also includes: Yahoo Finance (market cap/sector) + bundled shares data
These are NOT price data — they're fundamental/reference data.

Tables used:
  candles_daily  — OHLCV per stock per day, updated every ~10s during market hours
  stock_universe — 2,434 NSE stocks with security_id, symbol, company_name
"""
import os
import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, date, timedelta
from database.database import get_db, close_db
from config.settings import is_trading_day as _is_trading_day, prev_trading_date_or_today


# ═══════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════

MARKET_OPEN_HOUR = 9
MARKET_CLOSE_HOUR = 15

def is_data_fresh():
    """Check if candles_daily has recent data.
    On holidays/weekends, returns True with the last trading day's data
    so the rest of the app doesn't think data is missing."""
    conn = None
    try:
        # If today is not a trading day, data is "fresh" by definition — market is closed
        if not _is_trading_day():
            target = prev_trading_date_or_today()
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) as cnt, MAX(updated_at) as last_update
                FROM candles_daily WHERE date = %s
            """, (target.isoformat(),))
            row = cur.fetchone()
            cnt = row["cnt"] or 0
            last_update = row["last_update"]
            return True, str(last_update) if last_update else None, cnt

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) as cnt, MAX(updated_at) as last_update
            FROM candles_daily WHERE date = CURRENT_DATE
        """)
        row = cur.fetchone()
        cnt = row["cnt"] or 0
        last_update = row["last_update"]
        if cnt == 0:
            return False, None, 0

        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        if MARKET_OPEN_HOUR <= now_ist.hour < MARKET_CLOSE_HOUR and last_update:
            from datetime import timezone
            age_minutes = (datetime.now(timezone.utc) - last_update).total_seconds() / 60
            if age_minutes > 5:
                return False, str(last_update), cnt

        return True, str(last_update) if last_update else None, cnt
    except Exception as e:
        print(f"❌ is_data_fresh error: {e}")
        return False, None, 0
    finally:
        if conn:
            close_db(conn)


def get_market_health():
    fresh, last_update, count = is_data_fresh()
    return {
        "source": "supabase_websocket",
        "fresh": fresh,
        "last_update": last_update,
        "stocks_today": count,
    }


# ═══════════════════════════════════════════════════════════════════
# STOCK SEARCH — from stock_universe
# ═══════════════════════════════════════════════════════════════════

def search_stocks(query, kinds=None):
    """Search the universe, optionally including ETFs and indices.

    `kinds` is an iterable of strings selecting which asset classes to return:
      - "stock"  → equity from stock_universe (is_etf = false)
      - "etf"    → ETF from stock_universe (is_etf = true)
      - "index"  → index from index_daily_summary (no security_id)
    When omitted, defaults to ("stock", "etf") so existing callers
    (Watchlist, Scoring, Journal, AddPosition, CommandPalette, Alerts)
    keep working without seeing indices in their dropdowns. The Explore
    page opts in by passing kinds=stock,etf,index.

    Each result is tagged with `kind` so callers can render badges or
    route differently. Stocks/ETFs carry security_id; indices don't.
    """
    if not query or len(query) < 2:
        return []
    if kinds is None:
        kinds = ("stock", "etf")
    kinds = set(kinds)
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        q = query.strip().lower()
        params = {"exact": q, "starts": f"{q}%", "contains": f"%{q}%", "anywhere": f"%{q}%"}

        results = []

        # ── Stocks + ETFs from stock_universe ──────────────────────────────
        if "stock" in kinds or "etf" in kinds:
            etf_clause = ""
            if "stock" in kinds and "etf" not in kinds:
                etf_clause = "AND COALESCE(is_etf, false) = false"
            elif "etf" in kinds and "stock" not in kinds:
                etf_clause = "AND COALESCE(is_etf, false) = true"
            cur.execute(f"""
                SELECT security_id, symbol, company_name, exchange,
                    COALESCE(is_etf, false) AS is_etf,
                    CASE
                        WHEN LOWER(symbol) = %(exact)s THEN 100
                        WHEN LOWER(symbol) LIKE %(starts)s THEN 90
                        WHEN LOWER(company_name) ILIKE %(contains)s THEN 75
                        WHEN LOWER(symbol) LIKE %(contains)s THEN 50
                        ELSE 40
                    END as score
                FROM stock_universe
                WHERE is_active = true {etf_clause} AND (
                    LOWER(symbol) = %(exact)s OR LOWER(symbol) LIKE %(starts)s
                    OR LOWER(company_name) ILIKE %(contains)s OR LOWER(symbol) LIKE %(anywhere)s
                )
                ORDER BY score DESC, symbol ASC LIMIT 12
            """, params)
            results.extend([{
                "security_id": r["security_id"], "symbol": r["symbol"],
                "company_name": r["company_name"] or r["symbol"],
                "exchange": r["exchange"] or "NSE_EQ",
                "kind": "etf" if r["is_etf"] else "stock",
                "match_confidence": "exact" if r["score"] >= 95 else "high" if r["score"] >= 70 else "partial",
            } for r in cur.fetchall()])

        # ── Indices from index_daily_summary ───────────────────────────────
        # Symbol IS the identifier here; no security_id. Match against the
        # stripped name too so users searching "IT" hit "NIFTYIT".
        if "index" in kinds:
            cur.execute("""
                SELECT symbol, category,
                    CASE
                        WHEN LOWER(symbol) = %(exact)s THEN 100
                        WHEN LOWER(symbol) LIKE %(starts)s THEN 90
                        WHEN LOWER(REGEXP_REPLACE(symbol, '^NIFTY ?', '', 'i')) = %(exact)s THEN 88
                        WHEN LOWER(symbol) LIKE %(contains)s THEN 70
                        ELSE 40
                    END as score
                FROM index_daily_summary
                WHERE category IN ('broad', 'sector', 'thematic')
                  AND (
                    LOWER(symbol) = %(exact)s
                    OR LOWER(symbol) LIKE %(starts)s
                    OR LOWER(symbol) LIKE %(contains)s
                    OR LOWER(REGEXP_REPLACE(symbol, '^NIFTY ?', '', 'i')) = %(exact)s
                  )
                ORDER BY score DESC, symbol ASC LIMIT 6
            """, params)
            results.extend([{
                "security_id": None, "symbol": r["symbol"],
                "company_name": r["symbol"],
                "exchange": "INDEX",
                "kind": "index",
                "category": r["category"],
                "match_confidence": "exact" if r["score"] >= 95 else "high" if r["score"] >= 70 else "partial",
            } for r in cur.fetchall()])

        return results
    except Exception as e:
        print(f"❌ search_stocks error: {e}")
        return []
    finally:
        if conn: close_db(conn)


# ═══════════════════════════════════════════════════════════════════
# LIVE PRICE (LTP) — from candles_daily
# ═══════════════════════════════════════════════════════════════════

def get_ltp(security_ids):
    if not security_ids:
        return {}
    ids = [str(s["security_id"]) if isinstance(s, dict) else str(s) for s in security_ids]
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        # Reads live_close from stock_daily_summary — kept fresh every
        # ~10s during market hours by sync_prev_close_from_candles. Same
        # data the watchlist /items/enriched endpoint serves, so the
        # 200-sid LTP poll no longer DISTINCT-ONs candles_daily (6.56M
        # rows) every 10s. Falls back to prev_close if live_close hasn't
        # been written yet (off-hours or first day after listing).
        cur.execute("""
            SELECT security_id, COALESCE(live_close, prev_close) AS px
            FROM stock_daily_summary
            WHERE security_id = ANY(%(ids)s)
              AND COALESCE(live_close, prev_close) > 0
        """, {"ids": ids})
        out = {str(r["security_id"]): r["px"] for r in cur.fetchall()}
        # Fresh IPOs (< 50 trading days) aren't in stock_daily_summary yet,
        # so the watchlist sidebar would stop ticking for them during market
        # hours. Resolve the remainder from the latest candle in candles_daily.
        missing = [s for s in ids if s not in out]
        if missing:
            cur.execute("""
                SELECT DISTINCT ON (security_id) security_id, close AS px
                FROM candles_daily
                WHERE security_id = ANY(%(ids)s) AND volume > 0
                ORDER BY security_id, date DESC
            """, {"ids": missing})
            for r in cur.fetchall():
                if r["px"] and r["px"] > 0:
                    out[str(r["security_id"])] = r["px"]
        return out
    except Exception as e:
        print(f"❌ get_ltp error: {e}")
        return {}
    finally:
        if conn: close_db(conn)


# ═══════════════════════════════════════════════════════════════════
# LIVE CANDLE (OHLCV today) — from candles_daily
# ═══════════════════════════════════════════════════════════════════

def get_live_candle(security_id):
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        # Filter on close > 0 only — same as get_chart_data above. The
        # earlier `AND volume > 0` filter caused divergence: if the
        # upstream feed wrote today's price tick before the volume
        # update landed (close=X, volume=0), this query would skip
        # today and return yesterday's row, while get_chart_data + the
        # watchlist enriched query both returned today's. The toolbar's
        # ohlc.close would then disagree with both the chart series and
        # the watchlist for the same stock. Aligning the filters fixes
        # the three-way mismatch.
        cur.execute("""
            SELECT date, open, high, low, close, volume
            FROM candles_daily WHERE security_id = %(sid)s AND close > 0
            ORDER BY date DESC LIMIT 1
        """, {"sid": str(security_id)})
        r = cur.fetchone()
        if r and r["close"] and r["close"] > 0:
            return {"time": str(r["date"]), "open": r["open"], "high": r["high"],
                    "low": r["low"], "close": r["close"], "volume": r["volume"] or 0}
        return None
    except Exception as e:
        print(f"❌ get_live_candle error: {e}")
        return None
    finally:
        if conn: close_db(conn)


# ═══════════════════════════════════════════════════════════════════
# HISTORICAL DAILY — from candles_daily
# ═══════════════════════════════════════════════════════════════════

def get_historical_daily(security_id, days=25, verbose=True, background=False):
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT date, open, high, low, close, volume FROM (
                SELECT date, open, high, low, close, volume
                FROM candles_daily WHERE security_id = %s AND volume > 0
                ORDER BY date DESC LIMIT %s
            ) sub ORDER BY date ASC
        """, (str(security_id), days))
        rows = cur.fetchall()
        if verbose and rows:
            print(f"📊 {len(rows)} candles for {security_id}")
        return [{"date": str(r["date"]), "open": r["open"], "high": r["high"],
                 "low": r["low"], "close": r["close"], "volume": r["volume"] or 0} for r in rows]
    except Exception as e:
        print(f"❌ get_historical_daily error: {e}")
        return []
    finally:
        if conn: close_db(conn)


# ═══════════════════════════════════════════════════════════════════
# MA CALCULATION — SQL window functions
# ═══════════════════════════════════════════════════════════════════

def calculate_mas(security_id):
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT close,
                AVG(close) OVER (ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
                AVG(close) OVER (ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as ma10,
                AVG(close) OVER (ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as ma20,
                COUNT(*) OVER () as total_rows
            FROM candles_daily WHERE security_id = %(sid)s AND volume > 0
            ORDER BY date DESC LIMIT 1
        """, {"sid": str(security_id)})
        r = cur.fetchone()
        if not r: return None
        return {
            "latest_close": round(r["close"], 2), "candle_count": r["total_rows"],
            "five_ma": round(r["ma5"], 2) if r["ma5"] else None,
            "ten_ma": round(r["ma10"], 2) if r["ma10"] else None,
            "twenty_ma": round(r["ma20"], 2) if r["ma20"] else None,
        }
    except Exception as e:
        print(f"❌ calculate_mas error: {e}")
        return None
    finally:
        if conn: close_db(conn)


# ═══════════════════════════════════════════════════════════════════
# SINGLE POSITION REFRESH
# ═══════════════════════════════════════════════════════════════════

def refresh_position_data(security_id, entry_price, risk_pct, leg_base_price=None, valvo_ref_price=None):
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            WITH ranked AS (
                SELECT date, close,
                    AVG(close) OVER (ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
                    AVG(close) OVER (ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as ma10,
                    AVG(close) OVER (ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as ma20,
                    ROW_NUMBER() OVER (ORDER BY date DESC) as rn
                FROM candles_daily WHERE security_id = %(sid)s AND volume > 0
            )
            SELECT close, ma5, ma10, ma20 FROM ranked WHERE rn = 1
        """, {"sid": str(security_id)})
        r = cur.fetchone()
        if not r or not r["close"]: return None

        cp = r["close"]
        ma5 = round(r["ma5"], 2) if r["ma5"] else None
        ma10 = round(r["ma10"], 2) if r["ma10"] else None
        ma20 = round(r["ma20"], 2) if r["ma20"] else None
        lb = leg_base_price or entry_price
        vr = valvo_ref_price or entry_price
        rps = entry_price * (risk_pct / 100.0) if risk_pct else entry_price * 0.04

        defensive = "safe"
        if ma5:
            d = ((cp - ma5) / ma5) * 100
            if d < -1.0: defensive = "break"
            elif d < 0: defensive = "marginal"

        return {
            "current_price": cp, "five_ma": ma5, "ten_ma": ma10, "twenty_ma": ma20,
            "leg_extension_pct": round(((cp - lb) / lb * 100), 2) if lb else 0,
            "entry_extension_pct": round(((cp - entry_price) / entry_price * 100), 2) if entry_price else 0,
            "valvo_extension_pct": round(((cp - vr) / vr * 100), 2) if vr else 0,
            "r_multiple": round(((cp - entry_price) / rps), 2) if rps else 0,
            "defensive_status": defensive,
        }
    except Exception as e:
        print(f"❌ refresh_position_data error: {e}")
        return None
    finally:
        if conn: close_db(conn)


# ═══════════════════════════════════════════════════════════════════
# BULK REFRESH — All positions in ONE query
# ═══════════════════════════════════════════════════════════════════

def bulk_refresh_positions(positions):
    if not positions: return {}
    sec_ids = [str(p.get("security_id", "")) for p in positions if p.get("security_id")]
    if not sec_ids: return {}
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (security_id)
                    security_id, date, close,
                    AVG(close) OVER (PARTITION BY security_id ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
                    AVG(close) OVER (PARTITION BY security_id ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as ma10,
                    AVG(close) OVER (PARTITION BY security_id ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as ma20
                FROM candles_daily WHERE security_id = ANY(%(ids)s) AND volume > 0
                ORDER BY security_id, date DESC
            ) SELECT * FROM latest
        """, {"ids": sec_ids})

        pm = {}
        for row in cur.fetchall():
            pm[str(row["security_id"])] = {
                "close": row["close"],
                "ma5": round(row["ma5"], 2) if row["ma5"] else None,
                "ma10": round(row["ma10"], 2) if row["ma10"] else None,
                "ma20": round(row["ma20"], 2) if row["ma20"] else None,
            }

        results = {}
        for p in positions:
            sid = str(p.get("security_id", ""))
            if sid not in pm: continue
            pd = pm[sid]
            cp = pd["close"]
            entry = float(p.get("entry_price", 0))
            risk = float(p.get("risk_pct", 4))
            lb = float(p.get("leg_base_price", 0)) or entry
            vr = float(p.get("valvo_ref_price", 0)) or entry
            rps = entry * (risk / 100.0) if risk else entry * 0.04

            defensive = "safe"
            if pd["ma5"]:
                d = ((cp - pd["ma5"]) / pd["ma5"]) * 100
                if d < -1.0: defensive = "break"
                elif d < 0: defensive = "marginal"

            results[sid] = {
                "current_price": cp, "five_ma": pd["ma5"], "ten_ma": pd["ma10"], "twenty_ma": pd["ma20"],
                "leg_extension_pct": round(((cp - lb) / lb * 100), 2) if lb else 0,
                "entry_extension_pct": round(((cp - entry) / entry * 100), 2) if entry else 0,
                "valvo_extension_pct": round(((cp - vr) / vr * 100), 2) if vr else 0,
                "r_multiple": round(((cp - entry) / rps), 2) if rps else 0,
                "defensive_status": defensive,
            }
        print(f"✅ Bulk refresh: {len(results)}/{len(positions)} positions")
        return results
    except Exception as e:
        print(f"❌ bulk_refresh error: {e}")
        return {}
    finally:
        if conn: close_db(conn)


# ═══════════════════════════════════════════════════════════════════
# CHART DATA — Full history for charting
# ═══════════════════════════════════════════════════════════════════

def get_chart_data(security_id, days=365, from_date=None, to_date=None):
    """Fetch OHLCV candles. Supports two modes:
    - days mode (default): last N days from today
    - date range mode: from_date to to_date (when both provided)

    Note: we DO NOT filter by volume > 0. Several upstream feeds occasionally
    write rows with NULL or zero volume for stocks that traded — that
    earlier filter was causing entire constituents to render "No data" in
    Bird's Eye even though OHLC was perfectly valid. close > 0 is enough
    to weed out garbage.
    """
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        if from_date and to_date:
            cur.execute("""
                SELECT date, open, high, low, close, volume
                FROM candles_daily WHERE security_id = %s AND close > 0
                  AND date >= %s AND date <= %s
                ORDER BY date ASC
            """, (str(security_id), from_date, to_date))
            result = [{"date": str(r["date"]), "open": r["open"], "high": r["high"],
                       "low": r["low"], "close": r["close"], "volume": int(r["volume"] or 0)} for r in cur.fetchall()]
        else:
            cur.execute("""
                SELECT date, open, high, low, close, volume
                FROM candles_daily WHERE security_id = %s AND close > 0
                  AND date >= (CURRENT_DATE - make_interval(days => %s))
                ORDER BY date DESC LIMIT %s
            """, (str(security_id), days, days))
            result = [{"date": str(r["date"]), "open": r["open"], "high": r["high"],
                       "low": r["low"], "close": r["close"], "volume": int(r["volume"] or 0)} for r in cur.fetchall()]
            result.reverse()  # Back to chronological ASC order
        return result
    except Exception as e:
        print(f"❌ get_chart_data error: {e}")
        return []
    finally:
        if conn: close_db(conn)


def bulk_get_chart_data(security_ids, days=252):
    if not security_ids: return {}
    ids = [str(s) for s in security_ids]
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        # Use a window function to limit rows per stock. We filter on
        # close > 0 (not volume > 0) — the volume filter was excluding
        # entire stocks whose rows happened to have NULL/zero volume in
        # the feed, even though OHLC was valid. That's what caused the
        # Bird's Eye "No data" blank tiles for most constituents.
        cur.execute("""
            SELECT security_id, date, open, high, low, close, volume
            FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) as rn
                FROM candles_daily WHERE security_id = ANY(%s) AND close > 0
            ) sub
            WHERE rn <= %s
            ORDER BY security_id, date ASC
        """, (ids, days))
        result = {}
        for r in cur.fetchall():
            sid = str(r["security_id"])
            if sid not in result: result[sid] = []
            result[sid].append({"date": str(r["date"]), "open": r["open"], "high": r["high"],
                                "low": r["low"], "close": r["close"], "volume": int(r["volume"] or 0)})
        return result
    except Exception as e:
        print(f"❌ bulk_get_chart_data error: {e}")
        return {}
    finally:
        if conn: close_db(conn)


# ═══════════════════════════════════════════════════════════════════
# STOCK UNIVERSE
# ═══════════════════════════════════════════════════════════════════

_universe_cache = None
_universe_ts = None

def load_stock_universe():
    global _universe_cache, _universe_ts
    if _universe_cache and _universe_ts and (datetime.now() - _universe_ts).total_seconds() < 86400:
        return _universe_cache
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT security_id, symbol, company_name, exchange FROM stock_universe WHERE is_active = true")
        _universe_cache = [dict(r) for r in cur.fetchall()]
        _universe_ts = datetime.now()
        print(f"📥 Loaded {len(_universe_cache)} stocks from stock_universe")
        return _universe_cache
    except Exception as e:
        print(f"❌ load_stock_universe error: {e}")
        return []
    finally:
        if conn: close_db(conn)


# ═══════════════════════════════════════════════════════════════════
# YAHOO FINANCE — market cap, sector (NOT price data)
# Yahoo Finance functions — uses yfinance library, not any broker API
# ═══════════════════════════════════════════════════════════════════

_yahoo_cache = {}
_yahoo_ttl = timedelta(hours=24)
_yahoo_fetching = set()
_yf_executor = ThreadPoolExecutor(max_workers=2)


def _fetch_yahoo_info(symbol):
    """Fetch market cap, sector, industry from Yahoo Finance."""
    if not symbol:
        return {"market_cap_cr": None, "sector": None, "industry": None}

    cached = _yahoo_cache.get(symbol)
    if cached and datetime.now() - cached["fetched_at"] < _yahoo_ttl:
        return cached["data"]

    result = {"market_cap_cr": None, "sector": None, "industry": None}
    try:
        import yfinance as yf
        future = _yf_executor.submit(lambda: yf.Ticker(f"{symbol}.NS").info)
        info = future.result(timeout=15)
        mcap = info.get("marketCap")
        if mcap and mcap > 0:
            result["market_cap_cr"] = round(mcap / 10000000, 0)
        result["sector"] = info.get("sector")
        result["industry"] = info.get("industry")
        _yahoo_cache[symbol] = {"data": result, "fetched_at": datetime.now()}
    except FuturesTimeout:
        print(f"⚠️ Yahoo fetch timed out for {symbol} (15s limit)")
        _yahoo_cache[symbol] = {"data": result, "fetched_at": datetime.now()}
    except Exception as e:
        print(f"⚠️ Yahoo fetch failed for {symbol}: {e}")
        _yahoo_cache[symbol] = {"data": result, "fetched_at": datetime.now()}

    return result


def get_yahoo_cached(symbol):
    if not symbol: return None
    cached = _yahoo_cache.get(symbol)
    return cached["data"] if cached else None


def prefetch_yahoo_background(symbol):
    if not symbol or symbol in _yahoo_fetching: return
    cached = _yahoo_cache.get(symbol)
    if cached and datetime.now() - cached["fetched_at"] < _yahoo_ttl: return
    _yahoo_fetching.add(symbol)
    def _bg():
        try: _fetch_yahoo_info(symbol)
        finally: _yahoo_fetching.discard(symbol)
    threading.Thread(target=_bg, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════
# SHARES DATA — bundled nse_shares.json (market cap + sector)
# Static data file
# ═══════════════════════════════════════════════════════════════════

SHARES_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "nse_shares.json")
_shares_cache = None

def _load_shares_data():
    global _shares_cache
    if _shares_cache is not None: return _shares_cache
    _shares_cache = {}
    try:
        if os.path.exists(SHARES_PATH):
            with open(SHARES_PATH, "r") as f:
                _shares_cache = json.load(f)
            print(f"📥 Loaded shares data for {len(_shares_cache)} stocks")
    except Exception as e:
        print(f"⚠️ Failed to load nse_shares.json: {e}")
    return _shares_cache


def get_instant_market_cap(symbol, cmp):
    if not symbol or not cmp or cmp <= 0: return None
    data = _load_shares_data()
    entry = data.get(symbol)
    if entry and entry.get("shares"):
        return round(entry["shares"] * cmp / 10000000, 0)
    return None


def get_instant_sector(symbol):
    if not symbol: return None
    data = _load_shares_data()
    entry = data.get(symbol)
    if entry and entry.get("sector"):
        return {"sector": entry["sector"], "industry": entry.get("industry")}
    return None
