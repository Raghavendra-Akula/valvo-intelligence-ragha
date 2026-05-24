"""
Market Breadth routes — pre-computed breadth history + live EMA-based metrics.

Architecture:
  - breadth_daily_history: pre-computed daily breadth (backfilled, appended by screener refresh)
  - Live: stock_daily_summary EMA columns + live candle → one multiply per stock per EMA
  - /api/breadth/history  — historical data for area charts (<50ms)
  - /api/breadth/live     — today's live breadth (~500ms)
  - /api/breadth/sectors  — live per-sector breakdown (~500ms)
  - /api/breadth/sector-history — historical sector JSONB for heatmaps
"""
import time as _time
from flask import Blueprint, jsonify, request
from extensions import limiter
from database.database import get_db, close_db
from config.settings import is_trading_day, last_trading_date, is_market_open, prev_trading_date_or_today

breadth_bp = Blueprint("breadth", __name__)

# Time-throttle gap-fill so it can heal rows written after the first call
# (e.g., a mid-day screener refresh that corrupted today's row), but doesn't
# re-run the heavy query on every single request.
_gap_fill_last_run = 0.0
_GAP_FILL_INTERVAL_SECS = 300  # 5 minutes

# EMA smoothing constants
K10  = 2.0 / 11   # 0.181818
K20  = 2.0 / 21   # 0.095238
K50  = 2.0 / 51   # 0.039216
K200 = 2.0 / 201  # 0.009950

# Momentum movers: count of stocks where (close / close_N_days_ago - 1) > threshold.
# When adding new (lookback, threshold) pairs:
#   1. ALTER TABLE breadth_daily_history ADD COLUMN up_<pct>pc_<days>d INT DEFAULT 0;
#   2. Add to MOMENTUM_PAIRS below.
#   3. Add a COUNT(*) FILTER in each of: _gap_fill_breadth, _write_today_breadth_from_live,
#      breadth_all live SQL (and screener_routes._append_breadth_row).
#   4. Add the column to every SELECT/dict in breadth_history / breadth_live / breadth_all.
#   5. Re-run scripts/backfill_momentum_movers.py.
MOMENTUM_PAIRS = [
    # (column_name, threshold_pct, lookback_days)
    ("up_20pc_5d", 20, 5),
    ("up_30pc_5d", 30, 5),
]

# Stockbee-style daily breakout/breakdown: stocks moving >=4% with rising volume vs prior day.
# up_4pc_vol  = (close - prev_close)/prev_close >=  4% AND volume > prev_volume
# down_4pc_vol = (close - prev_close)/prev_close <= -4% AND volume > prev_volume
# These complement `thrust` (an A/D ratio %), not duplicate it: thrust measures
# participation breadth, up_4pc_vol measures conviction of leaders.


