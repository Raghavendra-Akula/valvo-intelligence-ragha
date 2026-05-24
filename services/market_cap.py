"""
Unified market cap service.

Market cap = shares_outstanding × price / 1e7 (in Crores)

- shares_outstanding: stored in stock_universe (refreshed monthly from Yahoo)
- price: from candles_daily (updated daily)

Usage:
    from services.market_cap import get_market_cap, get_historical_market_cap

    mcap = get_market_cap("IRFC")                          # current
    mcap = get_historical_market_cap("IRFC", "2023-12-15") # historical
"""
from __future__ import annotations


def get_market_cap(symbol: str, conn=None) -> float | None:
    """Get current market cap in Crores using latest close price."""
    return get_historical_market_cap(symbol, date=None, conn=conn)


def get_historical_market_cap(symbol: str, date: str | None = None, conn=None) -> float | None:
    """
    Get market cap in Crores at a specific date.
    If date is None, uses latest available price.
    Returns None if shares_outstanding is not available.
    """
    should_close = False
    if not conn:
        from database.database import get_db
        conn = get_db()
        should_close = True
    if not conn:
        return None

    try:
        cur = conn.cursor()

        # Get shares outstanding
        cur.execute(
            "SELECT security_id, shares_outstanding FROM stock_universe WHERE symbol = %s LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()
        if not row or not row.get("shares_outstanding"):
            # Fallback: try Yahoo Finance live
            return _yahoo_fallback(symbol, date, cur)

        sec_id = row["security_id"]
        shares = row["shares_outstanding"]

        # Get price at date (or latest)
        if date:
            cur.execute(
                "SELECT close FROM candles_daily WHERE security_id = %s AND date <= %s ORDER BY date DESC LIMIT 1",
                (sec_id, date),
            )
        else:
            cur.execute(
                "SELECT close FROM candles_daily WHERE security_id = %s ORDER BY date DESC LIMIT 1",
                (sec_id,),
            )

        price_row = cur.fetchone()
        if not price_row:
            return None

        price = float(price_row["close"])
        return round(shares * price / 1e7, 2)  # Crores

    except Exception:
        return None
    finally:
        if should_close and conn:
            try:
                conn.close()
            except Exception:
                pass


def get_market_cap_by_security_id(security_id: str, date: str | None = None, conn=None) -> float | None:
    """Same as get_historical_market_cap but takes security_id directly."""
    should_close = False
    if not conn:
        from database.database import get_db
        conn = get_db()
        should_close = True
    if not conn:
        return None

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT shares_outstanding, symbol FROM stock_universe WHERE security_id = %s LIMIT 1",
            (security_id,),
        )
        row = cur.fetchone()
        if not row or not row.get("shares_outstanding"):
            sym = row["symbol"] if row else None
            return _yahoo_fallback(sym, date, cur) if sym else None

        shares = row["shares_outstanding"]

        if date:
            cur.execute(
                "SELECT close FROM candles_daily WHERE security_id = %s AND date <= %s ORDER BY date DESC LIMIT 1",
                (security_id, date),
            )
        else:
            cur.execute(
                "SELECT close FROM candles_daily WHERE security_id = %s ORDER BY date DESC LIMIT 1",
                (security_id,),
            )

        price_row = cur.fetchone()
        if not price_row:
            return None

        return round(shares * float(price_row["close"]) / 1e7, 2)

    except Exception:
        return None
    finally:
        if should_close and conn:
            try:
                conn.close()
            except Exception:
                pass


def _yahoo_fallback(symbol: str | None, date: str | None, cur) -> float | None:
    """Last resort: fetch from Yahoo Finance if shares not in DB."""
    if not symbol:
        return None
    try:
        import yfinance as yf
        info = yf.Ticker(f"{symbol}.NS").info
        mcap = info.get("marketCap")
        if not mcap or mcap <= 0:
            return None

        current_mcap_cr = round(mcap / 1e7, 2)

        # If historical date requested, scale by price ratio
        if date:
            cur.execute(
                "SELECT security_id FROM stock_universe WHERE symbol = %s LIMIT 1",
                (symbol,),
            )
            sid_row = cur.fetchone()
            if sid_row:
                cur.execute(
                    "SELECT close FROM candles_daily WHERE security_id = %s AND date <= %s ORDER BY date DESC LIMIT 1",
                    (sid_row["security_id"], date),
                )
                old_price = cur.fetchone()
                cur.execute(
                    "SELECT close FROM candles_daily WHERE security_id = %s ORDER BY date DESC LIMIT 1",
                    (sid_row["security_id"],),
                )
                new_price = cur.fetchone()
                if old_price and new_price:
                    ratio = float(old_price["close"]) / float(new_price["close"])
                    return round(current_mcap_cr * ratio, 2)

        return current_mcap_cr
    except Exception:
        return None
