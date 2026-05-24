#!/usr/bin/env python3
"""
expand_breadth_2003_2009.py — Expand breadth_daily_history back to 2003.

2001-2002 has <60 stocks — too thin for meaningful breadth.
2003 has 462 stocks — meaningful for percentage-based metrics.
Uses 2002 as EMA warmup year (1 year sufficient for 200-EMA convergence
with 250+ data points).

Only inserts rows for 2003-01-01 to 2009-12-31.
Does NOT touch existing 2010+ data.

Usage:
  cd Backend && python3 scripts/expand_breadth_2003_2009.py
  Dry-run (2003 only):
  cd Backend && python3 scripts/expand_breadth_2003_2009.py --dry-run
"""
import os, sys, json, argparse
from datetime import datetime, timedelta, timezone, date as date_type
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.database import get_db, close_db

IST = timezone(timedelta(hours=5, minutes=30))
K20  = 2.0 / 21
K50  = 2.0 / 51
K200 = 2.0 / 201

FETCH_START = "2002-01-01"  # 1 year warmup
DATA_START  = date_type(2003, 1, 1)
DATA_END    = date_type(2009, 12, 31)


def log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        log("=== DRY RUN — only 2003, no DB writes ===")

    log("=== Breadth Expansion: 2003-2009 ===")
    log(f"Fetch from: {FETCH_START} | Emit: {DATA_START} to {DATA_END}")

    conn = get_db()
    try:
        cur = conn.cursor()

        # Check existing
        cur.execute("SELECT COUNT(*) as cnt FROM breadth_daily_history WHERE date < '2010-01-01'")
        existing = cur.fetchone()["cnt"]
        log(f"Existing pre-2010 rows: {existing}")

        # Fetch candles
        log("Fetching candles...")
        cur.execute("SET LOCAL statement_timeout = '300000'")
        cur.execute("""
            SELECT cd.security_id, cd.date, cd.open, cd.high, cd.low, cd.close, cd.volume,
                   COALESCE(su.sector, '') as sector
            FROM candles_daily cd
            JOIN stock_universe su ON cd.security_id = su.security_id
            WHERE cd.date >= %s AND cd.date <= %s
              AND cd.volume > 0
              AND COALESCE(su.is_etf, false) = false
            ORDER BY cd.security_id, cd.date
        """, (FETCH_START, str(DATA_END)))
        rows = cur.fetchall()
        log(f"Fetched {len(rows):,} rows")

        if not rows:
            log("No data. Exiting.")
            return

        # Organize per-stock
        stocks = defaultdict(list)
        for r in rows:
            stocks[r["security_id"]].append({
                "date": r["date"],
                "high": float(r["high"] or 0),
                "low": float(r["low"] or 0),
                "close": float(r["close"] or 0),
                "sector": r["sector"],
            })
        log(f"Organized {len(stocks)} stocks")

        # Compute EMAs
        log("Computing EMAs...")
        stock_emas = {}
        for sid, candles in stocks.items():
            if len(candles) < 10:
                continue
            ema20 = candles[0]["close"]
            ema50 = candles[0]["close"]
            ema200 = candles[0]["close"]
            series = []
            for i, c in enumerate(candles):
                cl = c["close"]
                if cl <= 0:
                    continue
                ema20 = cl * K20 + ema20 * (1 - K20)
                ema50 = cl * K50 + ema50 * (1 - K50)
                ema200 = cl * K200 + ema200 * (1 - K200)
                prev_close = candles[i - 1]["close"] if i > 0 else cl
                series.append({
                    "date": c["date"], "close": cl, "high": c["high"], "low": c["low"],
                    "prev_close": prev_close, "ema20": ema20, "ema50": ema50, "ema200": ema200,
                    "sector": c["sector"],
                })
            stock_emas[sid] = series
        log(f"EMAs computed for {len(stock_emas)} stocks")

        # Rolling 252-day highs/lows
        log("Computing rolling 252-day highs/lows...")
        stock_rolling = {}
        for sid, series in stock_emas.items():
            highs, lows = [], []
            for i in range(len(series)):
                window = series[max(0, i - 252):i]
                if window:
                    highs.append(max(e["high"] for e in window))
                    lows.append(min(e["low"] for e in window))
                else:
                    highs.append(series[i]["high"])
                    lows.append(series[i]["low"])
            stock_rolling[sid] = (highs, lows)

        # Build date index
        effective_end = date_type(2003, 12, 31) if args.dry_run else DATA_END
        log(f"Building date index ({DATA_START} to {effective_end})...")
        date_stocks = defaultdict(list)
        for sid, series in stock_emas.items():
            h_list, l_list = stock_rolling.get(sid, ([], []))
            for i in range(len(series)):
                d = series[i]["date"]
                if d < DATA_START or d > effective_end:
                    continue
                entry = series[i].copy()
                entry["high_252"] = h_list[i] if i < len(h_list) else entry["high"]
                entry["low_252"] = l_list[i] if i < len(l_list) else entry["low"]
                date_stocks[d].append(entry)

        all_dates = sorted(date_stocks.keys())
        log(f"{len(all_dates)} trading dates to process")

        # Aggregate
        log("Aggregating breadth...")
        breadth_rows = []
        for d in all_dates:
            entries = date_stocks[d]
            if len(entries) < 30:  # lower threshold for early years
                continue

            total = len(entries)
            above_ema20 = sum(1 for e in entries if e["close"] > e["ema20"])
            above_ema50 = sum(1 for e in entries if e["close"] > e["ema50"])
            above_ema200 = sum(1 for e in entries if e["close"] > e["ema200"])
            new_highs = sum(1 for e in entries if e["high"] > e["high_252"])
            new_lows = sum(1 for e in entries if e["low"] < e["low_252"])
            advances = sum(1 for e in entries if e["close"] > e["prev_close"])
            declines = sum(1 for e in entries if e["close"] < e["prev_close"])

            down_20 = sum(1 for e in entries if (e["close"] - max(e["high_252"], e["high"])) / max(e["high_252"], e["high"], 1) < -0.20)
            down_30 = sum(1 for e in entries if (e["close"] - max(e["high_252"], e["high"])) / max(e["high_252"], e["high"], 1) < -0.30)
            down_50 = sum(1 for e in entries if (e["close"] - max(e["high_252"], e["high"])) / max(e["high_252"], e["high"], 1) < -0.50)

            ad_total = advances + declines
            thrust = round(100.0 * advances / ad_total, 1) if ad_total > 0 else 50.0
            momentum = sum(1 for e in entries if e["prev_close"] > 0 and abs(e["close"] - e["prev_close"]) / e["prev_close"] > 0.20)

            sector_data = defaultdict(lambda: {"total": 0, "above20": 0, "above50": 0, "above200": 0})
            for e in entries:
                sec = e["sector"]
                if not sec: continue
                sector_data[sec]["total"] += 1
                if e["close"] > e["ema20"]: sector_data[sec]["above20"] += 1
                if e["close"] > e["ema50"]: sector_data[sec]["above50"] += 1
                if e["close"] > e["ema200"]: sector_data[sec]["above200"] += 1

            sector_json = {sec: {"total": sd["total"],
                "above20": round(100.0 * sd["above20"] / sd["total"], 1),
                "above50": round(100.0 * sd["above50"] / sd["total"], 1),
                "above200": round(100.0 * sd["above200"] / sd["total"], 1)}
                for sec, sd in sector_data.items() if sd["total"] >= 3}

            breadth_rows.append((d, total,
                round(100.0 * above_ema20 / total, 1), round(100.0 * above_ema50 / total, 1),
                round(100.0 * above_ema200 / total, 1), new_highs, new_lows,
                round(100.0 * down_20 / total, 1), round(100.0 * down_30 / total, 1),
                round(100.0 * down_50 / total, 1), advances, declines, thrust,
                momentum, json.dumps(sector_json)))

        log(f"Computed {len(breadth_rows)} breadth rows")

        # Print samples
        if breadth_rows:
            samples = [breadth_rows[0], breadth_rows[len(breadth_rows)//2], breadth_rows[-1]]
            for row in samples:
                log(f"  {row[0]} | stocks={row[1]} | adv={row[10]} dec={row[11]} | "
                    f"ema20={row[2]}% ema50={row[3]}% ema200={row[4]}% | highs={row[5]} lows={row[6]}")

        if args.dry_run:
            log(f"=== DRY RUN COMPLETE — would insert {len(breadth_rows)} rows ===")
            return

        # Insert
        log("Inserting...")
        cur.execute("SET LOCAL statement_timeout = '120000'")
        inserted = 0
        batch_size = 50
        for i in range(0, len(breadth_rows), batch_size):
            batch = breadth_rows[i:i + batch_size]
            values_sql = ",".join(cur.mogrify(
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)", row
            ).decode() for row in batch)
            cur.execute(f"""
                INSERT INTO breadth_daily_history
                    (date, total_stocks, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                     new_highs, new_lows, pct_down_20, pct_down_30, pct_down_50,
                     advance_count, decline_count, thrust, momentum_20pc, sector_breadth)
                VALUES {values_sql}
                ON CONFLICT (date) DO UPDATE SET
                    total_stocks=EXCLUDED.total_stocks, pct_above_ema20=EXCLUDED.pct_above_ema20,
                    pct_above_ema50=EXCLUDED.pct_above_ema50, pct_above_ema200=EXCLUDED.pct_above_ema200,
                    new_highs=EXCLUDED.new_highs, new_lows=EXCLUDED.new_lows,
                    pct_down_20=EXCLUDED.pct_down_20, pct_down_30=EXCLUDED.pct_down_30,
                    pct_down_50=EXCLUDED.pct_down_50, advance_count=EXCLUDED.advance_count,
                    decline_count=EXCLUDED.decline_count, thrust=EXCLUDED.thrust,
                    momentum_20pc=EXCLUDED.momentum_20pc, sector_breadth=EXCLUDED.sector_breadth,
                    computed_at=NOW()
            """)
            inserted += len(batch)
            if inserted % 500 == 0:
                log(f"  ... {inserted}/{len(breadth_rows)}")
        conn.commit()
        log(f"Inserted {inserted} rows")

        # Verify
        cur.execute("SELECT COUNT(*) as total, MIN(date) as earliest, MAX(date) as latest FROM breadth_daily_history")
        v = cur.fetchone()
        log(f"Total: {v['total']} rows | {v['earliest']} to {v['latest']}")
        log("=== Done ===")

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback; traceback.print_exc()
        conn.rollback()
    finally:
        close_db(conn)


if __name__ == "__main__":
    main()