def _gap_fill_breadth():
    """Detect trading days with candles_daily data but no/stale breadth_daily_history
    row and rewrite them. Throttled to once every 5 minutes per process.

    Heals three failure modes:
    (a) row missing for a trading day with candles_daily data
    (b) row present but corrupt — advance_count=decline_count=0
    (c) row present but stale — computed mid-session (computed_at < 15:30 IST
        on the row's own date), i.e. a partial-day snapshot that needs an EOD
        recompute. This was the 2026-04-24 bug: an early-morning request
        wrote Friday's row from 9:24 IST data and it was never refreshed,
        so Saturday's display served partial-day numbers.

    Never touches today's row while the market is open — that would itself
    create a partial-day row."""
    global _gap_fill_last_run
    now = _time.monotonic()
    if now - _gap_fill_last_run < _GAP_FILL_INTERVAL_SECS:
        return
    _gap_fill_last_run = now
    market_open = is_market_open()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '15000'")
        # Find dates in candles_daily (last 10 days) that need a fresh EOD compute.
        # Excludes today's date if the market is currently open — otherwise we'd
        # write a partial-day snapshot that gets stuck if no EOD refresh runs.
        cur.execute("""
            SELECT DISTINCT cd.date
            FROM candles_daily cd
            LEFT JOIN breadth_daily_history bd ON bd.date = cd.date
            WHERE cd.date >= CURRENT_DATE - 10
              AND EXTRACT(DOW FROM cd.date) NOT IN (0, 6)
              AND NOT (
                  %s::boolean
                  AND cd.date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
              )
              AND (bd.date IS NULL
                   OR (COALESCE(bd.advance_count, 0) = 0
                       AND COALESCE(bd.decline_count, 0) = 0)
                   OR bd.pct_above_ema10 IS NULL
                   OR (bd.computed_at AT TIME ZONE 'Asia/Kolkata')
                       < (bd.date::timestamp + INTERVAL '15 hours 30 minutes'))
            ORDER BY cd.date
        """, (market_open,))
        missing = [r["date"] for r in cur.fetchall()]
        if missing:
            print(f"🔧 Gap-filling {len(missing)} missing/corrupt breadth dates: {[str(d) for d in missing]}")

        for dt in missing:
            dt_str = dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)
            try:
                # Compute 52W high/low from candles BEFORE this date (not from
                # stock_daily_summary which already includes recent data).
                cur.execute(f"""
                    WITH daily AS (
                        SELECT DISTINCT ON (security_id) security_id, close, high, low, volume
                        FROM candles_daily WHERE date = %s
                        ORDER BY security_id, date DESC
                    ),
                    prev AS (
                        SELECT DISTINCT ON (security_id)
                               security_id, close AS prev_close, volume AS prev_volume
                        FROM candles_daily WHERE date < %s
                        ORDER BY security_id, date DESC
                    ),
                    -- Close N trading days ago (rn=5 means 5th-most-recent trading day before %s)
                    lagged AS (
                        SELECT security_id, close, date,
                               ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
                        FROM candles_daily
                        WHERE date < %s AND date >= %s::date - 30 AND volume > 0
                    ),
                    close_lag AS (
                        SELECT security_id, MAX(close) FILTER (WHERE rn = 5) AS c5
                        FROM lagged WHERE rn = 5 GROUP BY security_id
                    ),
                    prior_52w AS (
                        SELECT security_id,
                               MAX(high) AS high_52w,
                               MIN(low)  AS low_52w
                        FROM candles_daily
                        WHERE date >= (%s::date - 365) AND date < %s
                        GROUP BY security_id
                    ),
                    computed AS (
                        SELECT d.close, d.high, d.low, d.volume, p.prev_close, p.prev_volume,
                               w.high_52w, w.low_52w,
                               cl.c5,
                               d.close * {K10} + COALESCE(s.ema10, d.close) * {1-K10} AS live_ema10,
                               d.close * {K20} + COALESCE(s.ema20, d.close) * {1-K20} AS live_ema20,
                               d.close * {K50} + COALESCE(s.ema50, d.close) * {1-K50} AS live_ema50,
                               d.close * {K200} + COALESCE(s.ema200, d.close) * {1-K200} AS live_ema200
                        FROM daily d
                        JOIN prev p ON d.security_id = p.security_id
                        LEFT JOIN prior_52w w ON d.security_id = w.security_id
                        LEFT JOIN close_lag cl ON d.security_id = cl.security_id
                        LEFT JOIN stock_daily_summary s ON d.security_id = s.security_id
                        WHERE COALESCE(s.is_etf, false) = false AND COALESCE(s.ema20, 1) > 0
                    )
                    SELECT COUNT(*) AS total,
                        ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema10) / NULLIF(COUNT(*), 0), 1) AS ema10,
                        ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema20) / NULLIF(COUNT(*), 0), 1) AS ema20,
                        ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema50) / NULLIF(COUNT(*), 0), 1) AS ema50,
                        ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema200) / NULLIF(COUNT(*), 0), 1) AS ema200,
                        COUNT(*) FILTER (WHERE high > high_52w) AS new_highs,
                        COUNT(*) FILTER (WHERE low < low_52w) AS new_lows,
                        ROUND(100.0 * COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, high)) / NULLIF(GREATEST(high_52w, high), 0) < -0.20) / NULLIF(COUNT(*), 0), 1) AS down20,
                        ROUND(100.0 * COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, high)) / NULLIF(GREATEST(high_52w, high), 0) < -0.30) / NULLIF(COUNT(*), 0), 1) AS down30,
                        ROUND(100.0 * COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, high)) / NULLIF(GREATEST(high_52w, high), 0) < -0.50) / NULLIF(COUNT(*), 0), 1) AS down50,
                        COUNT(*) FILTER (WHERE close > prev_close) AS advances,
                        COUNT(*) FILTER (WHERE close < prev_close) AS declines,
                        ROUND(100.0 * COUNT(*) FILTER (WHERE close > prev_close) /
                              NULLIF(COUNT(*) FILTER (WHERE close > prev_close) + COUNT(*) FILTER (WHERE close < prev_close), 0), 1) AS thrust,
                        COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.20) AS up_20pc_5d,
                        COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.30) AS up_30pc_5d,
                        COUNT(*) FILTER (WHERE prev_close > 0 AND volume > prev_volume
                            AND (close - prev_close) / prev_close >=  0.04) AS up_4pc_vol,
                        COUNT(*) FILTER (WHERE prev_close > 0 AND volume > prev_volume
                            AND (close - prev_close) / prev_close <= -0.04) AS down_4pc_vol
                    FROM computed
                """, (dt_str, dt_str, dt_str, dt_str, dt_str, dt_str))
                r = cur.fetchone()
                if r and r["total"] > 0:
                    cur.execute("""
                        INSERT INTO breadth_daily_history
                            (date, total_stocks, pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                             new_highs, new_lows, pct_down_20, pct_down_30, pct_down_50,
                             advance_count, decline_count, thrust, momentum_20pc,
                             up_20pc_5d, up_30pc_5d, up_4pc_vol, down_4pc_vol, computed_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0,
                                %s, %s, %s, %s, NOW())
                        ON CONFLICT (date) DO UPDATE SET
                            total_stocks     = EXCLUDED.total_stocks,
                            pct_above_ema10  = EXCLUDED.pct_above_ema10,
                            pct_above_ema20  = EXCLUDED.pct_above_ema20,
                            pct_above_ema50  = EXCLUDED.pct_above_ema50,
                            pct_above_ema200 = EXCLUDED.pct_above_ema200,
                            new_highs        = EXCLUDED.new_highs,
                            new_lows         = EXCLUDED.new_lows,
                            pct_down_20      = EXCLUDED.pct_down_20,
                            pct_down_30      = EXCLUDED.pct_down_30,
                            pct_down_50      = EXCLUDED.pct_down_50,
                            advance_count    = EXCLUDED.advance_count,
                            decline_count    = EXCLUDED.decline_count,
                            thrust           = EXCLUDED.thrust,
                            up_20pc_5d       = EXCLUDED.up_20pc_5d,
                            up_30pc_5d       = EXCLUDED.up_30pc_5d,
                            up_4pc_vol       = EXCLUDED.up_4pc_vol,
                            down_4pc_vol     = EXCLUDED.down_4pc_vol,
                            computed_at      = NOW()
                    """, (dt_str, r["total"], float(r["ema10"]), float(r["ema20"]), float(r["ema50"]), float(r["ema200"]),
                          r["new_highs"], r["new_lows"],
                          float(r["down20"]), float(r["down30"]), float(r["down50"]),
                          r["advances"], r["declines"], float(r["thrust"]),
                          r["up_20pc_5d"], r["up_30pc_5d"],
                          r["up_4pc_vol"], r["down_4pc_vol"]))
                    conn.commit()
                    print(f"  ✅ Backfilled breadth for {dt_str} ({r['total']} stocks)")
            except Exception as e:
                print(f"  ⚠️ Failed to backfill {dt_str}: {e}")
                conn.rollback()

        # ── Today's row from stock_daily_summary.live_* ─────────────────────
        # candles_daily's today row may be written by a separate EOD pipeline
        # that runs late; meanwhile stock_daily_summary.live_close is kept
        # ~10s fresh by the WebSocket triggers. If today is a trading day and
        # the market is closed but today's breadth_daily_history row is still
        # missing, write it from the live columns so users don't see Friday's
        # numbers all evening. Skipped while the market is open (the live
        # endpoint already serves intraday breadth).
        if not market_open and is_trading_day():
            try:
                _write_today_breadth_from_live(conn)
            except Exception as e:
                print(f"  ⚠️ Failed to write today's breadth from live: {e}")
                conn.rollback()
    except Exception as e:
        print(f"⚠️ Gap-fill check failed (non-fatal): {e}")
    finally:
        close_db(conn)


