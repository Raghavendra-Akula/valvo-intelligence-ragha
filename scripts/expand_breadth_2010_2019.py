#!/usr/bin/env python3
"""
expand_breadth_2010_2019.py — Expand breadth_daily_history back to Jan 2010.

This is a ONE-TIME expansion script. It ONLY inserts rows for dates
that don't already exist in breadth_daily_history (2010-01-01 to 2019-12-31).
It does NOT touch or overwrite existing 2020+ data.

Data landscape:
  - candles_daily has data from 2001, but meaningful coverage starts ~2003
  - 2009: 850 stocks (warmup year for EMA convergence)
  - 2010: 933 stocks → our DATA_START
  - 2019: 1,489 stocks → end of expansion range
  - Total rows to fetch: ~3.3M (2008-2019)
  - Total breadth rows to generate: ~2,500 (2010-2019, ~250 trading days/year)
  - RAM needed: ~700MB for Python dicts (fits in 2GB easily)

EMA warmup strategy:
  - Fetch from 2008-01-01 (2 years before DATA_START for 200-EMA to converge)
  - EMA200 needs ~500 data points to be within 1% of true value
  - 2 years = ~500 trading days — sufficient warmup

Usage:
  cd Backend && python3 scripts/expand_breadth_2010_2019.py

  Optional dry-run (only 2010, no DB writes):
  cd Backend && python3 scripts/expand_breadth_2010_2019.py --dry-run
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

# Fetch from 2008 for EMA warmup. Emit breadth from 2010 to 2019.
FETCH_START = "2008-01-01"
DATA_START  = date_type(2010, 1, 1)
DATA_END    = date_type(2019, 12, 31)  # don't touch 2020+ data


def log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Only compute 2010, print stats, don't write to DB")
    args = parser.parse_args()

    if args.dry_run:
        log("=== DRY RUN MODE — only computing 2010, no DB writes ===")

    log("=== Breadth Expansion: 2010-2019 ===")
    log(f"Fetch from: {FETCH_START}")
    log(f"Emit range: {DATA_START} to {DATA_END}")

    conn = get_db()
    try:
        cur = conn.cursor()

        # ── Step 1: Check what already exists ──
        cur.execute("SELECT COUNT(*) as cnt FROM breadth_daily_history WHERE date < '2020-01-01'")
        existing = cur.fetchone()["cnt"]
        if existing > 0 and not args.dry_run:
            log(f"WARNING: {existing} pre-2020 rows already exist. They will be UPDATED (ON CONFLICT).")

        # ── Step 2: Fetch historical candles ──
        log(f"Fetching candles from {FETCH_START} to {DATA_END} ...")
        cur.execute("SET LOCAL statement_timeout = '300000'")  # 5 min
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
        log(f"Fetched {len(rows):,} candle rows")

        if not rows:
            log("No data found. Exiting.")
            return

        # ── Step 3: Organize per-stock time series ──
        log("Organizing per-stock time series...")
        stocks = defaultdict(list)
        for r in rows:
            stocks[r["security_id"]].append({
                "date": r["date"],
                "open": float(r["open"] or 0),
                "high": float(r["high"] or 0),
                "low": float(r["low"] or 0),
                "close": float(r["close"] or 0),
                "volume": int(r["volume"] or 0),
                "sector": r["sector"],
            })
        log(f"Organized {len(stocks)} stocks")

        # ── Step 4: Compute EMAs per stock ──
        log("Computing EMAs iteratively for each stock...")
        stock_emas = {}
        for sid, candles in stocks.items():
            if len(candles) < 10:  # need at least some history
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
                    "date": c["date"],
                    "close": cl,
                    "high": c["high"],
                    "low": c["low"],
                    "prev_close": prev_close,
                    "ema20": ema20,
                    "ema50": ema50,
                    "ema200": ema200,
                    "sector": c["sector"],
                })

            stock_emas[sid] = series
        log(f"Computed EMAs for {len(stock_emas)} stocks")

        # ── Step 5: Precompute rolling 252-day highs/lows per stock ──
        log("Computing rolling 252-day highs and lows...")
        stock_rolling = {}
        for sid, series in stock_emas.items():
            highs_252 = []
            lows_252 = []
            for i in range(len(series)):
                start = max(0, i - 252)
                window = series[start:i]  # exclude current day
                if window:
                    h252 = max(e["high"] for e in window)
                    l252 = min(e["low"] for e in window)
                else:
                    h252 = series[i]["high"]
                    l252 = series[i]["low"]
                highs_252.append(h252)
                lows_252.append(l252)
            stock_rolling[sid] = (highs_252, lows_252)

        # ── Step 6: Build date-indexed lookup (only DATA_START to DATA_END) ──
        log(f"Building date index from {DATA_START} to {DATA_END}...")
        effective_end = date_type(2010, 12, 31) if args.dry_run else DATA_END

        date_stocks = defaultdict(list)
        for sid, series in stock_emas.items():
            h252_list, l252_list = stock_rolling.get(sid, ([], []))
            for i in range(len(series)):
                d = series[i]["date"]
                if d < DATA_START or d > effective_end:
                    continue
                entry = series[i].copy()
                entry["high_252"] = h252_list[i] if i < len(h252_list) else entry["high"]
                entry["low_252"] = l252_list[i] if i < len(l252_list) else entry["low"]
                date_stocks[d].append(entry)

        all_dates = sorted(date_stocks.keys())
        log(f"Found {len(all_dates)} trading dates to process")

        # ── Step 7: Aggregate breadth per date ──
        log("Aggregating breadth metrics per date...")
        breadth_rows = []

        for d in all_dates:
            entries = date_stocks[d]
            if len(entries) < 50:  # need meaningful sample
                continue

            total = len(entries)
            above_ema20 = sum(1 for e in entries if e["close"] > e["ema20"])
            above_ema50 = sum(1 for e in entries if e["close"] > e["ema50"])
            above_ema200 = sum(1 for e in entries if e["close"] > e["ema200"])

            # New highs: today's HIGH exceeds prior 252-day high
            new_highs = sum(1 for e in entries if e["high"] > e["high_252"])
            # New lows: today's LOW breaks below prior 252-day low
            new_lows = sum(1 for e in entries if e["low"] < e["low_252"])

            advances = sum(1 for e in entries if e["close"] > e["prev_close"])
            declines = sum(1 for e in entries if e["close"] < e["prev_close"])

            # Fall from 52W high
            down_20 = sum(1 for e in entries
                          if (e["close"] - max(e["high_252"], e["high"])) / max(e["high_252"], e["high"], 1) < -0.20)
            down_30 = sum(1 for e in entries
                          if (e["close"] - max(e["high_252"], e["high"])) / max(e["high_252"], e["high"], 1) < -0.30)
            down_50 = sum(1 for e in entries
                          if (e["close"] - max(e["high_252"], e["high"])) / max(e["high_252"], e["high"], 1) < -0.50)

            # Thrust
            ad_total = advances + declines
            thrust = round(100.0 * advances / ad_total, 1) if ad_total > 0 else 50.0

            # Momentum movers: >20% move from prev_close
            momentum = sum(1 for e in entries
                           if e["prev_close"] > 0 and abs(e["close"] - e["prev_close"]) / e["prev_close"] > 0.20)

            # Sector breadth
            sector_data = defaultdict(lambda: {"total": 0, "above20": 0, "above50": 0, "above200": 0})
            for e in entries:
                sec = e["sector"]
                if not sec:
                    continue
                sector_data[sec]["total"] += 1
                if e["close"] > e["ema20"]:
                    sector_data[sec]["above20"] += 1
                if e["close"] > e["ema50"]:
                    sector_data[sec]["above50"] += 1
                if e["close"] > e["ema200"]:
                    sector_data[sec]["above200"] += 1

            sector_json = {}
            for sec, sd in sector_data.items():
                if sd["total"] >= 5:
                    sector_json[sec] = {
                        "total": sd["total"],
                        "above20": round(100.0 * sd["above20"] / sd["total"], 1),
                        "above50": round(100.0 * sd["above50"] / sd["total"], 1),
                        "above200": round(100.0 * sd["above200"] / sd["total"], 1),
                    }

            breadth_rows.append((
                d, total,
                round(100.0 * above_ema20 / total, 1),
                round(100.0 * above_ema50 / total, 1),
                round(100.0 * above_ema200 / total, 1),
                new_highs, new_lows,
                round(100.0 * down_20 / total, 1),
                round(100.0 * down_30 / total, 1),
                round(100.0 * down_50 / total, 1),
                advances, declines, thrust,
                momentum,
                json.dumps(sector_json),
            ))

        log(f"Computed {len(breadth_rows)} breadth rows")

        # ── Step 8: Print sample rows for verification ──
        if breadth_rows:
            log("── Sample rows for verification ──")
            # Show first, middle, and last
            samples = [breadth_rows[0], breadth_rows[len(breadth_rows)//2], breadth_rows[-1]]
            for row in samples:
                log(f"  {row[0]} | stocks={row[1]} | adv={row[10]} dec={row[11]} | "
                    f"ema20={row[2]}% ema50={row[3]}% ema200={row[4]}% | "
                    f"highs={row[5]} lows={row[6]} | thrust={row[12]}%")

        if args.dry_run:
            log("=== DRY RUN COMPLETE — no DB writes ===")
            log(f"Would insert {len(breadth_rows)} rows for 2010")
            return

        # ── Step 9: Insert into breadth_daily_history ──
        log("Inserting into breadth_daily_history...")
        cur.execute("SET LOCAL statement_timeout = '120000'")
        batch_size = 50
        inserted = 0
        for i in range(0, len(breadth_rows), batch_size):
            batch = breadth_rows[i:i + batch_size]
            values_sql = ",".join(
                cur.mogrify(
                    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                    row
                ).decode() for row in batch
            )
            cur.execute(f"""
                INSERT INTO breadth_daily_history
                    (date, total_stocks,
                     pct_above_ema20, pct_above_ema50, pct_above_ema200,
                     new_highs, new_lows,
                     pct_down_20, pct_down_30, pct_down_50,
                     advance_count, decline_count, thrust,
                     momentum_20pc, sector_breadth)
                VALUES {values_sql}
                ON CONFLICT (date) DO UPDATE SET
                    total_stocks = EXCLUDED.total_stocks,
                    pct_above_ema20 = EXCLUDED.pct_above_ema20,
                    pct_above_ema50 = EXCLUDED.pct_above_ema50,
                    pct_above_ema200 = EXCLUDED.pct_above_ema200,
                    new_highs = EXCLUDED.new_highs,
                    new_lows = EXCLUDED.new_lows,
                    pct_down_20 = EXCLUDED.pct_down_20,
                    pct_down_30 = EXCLUDED.pct_down_30,
                    pct_down_50 = EXCLUDED.pct_down_50,
                    advance_count = EXCLUDED.advance_count,
                    decline_count = EXCLUDED.decline_count,
                    thrust = EXCLUDED.thrust,
                    momentum_20pc = EXCLUDED.momentum_20pc,
                    sector_breadth = EXCLUDED.sector_breadth,
                    computed_at = NOW()
            """)
            inserted += len(batch)
            if inserted % 500 == 0:
                log(f"  ... inserted {inserted}/{len(breadth_rows)}")
        conn.commit()
        log(f"Inserted/updated {inserted} rows")

        # ── Step 10: Verify ──
        cur.execute("""
            SELECT COUNT(*) as total,
                   MIN(date) as earliest, MAX(date) as latest,
                   COUNT(*) FILTER (WHERE date < '2020-01-01') as pre_2020
            FROM breadth_daily_history
        """)
        v = cur.fetchone()
        log(f"Verification: {v['total']} total rows, earliest={v['earliest']}, latest={v['latest']}, pre-2020={v['pre_2020']}")
        log("=== Expansion complete ===")

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        conn.rollback()
    finally:
        close_db(conn)


if __name__ == "__main__":
    main()
