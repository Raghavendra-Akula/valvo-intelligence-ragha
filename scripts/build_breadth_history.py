#!/usr/bin/env python3
"""
build_breadth_history.py — One-time backfill for breadth_daily_history + seed EMA columns.

Computes EMA20/50/200 for all stocks iteratively over ~750 trading days,
aggregates daily breadth metrics, and inserts into breadth_daily_history.
Also seeds stock_daily_summary.ema20/50/200 with the latest computed values.

Usage:  cd Backend && python scripts/build_breadth_history.py
"""
import os, sys, json, time
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Add parent dir so we can import database module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database.database import get_db, close_db

IST = timezone(timedelta(hours=5, minutes=30))
K20 = 2.0 / 21   # 0.095238
K50 = 2.0 / 51   # 0.039216
K200 = 2.0 / 201  # 0.009950
WARMUP_DAYS = 250  # skip first 250 days for EMA convergence
FETCH_START = "2019-01-01"  # 1 year before 2020 for EMA warmup
DATA_START = "2020-01-01"   # actual breadth data starts here

def log(msg):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def main():
    log("=== Breadth History Backfill ===")

    conn = get_db()
    try:
        cur = conn.cursor()

        # ── Step 1: Fetch all daily data from 2019 (warmup) onward ──
        log(f"Fetching candles_daily + stock_universe from {FETCH_START}...")
        cur.execute("SET LOCAL statement_timeout = '300000'")  # 5 min for large fetch
        cur.execute("""
            SELECT cd.security_id, cd.date, cd.open, cd.high, cd.low, cd.close, cd.volume,
                   su.sector
            FROM candles_daily cd
            JOIN stock_universe su ON cd.security_id = su.security_id
            WHERE cd.date >= %s
              AND cd.volume > 0
              AND COALESCE(su.is_etf, false) = false
            ORDER BY cd.security_id, cd.date
        """, (FETCH_START,))
        rows = cur.fetchall()
        log(f"Fetched {len(rows):,} candle rows")

        if not rows:
            log("No data found. Exiting.")
            return

        # ── Step 2: Organize per-stock time series ──
        stocks = defaultdict(list)
        for r in rows:
            stocks[r["security_id"]].append({
                "date": r["date"],
                "open": float(r["open"] or 0),
                "high": float(r["high"] or 0),
                "low": float(r["low"] or 0),
                "close": float(r["close"] or 0),
                "volume": int(r["volume"] or 0),
                "sector": r["sector"] or "",
            })
        log(f"Organized {len(stocks)} stocks into time series")

        # ── Step 3: Compute EMAs per stock ──
        # Result: per-stock, per-date EMA values + price data
        # stock_emas[sid] = [{date, close, high, low, prev_close, ema20, ema50, ema200, sector}, ...]
        stock_emas = {}
        final_emas = {}  # for seeding stock_daily_summary

        for sid, candles in stocks.items():
            if len(candles) < 50:
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
                prev_close = candles[i-1]["close"] if i > 0 else cl
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
            if series:
                last = series[-1]
                final_emas[sid] = (last["ema20"], last["ema50"], last["ema200"])

        log(f"Computed EMAs for {len(stock_emas)} stocks")

        # ── Step 4: Collect all trading dates from DATA_START onward ──
        from datetime import date as date_type
        data_start_date = date_type.fromisoformat(DATA_START)
        all_dates = set()
        for sid, series in stock_emas.items():
            for entry in series:
                if entry["date"] >= data_start_date:
                    all_dates.add(entry["date"])
        all_dates = sorted(all_dates)
        log(f"Found {len(all_dates)} trading dates from {DATA_START} onward")

        # Build date-indexed lookup: date -> list of stock entries (from DATA_START)
        date_stocks = defaultdict(list)
        for sid, series in stock_emas.items():
            for entry in series:
                if entry["date"] >= data_start_date:
                    date_stocks[entry["date"]].append(entry)

        # Also need rolling 252-day highs/lows per stock per date
        # Precompute high_252 and low_252 using the full series
        stock_rolling = {}
        for sid, series in stock_emas.items():
            highs_252 = []
            lows_252 = []
            for i in range(len(series)):
                start = max(0, i - 251)
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

        # Rebuild date_stocks with rolling high/low (only from DATA_START)
        date_stocks_full = defaultdict(list)
        for sid, series in stock_emas.items():
            h252_list, l252_list = stock_rolling.get(sid, ([], []))
            for i in range(len(series)):
                if series[i]["date"] < data_start_date:
                    continue
                entry = series[i].copy()
                entry["high_252"] = h252_list[i] if i < len(h252_list) else entry["high"]
                entry["low_252"] = l252_list[i] if i < len(l252_list) else entry["low"]
                date_stocks_full[entry["date"]].append(entry)

        # ── Step 5: Aggregate breadth per date ──
        log("Aggregating breadth metrics per date...")
        breadth_rows = []

        for d in all_dates:
            entries = date_stocks_full.get(d, [])
            if len(entries) < 100:
                continue

            total = len(entries)
            above_ema20 = sum(1 for e in entries if e["close"] > e["ema20"])
            above_ema50 = sum(1 for e in entries if e["close"] > e["ema50"])
            above_ema200 = sum(1 for e in entries if e["close"] > e["ema200"])
            new_highs = sum(1 for e in entries if e["high"] > e["high_252"])
            new_lows = sum(1 for e in entries if e["low"] < e["low_252"])
            advances = sum(1 for e in entries if e["close"] > e["prev_close"])
            declines = sum(1 for e in entries if e["close"] < e["prev_close"])

            # Fall from 52W high distribution
            down_20 = sum(1 for e in entries if (e["close"] - max(e["high_252"], e["high"])) / max(e["high_252"], e["high"], 1) < -0.20)
            down_30 = sum(1 for e in entries if (e["close"] - max(e["high_252"], e["high"])) / max(e["high_252"], e["high"], 1) < -0.30)
            down_50 = sum(1 for e in entries if (e["close"] - max(e["high_252"], e["high"])) / max(e["high_252"], e["high"], 1) < -0.50)

            # Thrust
            ad_total = advances + declines
            thrust = round(100.0 * advances / ad_total, 1) if ad_total > 0 else 50.0

            # Momentum movers: >20% move from prev_close
            momentum = sum(1 for e in entries if e["prev_close"] > 0 and abs(e["close"] - e["prev_close"]) / e["prev_close"] > 0.20)

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

            # Convert counts to percentages
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

        # ── Step 6: Insert into breadth_daily_history ──
        log("Inserting into breadth_daily_history...")
        cur.execute("SET LOCAL statement_timeout = '120000'")
        batch_size = 50
        inserted = 0
        for i in range(0, len(breadth_rows), batch_size):
            batch = breadth_rows[i:i+batch_size]
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
        conn.commit()
        log(f"Inserted/updated {inserted} rows in breadth_daily_history")

        # ── Step 7: Seed EMA columns in stock_daily_summary ──
        log(f"Seeding EMA columns for {len(final_emas)} stocks...")
        batch = []
        for sid, (e20, e50, e200) in final_emas.items():
            batch.append((e20, e50, e200, sid))
            if len(batch) >= 200:
                cur.executemany("""
                    UPDATE stock_daily_summary
                    SET ema20 = %s, ema50 = %s, ema200 = %s
                    WHERE security_id = %s
                """, batch)
                batch = []
        if batch:
            cur.executemany("""
                UPDATE stock_daily_summary
                SET ema20 = %s, ema50 = %s, ema200 = %s
                WHERE security_id = %s
            """, batch)
        conn.commit()
        log(f"Seeded EMA values for {len(final_emas)} stocks")

        # Verify
        cur.execute("SELECT COUNT(*) as cnt FROM breadth_daily_history")
        cnt = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) as cnt FROM stock_daily_summary WHERE ema20 > 0")
        ema_cnt = cur.fetchone()["cnt"]
        log(f"Verification: {cnt} breadth history rows, {ema_cnt} stocks with EMA seeded")
        log("=== Backfill complete ===")

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        conn.rollback()
    finally:
        close_db(conn)


if __name__ == "__main__":
    main()