def _write_today_breadth_from_live(conn):
    """Compute today's breadth from stock_daily_summary.live_* and upsert into
    breadth_daily_history. Used post-close when candles_daily hasn't received
    today's EOD row yet but live_close has the final 15:30 print."""
    cur = conn.cursor()
    cur.execute("SET LOCAL statement_timeout = '15000'")

    # Skip if today's row already exists with sane numbers (advance != decline).
    cur.execute("""
        SELECT advance_count, decline_count, computed_at
        FROM breadth_daily_history
        WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
    """)
    existing = cur.fetchone()
    if existing and (
        (existing.get("advance_count") or 0) > 0
        or (existing.get("decline_count") or 0) > 0
    ):
        return  # already populated

    cur.execute(f"""
        WITH lagged AS (
            SELECT security_id, close, volume, date,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
            FROM candles_daily
            WHERE date < (NOW() AT TIME ZONE 'Asia/Kolkata')::date
              AND date >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date - 30
              AND volume > 0
        ),
        close_lag AS (
            SELECT security_id,
                   MAX(close)  FILTER (WHERE rn = 5) AS c5,
                   MAX(volume) FILTER (WHERE rn = 1) AS prev_vol
            FROM lagged WHERE rn IN (1, 5) GROUP BY security_id
        ),
        computed AS (
            SELECT
                COALESCE(s.live_close, s.prev_close) AS close,
                COALESCE(s.live_high, s.high_52w) AS today_high,
                COALESCE(s.live_low, s.low_52w) AS today_low,
                COALESCE(s.live_volume, 0) AS today_vol,
                s.prev_close, s.high_52w, s.low_52w,
                cl.c5, cl.prev_vol,
                COALESCE(s.live_close, s.prev_close) * {K10}
                  + COALESCE(s.ema10, s.prev_close) * {1-K10} AS live_ema10,
                COALESCE(s.live_close, s.prev_close) * {K20}
                  + s.ema20 * {1-K20} AS live_ema20,
                COALESCE(s.live_close, s.prev_close) * {K50}
                  + s.ema50 * {1-K50} AS live_ema50,
                COALESCE(s.live_close, s.prev_close) * {K200}
                  + s.ema200 * {1-K200} AS live_ema200
            FROM stock_daily_summary s
            LEFT JOIN close_lag cl ON s.security_id = cl.security_id
            WHERE s.is_etf = false
              AND s.ema20 > 0
              AND s.prev_close > 0
              AND s.live_close IS NOT NULL
        )
        SELECT COUNT(*) AS total,
            ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema10)  / NULLIF(COUNT(*), 0), 1) AS ema10,
            ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema20)  / NULLIF(COUNT(*), 0), 1) AS ema20,
            ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema50)  / NULLIF(COUNT(*), 0), 1) AS ema50,
            ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema200) / NULLIF(COUNT(*), 0), 1) AS ema200,
            COUNT(*) FILTER (WHERE today_high > high_52w) AS new_highs,
            COUNT(*) FILTER (WHERE today_low  < low_52w)  AS new_lows,
            ROUND(100.0 * COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.20) / NULLIF(COUNT(*), 0), 1) AS down20,
            ROUND(100.0 * COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.30) / NULLIF(COUNT(*), 0), 1) AS down30,
            ROUND(100.0 * COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.50) / NULLIF(COUNT(*), 0), 1) AS down50,
            COUNT(*) FILTER (WHERE close > prev_close) AS advances,
            COUNT(*) FILTER (WHERE close < prev_close) AS declines,
            ROUND(100.0 * COUNT(*) FILTER (WHERE close > prev_close) /
                  NULLIF(COUNT(*) FILTER (WHERE close > prev_close)
                         + COUNT(*) FILTER (WHERE close < prev_close), 0), 1) AS thrust,
            COUNT(*) FILTER (WHERE prev_close > 0
                AND ABS(close - prev_close) / prev_close > 0.20) AS momentum,
            COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.20) AS up_20pc_5d,
            COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.30) AS up_30pc_5d,
            COUNT(*) FILTER (WHERE prev_close > 0 AND prev_vol > 0 AND today_vol > prev_vol
                AND (close - prev_close) / prev_close >=  0.04) AS up_4pc_vol,
            COUNT(*) FILTER (WHERE prev_close > 0 AND prev_vol > 0 AND today_vol > prev_vol
                AND (close - prev_close) / prev_close <= -0.04) AS down_4pc_vol
        FROM computed
    """)
    r = cur.fetchone()
    if not r or not r["total"] or (r.get("advances", 0) == 0 and r.get("declines", 0) == 0):
        # No live data yet (everyone tied or no live_close populated) —
        # don't overwrite with a meaningless zero row.
        return

    cur.execute("""
        INSERT INTO breadth_daily_history
            (date, total_stocks, pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
             new_highs, new_lows, pct_down_20, pct_down_30, pct_down_50,
             advance_count, decline_count, thrust, momentum_20pc,
             up_20pc_5d, up_30pc_5d, up_4pc_vol, down_4pc_vol, computed_at)
        VALUES (
            (NOW() AT TIME ZONE 'Asia/Kolkata')::date,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
        )
        ON CONFLICT (date) DO UPDATE SET
            total_stocks     = EXCLUDED.total_stocks,
            pct_above_ema10  = EXCLUDED.pct_above_ema10,
            pct_above_ema20  = EXCLUDED.pct_above_ema20,
            pct_above_ema50  = EXCLUDED.pct_above_ema50,
            pct_above_ema200 = EXCLUDED.pct_above_ema200,
            new_highs        = EXCLUDED.new_highs,
            new_lows         = EXCLUDED.new_lows,
            pct_down_20      = EXCLUDED.pct_down_20,
            pct_down_30      = EXCLUDED.pct_down_30,
            pct_down_50      = EXCLUDED.pct_down_50,
            advance_count    = EXCLUDED.advance_count,
            decline_count    = EXCLUDED.decline_count,
            thrust           = EXCLUDED.thrust,
            momentum_20pc    = EXCLUDED.momentum_20pc,
            up_20pc_5d       = EXCLUDED.up_20pc_5d,
            up_30pc_5d       = EXCLUDED.up_30pc_5d,
            up_4pc_vol       = EXCLUDED.up_4pc_vol,
            down_4pc_vol     = EXCLUDED.down_4pc_vol,
            computed_at      = NOW()
    """, (
        r["total"], float(r["ema10"] or 0), float(r["ema20"] or 0), float(r["ema50"] or 0), float(r["ema200"] or 0),
        r["new_highs"], r["new_lows"],
        float(r["down20"] or 0), float(r["down30"] or 0), float(r["down50"] or 0),
        r["advances"], r["declines"], float(r["thrust"] or 0), r["momentum"],
        r["up_20pc_5d"], r["up_30pc_5d"],
        r["up_4pc_vol"], r["down_4pc_vol"],
    ))
    conn.commit()
    print(f"  ✅ Wrote today's breadth from live ({r['total']} stocks, "
          f"adv={r['advances']}, dec={r['declines']})")


