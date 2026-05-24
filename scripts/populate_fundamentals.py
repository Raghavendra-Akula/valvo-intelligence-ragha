"""
Populate shares_outstanding in stock_universe from Yahoo Finance.
Market cap = shares_outstanding × price (computed on the fly for any date).

Usage: python scripts/populate_fundamentals.py [--limit 100] [--offset 0]
Run monthly or after stock splits to keep shares_outstanding current.
"""
import os
import sys
import time
import argparse
from dotenv import load_dotenv

# Load Backend/.env so the script picks up the same DB credentials
# the main app uses (DB_HOST, DB_USER, DB_PASSWORD, DB_PORT, etc.).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def get_db():
    import psycopg2
    import psycopg2.extras
    # Read the same env vars Backend/database/database.py reads, with the
    # same defaults — so the script picks up whatever .env the main app
    # is already configured with (port 6543 = Supabase transaction pooler).
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        dbname=os.getenv("DB_NAME", "postgres"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD"),
        port=os.getenv("DB_PORT", "6543"),
        sslmode="require",
    )
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def fetch_shares_batch(symbols):
    """Fetch shares outstanding for symbols from yfinance."""
    import yfinance as yf
    results = {}
    nse_syms = " ".join(f"{s}.NS" for s in symbols)
    try:
        tickers = yf.Tickers(nse_syms)
        for sym in symbols:
            try:
                info = tickers.tickers[f"{sym}.NS"].info
                shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
                sector = info.get("sector")
                industry = info.get("industry")
                if shares and shares > 0:
                    results[sym] = {"shares": int(shares), "sector": sector, "industry": industry}
            except Exception:
                pass
    except Exception as e:
        print(f"  Batch error: {e}")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=2500)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--missing-only", action="store_true", help="Only stocks without shares_outstanding")
    args = parser.parse_args()

    conn = get_db()
    cur = conn.cursor()

    if args.missing_only:
        cur.execute("""
            SELECT security_id, symbol FROM stock_universe
            WHERE is_active = true AND shares_outstanding IS NULL
            ORDER BY symbol LIMIT %s OFFSET %s
        """, (args.limit, args.offset))
    else:
        cur.execute("""
            SELECT security_id, symbol FROM stock_universe
            WHERE is_active = true ORDER BY symbol LIMIT %s OFFSET %s
        """, (args.limit, args.offset))

    stocks = cur.fetchall()
    print(f"Processing {len(stocks)} stocks...")

    total = 0
    for i in range(0, len(stocks), 20):
        batch = stocks[i:i + 20]
        syms = [s["symbol"] for s in batch]
        print(f"  Batch {i // 20 + 1}: {syms[0]}...{syms[-1]}")

        results = fetch_shares_batch(syms)
        for s in batch:
            data = results.get(s["symbol"])
            if not data:
                continue
            try:
                cur.execute("""
                    UPDATE stock_universe SET
                        shares_outstanding = %s,
                        sector = COALESCE(%s, sector),
                        industry = COALESCE(%s, industry),
                        fundamentals_updated_at = NOW()
                    WHERE security_id = %s
                """, (data["shares"], data.get("sector"), data.get("industry"), s["security_id"]))
                total += 1
            except Exception as e:
                print(f"    Error {s['symbol']}: {e}")
                conn.rollback()

        conn.commit()
        print(f"    Got {len(results)}/{len(syms)}")
        time.sleep(1)

    print(f"\nDone. Updated {total}/{len(stocks)}")
    conn.close()


if __name__ == "__main__":
    main()
