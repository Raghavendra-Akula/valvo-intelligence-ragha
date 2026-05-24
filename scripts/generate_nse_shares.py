"""
generate_nse_shares.py — Run ONCE to build shares outstanding + sector data

Reads nse_equities.csv, fetches sharesOutstanding + sector from Yahoo Finance,
saves as nse_shares.json. This file gets bundled with the backend.

Usage:
    pip install yfinance
    python generate_nse_shares.py

Takes ~30-60 minutes for ~3500 stocks. Run monthly to catch new listings + splits.
"""
import csv
import json
import os
import time
import sys

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "nse_equities.csv")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "nse_shares.json")

def main():
    import yfinance as yf

    # Load symbols from CSV
    symbols = []
    with open(CSV_PATH, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = (row.get("SYMBOL") or row.get("SEM_TRADING_SYMBOL") or "").strip()
            if sym:
                symbols.append(sym)

    print(f"📥 Loaded {len(symbols)} symbols from CSV")

    # Load existing data (resume-friendly)
    existing = {}
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, "r") as f:
            existing = json.load(f)
        print(f"📂 Loaded {len(existing)} existing entries — will skip these")

    result = dict(existing)
    total = len(symbols)
    done = 0
    errors = 0

    for i, sym in enumerate(symbols):
        if sym in result:
            done += 1
            continue

        try:
            ticker = yf.Ticker(f"{sym}.NS")
            info = ticker.info

            shares = info.get("sharesOutstanding")
            sector = info.get("sector")
            industry = info.get("industry")
            mcap = info.get("marketCap")

            if shares and shares > 0:
                result[sym] = {
                    "shares": shares,
                    "sector": sector,
                    "industry": industry,
                }
                done += 1
            else:
                # Try floatShares as fallback
                float_shares = info.get("floatShares")
                if float_shares and float_shares > 0:
                    result[sym] = {
                        "shares": float_shares,
                        "sector": sector,
                        "industry": industry,
                    }
                    done += 1
                else:
                    # Store even without shares — at least get sector
                    if sector:
                        result[sym] = {
                            "shares": None,
                            "sector": sector,
                            "industry": industry,
                        }
                    errors += 1

        except Exception as e:
            errors += 1
            if "Too Many Requests" in str(e) or "429" in str(e):
                print(f"⚠️ Rate limited at {sym} — waiting 60s...")
                time.sleep(60)
            else:
                pass  # Skip silently

        # Progress
        if (i + 1) % 50 == 0:
            print(f"   [{i+1}/{total}] done={done}, errors={errors}, latest={sym}")
            # Save checkpoint every 50 stocks
            with open(OUTPUT_PATH, "w") as f:
                json.dump(result, f)

        # Rate limit — ~2 per second
        time.sleep(0.5)

    # Final save
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f)

    print(f"\n✅ Done! {done} stocks with data, {errors} errors")
    print(f"📁 Saved to {OUTPUT_PATH}")

    # Stats
    with_shares = sum(1 for v in result.values() if v.get("shares"))
    with_sector = sum(1 for v in result.values() if v.get("sector"))
    print(f"   Shares data: {with_shares}")
    print(f"   Sector data: {with_sector}")


if __name__ == "__main__":
    main()