@breadth_bp.route("/api/breadth/history", methods=["GET"])
@limiter.limit("60 per minute")
def breadth_history():
    """Historical breadth data. ?days=365 OR ?from=2020-01-01&to=2022-12-31."""
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    days = min(int(request.args.get("days", 365)), 6000)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '10000'")
        if date_from and date_to:
            cur.execute("""
                SELECT date, total_stocks,
                       pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                       new_highs, new_lows,
                       pct_down_20, pct_down_30, pct_down_50,
                       advance_count, decline_count, thrust,
                       momentum_20pc, up_20pc_5d, up_30pc_5d,
                       up_4pc_vol, down_4pc_vol
                FROM breadth_daily_history
                WHERE date >= %s AND date <= %s
                ORDER BY date
            """, (date_from, date_to))
        else:
            cur.execute("""
                SELECT date, total_stocks,
                       pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                       new_highs, new_lows,
                       pct_down_20, pct_down_30, pct_down_50,
                       advance_count, decline_count, thrust,
                       momentum_20pc, up_20pc_5d, up_30pc_5d,
                       up_4pc_vol, down_4pc_vol
                FROM breadth_daily_history
                WHERE date >= CURRENT_DATE - %s
                ORDER BY date
            """, (days,))
        rows = cur.fetchall()
        history = []
        for r in rows:
            history.append({
                "date": str(r["date"]),
                "total": r["total_stocks"],
                "ema10": float(r["pct_above_ema10"] or 0),
                "ema20": float(r["pct_above_ema20"] or 0),
                "ema50": float(r["pct_above_ema50"] or 0),
                "ema200": float(r["pct_above_ema200"] or 0),
                "newHighs": r["new_highs"],
                "newLows": r["new_lows"],
                "down20": float(r["pct_down_20"] or 0),
                "down30": float(r["pct_down_30"] or 0),
                "down50": float(r["pct_down_50"] or 0),
                "advances": r["advance_count"],
                "declines": r["decline_count"],
                "thrust": float(r["thrust"] or 0),
                "momentum": r["momentum_20pc"],
                "up20pc5d": int(r["up_20pc_5d"] or 0),
                "up30pc5d": int(r["up_30pc_5d"] or 0),
                "up4pcVol": int(r["up_4pc_vol"] or 0),
                "down4pcVol": int(r["down_4pc_vol"] or 0),
            })
        return jsonify({"history": history, "count": len(history)})
    except Exception as e:
        print(f"❌ breadth/history error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@breadth_bp.route("/api/breadth/live", methods=["GET"])
@limiter.limit("60 per minute")
def breadth_live():
    """Today's live breadth from stock_daily_summary EMAs + live candle close.
    On non-trading days (weekends/holidays), returns the last trading day's stored data."""
    _gap_fill_breadth()  # auto-backfill any missing days (runs once per process)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '8000'")

        # ── Check if market is open (trading day + 9:15-15:30 IST) ──
        # Before market opens on a trading day, serve previous day's data.
        _is_trading = is_trading_day()
        _market_open = is_market_open()

        if not _is_trading or not _market_open:
            # ── MARKET CLOSED: prefer today if it's a trading day (post-close
            # gap-fill writes today's row), else last trading date for weekends
            # and holidays. last_trading_date() walks BACKWARDS from today and
            # would return Friday on a Monday post-close — wrong. ──
            _target = prev_trading_date_or_today()
            last_td_val = _target.isoformat() if _target else None

            r = None
            if last_td_val:
                cur.execute("""
                    SELECT date, total_stocks,
                           pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                           new_highs, new_lows,
                           pct_down_20, pct_down_30, pct_down_50,
                           advance_count, decline_count, thrust,
                           momentum_20pc, up_20pc_5d, up_30pc_5d,
                           up_4pc_vol, down_4pc_vol
                    FROM breadth_daily_history WHERE date = %s
                """, (last_td_val,))
                r = cur.fetchone()

            # Fallback: if calendar date has no data, use most recent row
            if not r:
                cur.execute("""
                    SELECT date, total_stocks,
                           pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                           new_highs, new_lows,
                           pct_down_20, pct_down_30, pct_down_50,
                           advance_count, decline_count, thrust,
                           momentum_20pc, up_20pc_5d, up_30pc_5d,
                           up_4pc_vol, down_4pc_vol
                    FROM breadth_daily_history ORDER BY date DESC LIMIT 1
                """)
                r = cur.fetchone()

            if not r:
                return jsonify({"error": "No breadth data available"}), 404

            return jsonify({
                "total": r["total_stocks"],
                "ema10": float(r["pct_above_ema10"] or 0),
                "ema20": float(r["pct_above_ema20"] or 0),
                "ema50": float(r["pct_above_ema50"] or 0),
                "ema200": float(r["pct_above_ema200"] or 0),
                "newHighs": r["new_highs"],
                "newLows": r["new_lows"],
                "down20": float(r["pct_down_20"] or 0),
                "down30": float(r["pct_down_30"] or 0),
                "down50": float(r["pct_down_50"] or 0),
                "advances": r["advance_count"],
                "declines": r["decline_count"],
                "thrust": float(r["thrust"] or 0),
                "momentum": r["momentum_20pc"],
                "up20pc5d": int(r["up_20pc_5d"] or 0),
                "up30pc5d": int(r["up_30pc_5d"] or 0),
                "up4pcVol": int(r["up_4pc_vol"] or 0),
                "down4pcVol": int(r["down_4pc_vol"] or 0),
                "asOf": str(r["date"]),
                "marketClosed": True,
            })

        # ── TRADING DAY: compute live ──
        cur.execute(f"""
            WITH live AS (
                SELECT DISTINCT ON (security_id)
                    security_id, close, high, low, volume
                FROM candles_daily
                WHERE date >= CURRENT_DATE - 5
                ORDER BY security_id, date DESC
            ),
            lagged AS (
                SELECT security_id, close, volume,
                       ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
                FROM candles_daily
                WHERE date >= CURRENT_DATE - 30 AND volume > 0
            ),
            close_lag AS (
                SELECT security_id,
                       MAX(close)  FILTER (WHERE rn = 6)  AS c5,
                       MAX(close)  FILTER (WHERE rn = 11) AS c10,
                       MAX(close)  FILTER (WHERE rn = 21) AS c20,
                       MAX(volume) FILTER (WHERE rn = 2)  AS prev_vol
                FROM lagged
                WHERE rn IN (2, 6, 11, 21)
                GROUP BY security_id
            ),
            computed AS (
                SELECT
                    s.security_id,
                    c.close,
                    c.high AS today_high,
                    c.low AS today_low,
                    c.volume AS today_vol,
                    s.prev_close,
                    s.high_52w,
                    s.low_52w,
                    cl.c5, cl.prev_vol,
                    c.close * {K10} + COALESCE(s.ema10, c.close) * {1-K10} AS live_ema10,
                    c.close * {K20} + s.ema20 * {1-K20} AS live_ema20,
                    c.close * {K50} + s.ema50 * {1-K50} AS live_ema50,
                    c.close * {K200} + s.ema200 * {1-K200} AS live_ema200
                FROM stock_daily_summary s
                JOIN live c ON s.security_id = c.security_id
                LEFT JOIN close_lag cl ON s.security_id = cl.security_id
                WHERE s.is_etf = false AND s.ema20 > 0
            )
            SELECT
                COUNT(*) AS total,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema10) / NULLIF(COUNT(*), 0), 1) AS ema10,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema20) / NULLIF(COUNT(*), 0), 1) AS ema20,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema50) / NULLIF(COUNT(*), 0), 1) AS ema50,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema200) / NULLIF(COUNT(*), 0), 1) AS ema200,
                -- New high = today's high exceeds the pre-computed 52W high (excludes today)
                COUNT(*) FILTER (WHERE today_high > high_52w) AS new_highs,
                -- New low = today's low breaks below the pre-computed 52W low (excludes today)
                COUNT(*) FILTER (WHERE today_low < low_52w) AS new_lows,
                -- Fall from high uses GREATEST to include today's potential new high
                ROUND(100.0 * COUNT(*) FILTER (
                    WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.20
                ) / NULLIF(COUNT(*), 0), 1) AS down20,
                ROUND(100.0 * COUNT(*) FILTER (
                    WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.30
                ) / NULLIF(COUNT(*), 0), 1) AS down30,
                ROUND(100.0 * COUNT(*) FILTER (
                    WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.50
                ) / NULLIF(COUNT(*), 0), 1) AS down50,
                COUNT(*) FILTER (WHERE close > prev_close) AS advances,
                COUNT(*) FILTER (WHERE close < prev_close) AS declines,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > prev_close) /
                      NULLIF(COUNT(*) FILTER (WHERE close > prev_close) +
                             COUNT(*) FILTER (WHERE close < prev_close), 0), 1) AS thrust,
                COUNT(*) FILTER (WHERE prev_close > 0 AND
                    ABS(close - prev_close) / prev_close > 0.20) AS momentum,
                COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.20) AS up_20pc_5d,
                COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.30) AS up_30pc_5d,
                COUNT(*) FILTER (WHERE prev_close > 0 AND prev_vol > 0 AND today_vol > prev_vol
                    AND (close - prev_close) / prev_close >=  0.04) AS up_4pc_vol,
                COUNT(*) FILTER (WHERE prev_close > 0 AND prev_vol > 0 AND today_vol > prev_vol
                    AND (close - prev_close) / prev_close <= -0.04) AS down_4pc_vol
            FROM computed
        """)
        r = cur.fetchone()
        if not r:
            return jsonify({"error": "No data"}), 404

        result = {
            "total": r["total"],
            "ema10": float(r["ema10"] or 0),
            "ema20": float(r["ema20"] or 0),
            "ema50": float(r["ema50"] or 0),
            "ema200": float(r["ema200"] or 0),
            "newHighs": r["new_highs"],
            "newLows": r["new_lows"],
            "down20": float(r["down20"] or 0),
            "down30": float(r["down30"] or 0),
            "down50": float(r["down50"] or 0),
            "advances": r["advances"],
            "declines": r["declines"],
            "thrust": float(r["thrust"] or 0),
            "momentum": r["momentum"],
            "up20pc5d": int(r["up_20pc_5d"] or 0),
            "up30pc5d": int(r["up_30pc_5d"] or 0),
            "up4pcVol": int(r["up_4pc_vol"] or 0),
            "down4pcVol": int(r["down_4pc_vol"] or 0),
        }

        # Piggyback: store intraday sample (at most once per minute)
        try:
            cur.execute("""
                INSERT INTO breadth_intraday (date, time_ist, advances, declines, total)
                VALUES (
                    (NOW() AT TIME ZONE 'Asia/Kolkata')::date,
                    date_trunc('minute', (NOW() AT TIME ZONE 'Asia/Kolkata')::time),
                    %s, %s, %s
                )
                ON CONFLICT (date, time_ist) DO NOTHING
            """, (r["advances"], r["declines"], r["total"]))
            conn.commit()
        except Exception:
            pass  # non-fatal — don't break the live response

        return jsonify(result)
    except Exception as e:
        print(f"❌ breadth/live error: {e}")
        import traceback; traceback.print_exc()
        print(f"[breadth] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@breadth_bp.route("/api/breadth/sectors", methods=["GET"])
@limiter.limit("60 per minute")
def breadth_sectors():
    """Live per-sector breadth breakdown."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '8000'")
        cur.execute(f"""
            WITH live AS (
                SELECT DISTINCT ON (security_id)
                    security_id, close, high, low
                FROM candles_daily
                WHERE date >= CURRENT_DATE - 5
                ORDER BY security_id, date DESC
            ),
            computed AS (
                SELECT
                    c.close,
                    c.close * {K20} + s.ema20 * {1-K20} AS live_ema20,
                    c.close * {K50} + s.ema50 * {1-K50} AS live_ema50,
                    c.close * {K200} + s.ema200 * {1-K200} AS live_ema200,
                    su.sector
                FROM stock_daily_summary s
                JOIN live c ON s.security_id = c.security_id
                JOIN stock_universe su ON s.security_id = su.security_id
                WHERE s.is_etf = false AND s.ema20 > 0
                  AND su.sector IS NOT NULL AND su.sector != ''
            )
            SELECT
                sector,
                COUNT(*) AS total,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema20) / NULLIF(COUNT(*), 0), 1) AS above20,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema50) / NULLIF(COUNT(*), 0), 1) AS above50,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema200) / NULLIF(COUNT(*), 0), 1) AS above200
            FROM computed
            GROUP BY sector
            HAVING COUNT(*) >= 5
            ORDER BY sector
        """)
        rows = cur.fetchall()
        sectors = [{
            "sector": r["sector"],
            "total": r["total"],
            "above20": float(r["above20"] or 0),
            "above50": float(r["above50"] or 0),
            "above200": float(r["above200"] or 0),
        } for r in rows]
        return jsonify({"sectors": sectors, "count": len(sectors)})
    except Exception as e:
        print(f"❌ breadth/sectors error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@breadth_bp.route("/api/breadth/sector-history", methods=["GET"])
@limiter.limit("60 per minute")
def breadth_sector_history():
    """Historical sector breadth from JSONB column."""
    days = min(int(request.args.get("days", 252)), 504)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute("""
            SELECT date, sector_breadth
            FROM breadth_daily_history
            WHERE date >= CURRENT_DATE - %s
              AND sector_breadth != '{}'::jsonb
            ORDER BY date
        """, (days,))
        rows = cur.fetchall()
        history = [{
            "date": str(r["date"]),
            "sectors": r["sector_breadth"] if isinstance(r["sector_breadth"], dict) else {},
        } for r in rows]
        return jsonify({"history": history, "count": len(history)})
    except Exception as e:
        print(f"❌ breadth/sector-history error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@breadth_bp.route("/api/breadth/intraday", methods=["GET"])
@limiter.limit("60 per minute")
def breadth_intraday():
    """Today's intraday advance/decline samples (one per minute, 9:15-15:30)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '3000'")
        cur.execute("""
            SELECT time_ist, advances, declines, total
            FROM breadth_intraday
            WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
            ORDER BY time_ist
        """)
        rows = cur.fetchall()
        samples = [{
            "time": str(r["time_ist"])[:5],  # "09:15"
            "adv": r["advances"],
            "dec": r["declines"],
            "total": r["total"],
        } for r in rows]
        return jsonify({"samples": samples, "count": len(samples)})
    except Exception as e:
        print(f"❌ breadth/intraday error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@breadth_bp.route("/api/breadth/all", methods=["GET"])
@limiter.limit("60 per minute")
def breadth_all():
    """Single endpoint: history + live + intraday in ONE DB connection.
    Avoids 4 parallel requests that exhaust the connection pool."""
    _gap_fill_breadth()  # auto-backfill any missing days (runs once per process)
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    days = min(int(request.args.get("days", 365)), 6000)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '15000'")

        # 1) History
        if date_from and date_to:
            cur.execute("""
                SELECT date, total_stocks, pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                       new_highs, new_lows, pct_down_20, pct_down_30, pct_down_50,
                       advance_count, decline_count, thrust, momentum_20pc, computed_at,
                       up_20pc_5d, up_30pc_5d, up_4pc_vol, down_4pc_vol
                FROM breadth_daily_history WHERE date >= %s AND date <= %s ORDER BY date
            """, (date_from, date_to))
        else:
            cur.execute("""
                SELECT date, total_stocks, pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                       new_highs, new_lows, pct_down_20, pct_down_30, pct_down_50,
                       advance_count, decline_count, thrust, momentum_20pc, computed_at,
                       up_20pc_5d, up_30pc_5d, up_4pc_vol, down_4pc_vol
                FROM breadth_daily_history WHERE date >= CURRENT_DATE - %s ORDER BY date
            """, (days,))
        history = [{
            "date": str(r["date"]), "total": r["total_stocks"],
            "ema10": float(r["pct_above_ema10"] or 0),
            "ema20": float(r["pct_above_ema20"] or 0), "ema50": float(r["pct_above_ema50"] or 0),
            "ema200": float(r["pct_above_ema200"] or 0),
            "newHighs": r["new_highs"], "newLows": r["new_lows"],
            "down20": float(r["pct_down_20"] or 0), "down30": float(r["pct_down_30"] or 0),
            "down50": float(r["pct_down_50"] or 0),
            "advances": r["advance_count"], "declines": r["decline_count"],
            "thrust": float(r["thrust"] or 0), "momentum": r["momentum_20pc"],
            "computedAt": r["computed_at"].isoformat() if r["computed_at"] else None,
            "up20pc5d": int(r["up_20pc_5d"] or 0),
            "up30pc5d": int(r["up_30pc_5d"] or 0),
            "up4pcVol": int(r["up_4pc_vol"] or 0),
            "down4pcVol": int(r["down_4pc_vol"] or 0),
        } for r in cur.fetchall()]

        # 2) Live — serve previous day's data when market isn't open
        # (non-trading day OR trading day before 9:15 / after 15:30 IST)
        _is_trading = is_trading_day()
        _market_open = is_market_open()

        if not _is_trading or not _market_open:
            # Prefer today if it's a trading day (gap-fill writes today's row
            # after close); fall back to last trading day on weekends/holidays.
            _target = prev_trading_date_or_today()
            _last_td_str = _target.isoformat() if _target else None

            hr = None
            if _last_td_str:
                cur.execute("""
                    SELECT date, total_stocks, pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                           new_highs, new_lows, pct_down_20, pct_down_30, pct_down_50,
                           advance_count, decline_count, thrust, momentum_20pc, computed_at,
                           up_20pc_5d, up_30pc_5d, up_4pc_vol, down_4pc_vol
                    FROM breadth_daily_history WHERE date = %s
                """, (_last_td_str,))
                hr = cur.fetchone()

            if not hr:
                cur.execute("""
                    SELECT date, total_stocks, pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                           new_highs, new_lows, pct_down_20, pct_down_30, pct_down_50,
                           advance_count, decline_count, thrust, momentum_20pc, computed_at,
                           up_20pc_5d, up_30pc_5d, up_4pc_vol, down_4pc_vol
                    FROM breadth_daily_history ORDER BY date DESC LIMIT 1
                """)
                hr = cur.fetchone()

            if hr:
                live_data = {
                    "total": hr["total_stocks"],
                    "ema10": float(hr["pct_above_ema10"] or 0),
                    "ema20": float(hr["pct_above_ema20"] or 0),
                    "ema50": float(hr["pct_above_ema50"] or 0), "ema200": float(hr["pct_above_ema200"] or 0),
                    "newHighs": hr["new_highs"], "newLows": hr["new_lows"],
                    "down20": float(hr["pct_down_20"] or 0), "down30": float(hr["pct_down_30"] or 0),
                    "down50": float(hr["pct_down_50"] or 0),
                    "advances": hr["advance_count"], "declines": hr["decline_count"],
                    "thrust": float(hr["thrust"] or 0), "momentum": hr["momentum_20pc"],
                    "computedAt": hr["computed_at"].isoformat() if hr["computed_at"] else None,
                    "up20pc5d": int(hr["up_20pc_5d"] or 0),
                    "up30pc5d": int(hr["up_30pc_5d"] or 0),
                    "up4pcVol": int(hr["up_4pc_vol"] or 0),
                    "down4pcVol": int(hr["down_4pc_vol"] or 0),
                    "asOf": str(hr["date"]), "marketClosed": True,
                }
            else:
                live_data = {}

            # Intraday from last trading day
            intraday_date = hr["date"] if hr else _last_td_str
            intraday = []
            if intraday_date:
                cur.execute("""
                    SELECT time_ist, advances, declines, total FROM breadth_intraday
                    WHERE date = %s ORDER BY time_ist
                """, (intraday_date,))
                intraday = [{"time": str(row["time_ist"])[:5], "adv": row["advances"],
                             "dec": row["declines"], "total": row["total"]} for row in cur.fetchall()]

            return jsonify({"history": history, "live": live_data, "intraday": intraday})

        # ── TRADING DAY: compute live (no live CTE — uses trigger-synced columns) ──
        # close_lag pre-computes 5/10/20-day lookback closes (rn=6/11/21) plus
        # prev day's volume (rn=2) for Stockbee-style 4% on volume counts.
        # rn=6 means "5 trading days before today" because today's row is rn=1.
        cur.execute(f"""
            WITH lagged AS (
                SELECT security_id, close, volume,
                       ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
                FROM candles_daily
                WHERE date >= CURRENT_DATE - 30 AND volume > 0
            ),
            close_lag AS (
                SELECT security_id,
                       MAX(close)  FILTER (WHERE rn = 6)  AS c5,
                       MAX(close)  FILTER (WHERE rn = 11) AS c10,
                       MAX(close)  FILTER (WHERE rn = 21) AS c20,
                       MAX(volume) FILTER (WHERE rn = 2)  AS prev_vol
                FROM lagged
                WHERE rn IN (2, 6, 11, 21)
                GROUP BY security_id
            ),
            computed AS (
                SELECT s.security_id,
                    COALESCE(s.live_close, s.prev_close) AS close,
                    COALESCE(s.live_high, s.high_52w) AS today_high,
                    COALESCE(s.live_low, s.low_52w) AS today_low,
                    COALESCE(s.live_volume, 0) AS today_vol,
                    s.prev_close, s.high_52w, s.low_52w,
                    cl.c5, cl.prev_vol,
                    COALESCE(s.live_close, s.prev_close) * {K10}
                        + COALESCE(s.ema10, s.prev_close) * {1-K10} AS live_ema10,
                    COALESCE(s.live_close, s.prev_close) * {K20} + s.ema20 * {1-K20} AS live_ema20,
                    COALESCE(s.live_close, s.prev_close) * {K50} + s.ema50 * {1-K50} AS live_ema50,
                    COALESCE(s.live_close, s.prev_close) * {K200} + s.ema200 * {1-K200} AS live_ema200
                FROM stock_daily_summary s
                LEFT JOIN close_lag cl ON s.security_id = cl.security_id
                WHERE s.is_etf = false AND s.ema20 > 0 AND s.prev_close > 0
            )
            SELECT COUNT(*) AS total,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema10) / NULLIF(COUNT(*), 0), 1) AS ema10,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema20) / NULLIF(COUNT(*), 0), 1) AS ema20,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema50) / NULLIF(COUNT(*), 0), 1) AS ema50,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema200) / NULLIF(COUNT(*), 0), 1) AS ema200,
                COUNT(*) FILTER (WHERE today_high > high_52w) AS new_highs,
                COUNT(*) FILTER (WHERE today_low < low_52w) AS new_lows,
                ROUND(100.0 * COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.20) / NULLIF(COUNT(*), 0), 1) AS down20,
                ROUND(100.0 * COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.30) / NULLIF(COUNT(*), 0), 1) AS down30,
                ROUND(100.0 * COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.50) / NULLIF(COUNT(*), 0), 1) AS down50,
                COUNT(*) FILTER (WHERE close > prev_close) AS advances,
                COUNT(*) FILTER (WHERE close < prev_close) AS declines,
                ROUND(100.0 * COUNT(*) FILTER (WHERE close > prev_close) / NULLIF(COUNT(*) FILTER (WHERE close > prev_close) + COUNT(*) FILTER (WHERE close < prev_close), 0), 1) AS thrust,
                COUNT(*) FILTER (WHERE prev_close > 0 AND ABS(close - prev_close) / prev_close > 0.20) AS momentum,
                COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.20) AS up_20pc_5d,
                COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.30) AS up_30pc_5d,
                COUNT(*) FILTER (WHERE prev_close > 0 AND prev_vol > 0 AND today_vol > prev_vol
                    AND (close - prev_close) / prev_close >=  0.04) AS up_4pc_vol,
                COUNT(*) FILTER (WHERE prev_close > 0 AND prev_vol > 0 AND today_vol > prev_vol
                    AND (close - prev_close) / prev_close <= -0.04) AS down_4pc_vol
            FROM computed
        """)
        r = cur.fetchone()
        live_data = {
            "total": r["total"],
            "ema10": float(r["ema10"] or 0),
            "ema20": float(r["ema20"] or 0), "ema50": float(r["ema50"] or 0),
            "ema200": float(r["ema200"] or 0), "newHighs": r["new_highs"], "newLows": r["new_lows"],
            "down20": float(r["down20"] or 0), "down30": float(r["down30"] or 0), "down50": float(r["down50"] or 0),
            "advances": r["advances"], "declines": r["declines"],
            "thrust": float(r["thrust"] or 0), "momentum": r["momentum"],
            "up20pc5d": int(r["up_20pc_5d"] or 0),
            "up30pc5d": int(r["up_30pc_5d"] or 0),
            "up4pcVol": int(r["up_4pc_vol"] or 0),
            "down4pcVol": int(r["down_4pc_vol"] or 0),
        } if r else {}

        # Piggyback intraday sample
        try:
            cur.execute("""
                INSERT INTO breadth_intraday (date, time_ist, advances, declines, total)
                VALUES ((NOW() AT TIME ZONE 'Asia/Kolkata')::date,
                        date_trunc('minute', (NOW() AT TIME ZONE 'Asia/Kolkata')::time), %s, %s, %s)
                ON CONFLICT (date, time_ist) DO NOTHING
            """, (r["advances"], r["declines"], r["total"]))
            conn.commit()
        except Exception:
            pass

        # 3) Intraday
        cur.execute("""
            SELECT time_ist, advances, declines, total FROM breadth_intraday
            WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date ORDER BY time_ist
        """)
        intraday = [{"time": str(row["time_ist"])[:5], "adv": row["advances"],
                      "dec": row["declines"], "total": row["total"]} for row in cur.fetchall()]

        return jsonify({"history": history, "live": live_data, "intraday": intraday})
    except Exception as e:
        print(f"❌ breadth/all error: {e}")
        import traceback; traceback.print_exc()
        print(f"[breadth] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@breadth_bp.route("/api/breadth/index-dashboard", methods=["GET"])
@limiter.limit("60 per minute")
def breadth_index_dashboard():
    """Composite endpoint for the Index Breadth Dashboard.
    Returns aligned candles_indices OHLC + breadth_daily_history rows in one
    round-trip so the frontend's six panes paint in lock-step.

    ALIGNMENT GUARANTEE
    Lightweight-Charts v5 positions bars by their logical INDEX on the time
    scale, not by their `time` value. If the candle pane has 60 dates and
    the breadth pane has 63 dates, bar #20 on each pane lands on a
    different x-pixel and the crosshair drifts. We therefore intersect
    both series on a single shared date set so every returned row has a
    1-to-1 match across all panes.

    The intersection runs in two passes:
      1. Fetch the candle window.  Filter breadth to dates that appear
         in candles → drops "breadth_only" rows (e.g. index holidays
         where individual stocks traded).
      2. From those breadth rows, build a set, and filter candles down
         to dates that also have a breadth row → drops "candles_only"
         rows (e.g. partial-data days where breadth couldn't be
         computed but the index still closed).

    Historical mismatches (2020+ audit on NIFTY SMALLCAP 250):
      - 2023-11-12  breadth_only  Diwali muhurat (Sunday)
      - 2025-07-16  candles_only  partial-data day, ~176/2340 stocks
      - 2026-03-17  breadth_only  Holi
      - 2026-03-20  breadth_only  Mahavir Jayanti
      - 2026-04-01  breadth_only  Ram Navami

    Query params:
      symbol  — index symbol (default 'NIFTY SMALLCAP 250')
      days    — trading-day lookback (default 365, max 6000)
    """
    _gap_fill_breadth()  # heal any missing/stale rows before returning
    symbol = request.args.get("symbol", "NIFTY SMALLCAP 250")
    days = min(int(request.args.get("days", 365)), 6000)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '10000'")

        # During market hours, `candles_indices` has today's row (live-fed
        # every ~10s by the websocket) but `breadth_daily_history` does not
        # — `_gap_fill_breadth` deliberately skips writing today's row while
        # the market is open. The intersection below would then drop today's
        # candle. Materialise today's breadth row from live so today survives.
        # Idempotent — no-op once today's row has sane adv/dec counts.
        if is_trading_day():
            try:
                _write_today_breadth_from_live(conn)
            except Exception as e:
                print(f"  ⚠️ index-dashboard live breadth write skipped: {e}")
                conn.rollback()

        # 1) Master date set = candle dates for the chosen index window.
        cur.execute("""
            SELECT date, open, high, low, close, volume
            FROM candles_indices
            WHERE symbol = %s
              AND date >= CURRENT_DATE - %s
            ORDER BY date
        """, (symbol, days))
        candle_rows = cur.fetchall()

        # 2) Breadth strictly limited to candle dates (drops breadth_only).
        if candle_rows:
            candle_dates = [r["date"] for r in candle_rows]
            cur.execute("""
                SELECT date, total_stocks,
                       pct_above_ema10, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                       new_highs, new_lows, advance_count, decline_count,
                       thrust, computed_at
                FROM breadth_daily_history
                WHERE date = ANY(%s)
                ORDER BY date
            """, (candle_dates,))
            breadth_rows = cur.fetchall()
        else:
            breadth_rows = []

        # 3) Drop candle rows that lack a breadth match (drops candles_only).
        breadth_date_set = {r["date"] for r in breadth_rows}
        candle_rows = [r for r in candle_rows if r["date"] in breadth_date_set]

        candles = [{
            "time": r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"]),
            "open": round(float(r["open"] or 0), 2),
            "high": round(float(r["high"] or 0), 2),
            "low": round(float(r["low"] or 0), 2),
            "close": round(float(r["close"] or 0), 2),
            "volume": int(r["volume"] or 0),
        } for r in candle_rows]

        breadth = [{
            "date": str(r["date"]),
            "total": r["total_stocks"],
            "ema10": float(r["pct_above_ema10"] or 0),
            "ema20": float(r["pct_above_ema20"] or 0),
            "ema50": float(r["pct_above_ema50"] or 0),
            "ema200": float(r["pct_above_ema200"] or 0),
            "newHighs": r["new_highs"],
            "newLows": r["new_lows"],
            "advances": r["advance_count"],
            "declines": r["decline_count"],
            "thrust": float(r["thrust"] or 0),
        } for r in breadth_rows]

        latest_breadth = breadth_rows[-1] if breadth_rows else None
        latest_candle  = candles[-1] if candles else None

        return jsonify({
            "symbol": symbol,
            "candles": candles,
            "breadth": breadth,
            "asOf": (str(latest_breadth["date"]) if latest_breadth else None),
            "computedAt": (latest_breadth["computed_at"].isoformat()
                           if latest_breadth and latest_breadth["computed_at"] else None),
            "candleAsOf": (latest_candle["time"] if latest_candle else None),
            "counts": {"candles": len(candles), "breadth": len(breadth)},
        })
    except Exception as e:
        print(f"❌ breadth/index-dashboard error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@breadth_bp.route("/api/internal/breadth-finalise", methods=["POST"])
@limiter.limit("10 per minute")
def run_finalise():
    """Cloud Scheduler endpoint — runs _gap_fill_breadth() to write today's
    final row from stock_daily_summary live values after close. Hit at
    17:30 IST (finalise) and 18:30 IST (backstop) on trading days.
    Auth-exempt from JWT; validates X-Cron-Secret instead.
    """
    import os
    cron_secret = os.getenv("CRON_SECRET", "")
    if cron_secret:
        req_secret = request.headers.get("X-Cron-Secret", "").strip()
        if req_secret != cron_secret:
            return jsonify({"error": "Unauthorized"}), 403

    global _gap_fill_last_run
    _gap_fill_last_run = 0.0  # force the gap-fill to actually run
    _gap_fill_breadth()

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT date, pct_above_ema10, pct_above_ema20, pct_above_ema50,
                   pct_above_ema200, new_highs, new_lows, computed_at
            FROM breadth_daily_history
            ORDER BY date DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": True, "row": None})
        return jsonify({
            "ok": True,
            "row": {
                "date": str(row["date"]),
                "ema10": float(row["pct_above_ema10"] or 0),
                "ema20": float(row["pct_above_ema20"] or 0),
                "ema50": float(row["pct_above_ema50"] or 0),
                "ema200": float(row["pct_above_ema200"] or 0),
                "newHighs": row["new_highs"],
                "newLows": row["new_lows"],
                "computedAt": row["computed_at"].isoformat() if row["computed_at"] else None,
            },
        })
    finally:
        close_db(conn)
