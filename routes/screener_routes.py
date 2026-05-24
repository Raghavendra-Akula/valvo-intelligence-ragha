"""
Screener routes — Supabase-native scanner + Watchlist + Liquidity

Architecture (v2 — pre-computed summary):
  - stock_daily_summary: pre-computed MA50, MA200, 52W H/L, 19d turnover
    Refreshed once daily. Heavy ARRAY_AGG query runs ONCE, not per request.
  - Scan: reads summary + joins live candle for CMP/volume (~1s vs old 32s)
  - Liquidity: 19-day pre-computed sum + live today turnover = exact 20-day avg
  - Default filters: within 25% of 52W high, above 200 SMA, liquidity > 0.5 Cr
  - Optional: above 50 SMA (query param ma50=true)
  - Watchlist → Supabase CRUD
"""
from flask import Blueprint, jsonify, request, g
import requests
import threading
from datetime import datetime, timedelta, timezone
import time
from database.database import get_db, close_db
from config.settings import is_trading_day as _cfg_is_trading_day, is_market_open as _cfg_is_market_open, prev_trading_date_or_today, trading_days_until
from extensions import limiter
from routes.explore_routes import track_stock_view

screener_bp = Blueprint("screener", __name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Watchlist limits ──
MAX_WATCHLISTS_PER_USER = 25
MAX_ITEMS_PER_WATCHLIST = 200
MAX_NAME_LENGTH = 100
MAX_NOTES_LENGTH = 2000
MAX_SECTION_LENGTH = 50
MAX_REORDER_ITEMS = 200

# Background summary refresh — non-blocking, prevents duplicate refreshes
_summary_refresh_lock = threading.Lock()
_summary_refreshing = False

def _trigger_background_refresh():
    """Kick off summary refresh in a background thread if not already running."""
    global _summary_refreshing
    if _summary_refreshing:
        return  # already refreshing, skip
    with _summary_refresh_lock:
        if _summary_refreshing:
            return
        _summary_refreshing = True

    def _do_refresh():
        global _summary_refreshing
        try:
            print("🔄 Background summary refresh triggered — user won't wait")
            count = _refresh_summary()
            print(f"✅ Background refresh done: {count} stocks updated")
        except Exception as e:
            print(f"❌ Background refresh failed: {e}")
        finally:
            _summary_refreshing = False

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()

CONDITIONS = [
    "Within 25% of 250-day high",
    "Above 200 SMA",
    "Liquidity > 0.5 Cr (live 20-day avg)",
]

# ═══════════════════════════════════════════════════
# SCAN CACHE — in-memory for fast repeated requests
# ═══════════════════════════════════════════════════

def _empty_scan_cache():
    return {
        "stocks": [],
        "total": 0,
        "fetched_at": None,
        "fetched_date": None,
        "after_4pm": False,
        "source": None,
        "elapsed_ms": None,
        "summary_fresh": None,
    }


_cache = {
    "ma200_only": _empty_scan_cache(),
    "ma50_and_ma200": _empty_scan_cache(),
}


def _scan_cache_key(include_ma50=False):
    return "ma50_and_ma200" if include_ma50 else "ma200_only"


def _cache_entry(include_ma50=False):
    return _cache[_scan_cache_key(include_ma50)]


def _is_cache_valid(include_ma50=False):
    entry = _cache_entry(include_ma50)
    if not entry["fetched_at"] or not entry["stocks"]:
        return False
    now_ist = datetime.now(IST)
    today = now_ist.strftime("%Y-%m-%d")
    if entry["fetched_date"] != today:
        return False
    if now_ist.hour >= 16 and not entry["after_4pm"]:
        return False
    return True


def _clear_scan_cache():
    for key in _cache:
        _cache[key] = _empty_scan_cache()


# ═══════════════════════════════════════════════════
# SUMMARY TABLE — pre-computed daily metrics
# ═══════════════════════════════════════════════════

def _is_summary_fresh():
    """Check if stock_daily_summary has data for the current/last trading day.
    On holidays/weekends, the last trading day's data is considered fresh."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT computed_date FROM stock_daily_summary LIMIT 1")
        row = cur.fetchone()
        if not row:
            return False
        target = prev_trading_date_or_today().isoformat()
        return str(row["computed_date"]) >= target
    except Exception:
        return False
    finally:
        close_db(conn)


def _refresh_summary():
    """Fallback path: invoke the database function refresh_stock_daily_summary().

    The summary is normally kept fresh by three pg_cron jobs (safety 6 AM,
    morning 9 AM, evening 4 PM IST) — see Backend/database/cron_jobs.sql.
    This Python wrapper exists only so the manual /refresh-summary endpoint
    and the staleness fallback can re-trigger the DB function on demand.

    Live-price fan-out to positions_live and fundamentals_overview is done
    by trg_sync_live_price (see Backend/database/candles_triggers.sql) — no
    Python fan-out needed.

    Breadth daily history is appended here because the DB function does not
    yet handle it.
    """
    if not _cfg_is_trading_day():
        print("📅 Skipping summary refresh — today is not a trading day (holiday/weekend)")
        return 0
    conn = get_db()
    try:
        cur = conn.cursor()
        # 5 minute ceiling — same as the DB function's internal guard.
        cur.execute("SET LOCAL statement_timeout = '300000'")
        cur.execute("SELECT refresh_stock_daily_summary(%s) AS stocks_refreshed", ("backend_fallback",))
        row = cur.fetchone()
        count = int(row["stocks_refreshed"]) if row and row.get("stocks_refreshed") is not None else 0
        conn.commit()
        print(f"✅ Backend fallback summary refresh: {count} stocks (cron should normally do this)")

        # The DB function does not append today's breadth_daily_history row.
        try:
            _append_breadth_row(conn)
        except Exception as bh_err:
            print(f"⚠️ Breadth history append failed (non-fatal): {bh_err}")

        return count
    except Exception as e:
        print(f"❌ Backend fallback refresh failed: {e}")
        import traceback; traceback.print_exc()
        conn.rollback()
        return 0
    finally:
        close_db(conn)


def _append_breadth_row(conn):
    """Write today's settled breadth row into breadth_daily_history.

    Pulls today's close/high/low from candles_daily and compares against the
    prior trading day's close (also from candles_daily). This mirrors the
    gap-fill logic in breadth_routes._gap_fill_breadth so results are always
    internally consistent.

    Guardrails — never write a stale row:
      1. Skip if today isn't a trading day.
      2. Skip if market is still open — the session high/low/close isn't final.
      3. Skip if candles_daily has no row for today yet (EOD pipeline pending).

    The previous implementation used stock_daily_summary.prev_close as the
    current close, which mathematically forced advance_count=decline_count=0
    and treated yesterday's close as today's for new-high/new-low tests.
    """
    # 1) Don't corrupt the table with mid-session or non-trading-day data.
    if _cfg_is_market_open():
        print("⏭️  Breadth append skipped: market is open — wait for EOD")
        return
    if not _cfg_is_trading_day():
        print("⏭️  Breadth append skipped: not a trading day")
        return

    K20, K50, K200 = 2.0/21, 2.0/51, 2.0/201
    cur = conn.cursor()
    cur.execute("SET LOCAL statement_timeout = '20000'")

    # 2) Need today's candle in candles_daily. If the EOD write hasn't landed
    #    yet, abort — we'd rather leave the row missing than write garbage.
    cur.execute("""
        SELECT COUNT(*) AS n
        FROM candles_daily
        WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
    """)
    if (cur.fetchone() or {}).get("n", 0) == 0:
        print("⏭️  Breadth append skipped: no candles_daily row for today yet")
        return

    # 3) Compute breadth from candles_daily.
    #    `today` is the freshest candle at today's date.
    #    `prev`  is the freshest candle strictly before today.
    #    `prior_52w` is the rolling 365-day max/min EXCLUDING today, so a
    #    "new high" means today's high genuinely broke yesterday's 52W ceiling.
    cur.execute(f"""
        WITH today AS (
            SELECT DISTINCT ON (security_id) security_id, close, high, low, volume
            FROM candles_daily
            WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
            ORDER BY security_id, date DESC
        ),
        prev AS (
            SELECT DISTINCT ON (security_id)
                   security_id, close AS prev_close, volume AS prev_volume
            FROM candles_daily
            WHERE date < (NOW() AT TIME ZONE 'Asia/Kolkata')::date
            ORDER BY security_id, date DESC
        ),
        -- Pre-compute 5/10/20-day lookback closes (rn=N is N trading days before today).
        -- Adding more (lookback, threshold) pairs to MOMENTUM_PAIRS only requires one
        -- new COUNT(*) FILTER below — no further SQL surgery.
        lagged AS (
            SELECT security_id, close,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
            FROM candles_daily
            WHERE date < (NOW() AT TIME ZONE 'Asia/Kolkata')::date
              AND date >= (NOW() AT TIME ZONE 'Asia/Kolkata')::date - 30
              AND volume > 0
        ),
        close_lag AS (
            SELECT security_id,
                   MAX(close) FILTER (WHERE rn = 5)  AS c5,
                   MAX(close) FILTER (WHERE rn = 10) AS c10,
                   MAX(close) FILTER (WHERE rn = 20) AS c20
            FROM lagged
            WHERE rn IN (5, 10, 20)
            GROUP BY security_id
        ),
        prior_52w AS (
            SELECT security_id,
                   MAX(high) AS high_52w,
                   MIN(low)  AS low_52w
            FROM candles_daily
            WHERE date >= ((NOW() AT TIME ZONE 'Asia/Kolkata')::date - 365)
              AND date <  (NOW() AT TIME ZONE 'Asia/Kolkata')::date
            GROUP BY security_id
        ),
        stats AS (
            SELECT
                t.security_id,
                t.close,
                t.high AS today_high,
                t.low  AS today_low,
                t.volume AS today_vol,
                p.prev_close, p.prev_volume,
                COALESCE(w.high_52w, t.high) AS high_52w,
                COALESCE(w.low_52w,  t.low)  AS low_52w,
                cl.c5,
                t.close * {K20}  + COALESCE(s.ema20,  t.close) * {1-K20}  AS live_ema20,
                t.close * {K50}  + COALESCE(s.ema50,  t.close) * {1-K50}  AS live_ema50,
                t.close * {K200} + COALESCE(s.ema200, t.close) * {1-K200} AS live_ema200,
                su.sector
            FROM today t
            JOIN prev  p ON t.security_id = p.security_id
            LEFT JOIN prior_52w w ON t.security_id = w.security_id
            LEFT JOIN close_lag cl ON t.security_id = cl.security_id
            JOIN stock_universe su ON t.security_id = su.security_id
            LEFT JOIN stock_daily_summary s ON t.security_id = s.security_id
            WHERE COALESCE(s.is_etf, false) = false
              AND t.close > 0 AND p.prev_close > 0
        )
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE close > live_ema20)  AS a20,
            COUNT(*) FILTER (WHERE close > live_ema50)  AS a50,
            COUNT(*) FILTER (WHERE close > live_ema200) AS a200,
            COUNT(*) FILTER (WHERE today_high > high_52w) AS nh,
            COUNT(*) FILTER (WHERE today_low  < low_52w)  AS nl,
            COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.20) AS d20,
            COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.30) AS d30,
            COUNT(*) FILTER (WHERE (close - GREATEST(high_52w, today_high)) / NULLIF(GREATEST(high_52w, today_high), 0) < -0.50) AS d50,
            COUNT(*) FILTER (WHERE close > prev_close) AS adv,
            COUNT(*) FILTER (WHERE close < prev_close) AS dec,
            COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.20) AS up_20pc_5d,
            COUNT(*) FILTER (WHERE c5 > 0 AND (close - c5) / c5 > 0.30) AS up_30pc_5d,
            COUNT(*) FILTER (WHERE prev_close > 0 AND prev_volume > 0 AND today_vol > prev_volume
                AND (close - prev_close) / prev_close >=  0.04) AS up_4pc_vol,
            COUNT(*) FILTER (WHERE prev_close > 0 AND prev_volume > 0 AND today_vol > prev_volume
                AND (close - prev_close) / prev_close <= -0.04) AS down_4pc_vol
        FROM stats
    """)
    row = cur.fetchone()
    if not row or not row["total"]:
        print("⏭️  Breadth append skipped: no stats rows produced")
        return

    total = row["total"]
    thrust = round(100.0 * row["adv"] / max(row["adv"] + row["dec"], 1), 1)

    # 4) Sector breadth — same data source as above, grouped by sector.
    cur.execute(f"""
        WITH today AS (
            SELECT DISTINCT ON (security_id) security_id, close
            FROM candles_daily
            WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
            ORDER BY security_id, date DESC
        ),
        stats AS (
            SELECT
                t.close,
                t.close * {K20}  + COALESCE(s.ema20,  t.close) * {1-K20}  AS live_ema20,
                t.close * {K50}  + COALESCE(s.ema50,  t.close) * {1-K50}  AS live_ema50,
                t.close * {K200} + COALESCE(s.ema200, t.close) * {1-K200} AS live_ema200,
                su.sector
            FROM today t
            JOIN stock_universe su ON t.security_id = su.security_id
            LEFT JOIN stock_daily_summary s ON t.security_id = s.security_id
            WHERE COALESCE(s.is_etf, false) = false
              AND t.close > 0
              AND su.sector IS NOT NULL AND su.sector != ''
        )
        SELECT sector, COUNT(*) AS total,
            ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema20)  / NULLIF(COUNT(*), 0), 1) AS above20,
            ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema50)  / NULLIF(COUNT(*), 0), 1) AS above50,
            ROUND(100.0 * COUNT(*) FILTER (WHERE close > live_ema200) / NULLIF(COUNT(*), 0), 1) AS above200
        FROM stats GROUP BY sector HAVING COUNT(*) >= 5
    """)
    sector_rows = cur.fetchall()
    import json
    sector_json = json.dumps({
        r["sector"]: {"total": r["total"], "above20": float(r["above20"]), "above50": float(r["above50"]), "above200": float(r["above200"])}
        for r in sector_rows
    })

    cur.execute("""
        INSERT INTO breadth_daily_history
            (date, total_stocks, pct_above_ema20, pct_above_ema50, pct_above_ema200,
             new_highs, new_lows, pct_down_20, pct_down_30, pct_down_50,
             advance_count, decline_count, thrust, momentum_20pc,
             up_20pc_5d, up_30pc_5d, up_4pc_vol, down_4pc_vol, sector_breadth)
        VALUES ((NOW() AT TIME ZONE 'Asia/Kolkata')::date,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0,
                %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (date) DO UPDATE SET
            total_stocks     = EXCLUDED.total_stocks,
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
            sector_breadth   = EXCLUDED.sector_breadth,
            computed_at      = NOW()
    """, (
        total,
        round(100.0 * row["a20"] / total, 1),
        round(100.0 * row["a50"] / total, 1),
        round(100.0 * row["a200"] / total, 1),
        row["nh"], row["nl"],
        round(100.0 * row["d20"] / total, 1),
        round(100.0 * row["d30"] / total, 1),
        round(100.0 * row["d50"] / total, 1),
        row["adv"], row["dec"], thrust,
        row["up_20pc_5d"], row["up_30pc_5d"],
        row["up_4pc_vol"], row["down_4pc_vol"],
        sector_json,
    ))
    conn.commit()
    print(f"✅ Breadth history row written (total={total}, adv={row['adv']}, dec={row['dec']}, nh={row['nh']}, nl={row['nl']})")


def _scan_from_summary(include_ma50=False):
    """Fast scan: reads entirely from stock_daily_summary (~50ms).
    Live prices synced by database trigger on candles_daily writes.
    MA50 = precise reverse-math: (ma50_sum - close_50th + live_close) / 50
    Liquidity = (19d pre-computed sum + today live turnover) / 20 / 1Cr.

    Fresh IPOs (< 50 trading days, not yet in stock_daily_summary) are
    unioned in directly from candles_daily so they aren't silently dropped.
    For them and any low-history row still in summary (< 200 days), MA
    filters are bypassed — user-requested: any stock with enough liquidity
    that sits within 25% of its 52w high should be visible regardless of
    whether MAs are meaningful yet.
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '10000'")

        # live_price = today's trigger-synced close, or yesterday's close if market hasn't opened
        lp = "COALESCE(s.live_close, s.prev_close)"
        lh = "COALESCE(s.live_high, s.high_52w)"
        ll = "COALESCE(s.live_low, s.low_52w)"
        lv = "COALESCE(s.live_volume, 0)"

        # Precise live MA50: drop oldest close, add today's live close
        ma50_expr = f"""CASE WHEN s.ma50_sum > 0 AND s.close_50th > 0
            THEN (s.ma50_sum - s.close_50th + {lp}) / 50.0
            ELSE s.ma50 END"""

        # MA200 filter — strict for full-history stocks, bypassed for IPOs
        # with < 200 trading days (their ma200 is a partial average, not a
        # true 200-day MA, so the comparison would be misleading).
        ma200_filter = f"(COALESCE(s.trading_days, 0) < 200 OR {lp} > s.ma200)"
        # MA50 filter — same logic: only enforce when enough history exists.
        # (Stocks in summary always have >= 50 days, so this is a defensive
        # guard; the real bypass happens in Path B for < 50 day IPOs.)
        ma50_filter = (
            f"AND (COALESCE(s.trading_days, 0) < 50 OR {lp} > ({ma50_expr}))"
            if include_ma50 else ""
        )
        # Path B fresh IPOs have no MA at all — when ma50=true is requested,
        # they should still be visible per user intent. No extra filter here.

        cur.execute(f"""
            -- Path A: stocks already in stock_daily_summary (>= 50 trading days)
            SELECT
                s.security_id,
                s.symbol as nsecode,
                s.company_name as name,
                {lp} as close,
                {lv} as volume,
                CASE WHEN s.prev_close > 0
                    THEN ROUND((({lp} - s.prev_close) / s.prev_close * 100)::numeric, 2)
                    ELSE 0 END as pchange,
                GREATEST(s.high_52w, {lh}) as high_52w,
                LEAST(s.low_52w, {ll}) as low_52w,
                ROUND((({lp} - GREATEST(s.high_52w, {lh})) /
                       NULLIF(GREATEST(s.high_52w, {lh}), 0) * 100)::numeric, 2) as pct_from_high,
                CASE WHEN COALESCE(s.trading_days, 0) >= 50 THEN ({lp} > ({ma50_expr})) ELSE NULL END as above_ma50,
                CASE WHEN COALESCE(s.trading_days, 0) >= 200 THEN ({lp} > s.ma200) ELSE NULL END as above_ma200,
                CASE WHEN COALESCE(s.trading_days, 0) >= 50 THEN ROUND(({ma50_expr})::numeric, 2) ELSE NULL END as ma50,
                CASE WHEN COALESCE(s.trading_days, 0) >= 200 THEN ROUND(s.ma200::numeric, 2) ELSE NULL END as ma200,
                CASE WHEN {lv} > 0 THEN
                    ROUND(((s.turnover_19d_sum + {lv}::double precision * {lp}) /
                           GREATEST(s.turnover_19d_count + 1, 1) / 10000000.0)::numeric, 2)
                ELSE s.liq_cr END as liq_cr,
                s.last_hist_date as last_date,
                COALESCE(su.valvo_sector, su.sector) as sector,
                COALESCE(st.themes, '[]'::jsonb) as themes,
                COALESCE(s.trading_days, 0) as trading_days,
                FALSE as is_fresh_ipo
            FROM stock_daily_summary s
            LEFT JOIN stock_universe su ON su.security_id = s.security_id
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(theme_slug ORDER BY rn) as themes
                FROM (
                    SELECT theme_slug,
                           ROW_NUMBER() OVER (ORDER BY is_primary DESC, exposure_score DESC) as rn
                    FROM stock_themes_v2
                    WHERE security_id = s.security_id
                ) x
                WHERE rn <= 2
            ) st ON true
            WHERE
                s.is_etf = false
                AND {ma200_filter}
                AND {lp} >= 0.75 * GREATEST(s.high_52w, {lh})
                AND CASE WHEN {lv} > 0 THEN
                    (s.turnover_19d_sum + {lv}::double precision * {lp}) /
                    GREATEST(s.turnover_19d_count + 1, 1) / 10000000.0
                ELSE s.liq_cr END > 0.5
                {ma50_filter}

            UNION ALL

            -- Path B: fresh IPOs (< 50 trading days, NOT yet in summary).
            -- MA gates are skipped entirely — they need only the liquidity
            -- and "within 25% of 52w high (= ATH so far)" gates to qualify.
            -- Pattern: shrink to candidate sids via stock_universe first
            -- (the missing-from-summary anti-join is tiny ~few hundred rows),
            -- then aggregate candles_daily for just those sids.
            SELECT
                fr.security_id,
                u.symbol as nsecode,
                u.company_name as name,
                latest.close,
                latest.volume,
                CASE WHEN prev.close > 0
                    THEN ROUND(((latest.close - prev.close) / prev.close * 100)::numeric, 2)
                    ELSE 0 END as pchange,
                fr.ath as high_52w,
                fr.low_52w as low_52w,
                ROUND(((latest.close - fr.ath) / NULLIF(fr.ath, 0) * 100)::numeric, 2) as pct_from_high,
                NULL::boolean as above_ma50,
                NULL::boolean as above_ma200,
                NULL::numeric as ma50,
                NULL::numeric as ma200,
                fr.liq_cr,
                latest.date as last_date,
                COALESCE(u.valvo_sector, u.sector) as sector,
                COALESCE(st.themes, '[]'::jsonb) as themes,
                fr.trading_days,
                TRUE as is_fresh_ipo
            FROM (
                WITH missing_sids AS (
                    SELECT u2.security_id
                    FROM stock_universe u2
                    WHERE u2.is_active = true
                      AND COALESCE(u2.is_etf, false) = false
                      AND NOT EXISTS (
                          SELECT 1 FROM stock_daily_summary s2
                          WHERE s2.security_id = u2.security_id
                      )
                )
                SELECT cd.security_id,
                       COUNT(*) as trading_days,
                       MAX(cd.high) as ath,
                       MIN(cd.low) as low_52w,
                       ROUND((AVG(cd.close * cd.volume) / 10000000.0)::numeric, 2) as liq_cr
                FROM candles_daily cd
                JOIN missing_sids ms ON cd.security_id = ms.security_id
                WHERE cd.volume > 0
                GROUP BY cd.security_id
                HAVING COUNT(*) < 50
                   AND COUNT(*) >= 1
                   AND MIN(cd.date) >= CURRENT_DATE - 365
            ) fr
            JOIN stock_universe u ON u.security_id = fr.security_id
            JOIN LATERAL (
                SELECT cd.close, cd.volume, cd.date
                FROM candles_daily cd
                WHERE cd.security_id = fr.security_id AND cd.volume > 0
                ORDER BY cd.date DESC
                LIMIT 1
            ) latest ON true
            LEFT JOIN LATERAL (
                SELECT cd.close
                FROM candles_daily cd
                WHERE cd.security_id = fr.security_id
                  AND cd.volume > 0
                  AND cd.date < latest.date
                ORDER BY cd.date DESC
                LIMIT 1
            ) prev ON true
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(theme_slug ORDER BY rn) as themes
                FROM (
                    SELECT theme_slug,
                           ROW_NUMBER() OVER (ORDER BY is_primary DESC, exposure_score DESC) as rn
                    FROM stock_themes_v2
                    WHERE security_id = fr.security_id
                ) x
                WHERE rn <= 2
            ) st ON true
            WHERE latest.close >= 0.75 * fr.ath
              AND fr.liq_cr > 0.5
              AND COALESCE(u.company_name, '') NOT LIKE '%%AMC - %%'
              AND u.symbol NOT SIMILAR TO '%%(GOLD|SILVER|NIFTY|BANKBEES|LIQUID|DEBT)%%'

            ORDER BY liq_cr DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        fresh_ipo_count = 0
        for r in rows:
            if r.get("last_date"):
                r["last_date"] = str(r["last_date"])
            r["liquidity"] = float(r.get("liq_cr") or 0)
            if r.get("is_fresh_ipo"):
                r["liquidity_source"] = "ipo_candles"
                fresh_ipo_count += 1
            else:
                r["liquidity_source"] = "sma"
        print(
            f"✅ Fast scan returned {len(rows)} stocks "
            f"(summary + live, ma50={'precise' if include_ma50 else 'off'}, "
            f"fresh_ipos={fresh_ipo_count})"
        )
        return {"stocks": rows, "total": len(rows)}
    except Exception as e:
        print(f"❌ Fast scan failed: {e}")
        import traceback; traceback.print_exc()
        return None
    finally:
        close_db(conn)


def _get_scan_counts():
    """Get stock counts for both scan modes in a single fast query.
    Returns {ma200_only: N, ma50_and_ma200: N} for card display.

    Mirrors _scan_from_summary's filter logic — MA gates are bypassed for
    low-history rows (< 200 trading days in summary, plus fresh IPOs from
    candles_daily that haven't entered summary yet) so the card numbers
    match the actual scan output."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '8000'")
        cur.execute("""
            WITH summary_base AS (
                SELECT
                    s.security_id,
                    COALESCE(s.trading_days, 0) as td,
                    COALESCE(s.live_close, s.prev_close) as lp,
                    -- pass_ma200: bypass for IPOs with < 200 trading days
                    (COALESCE(s.trading_days, 0) < 200
                     OR COALESCE(s.live_close, s.prev_close) > s.ma200) as pass_ma200,
                    -- pass_ma50: bypass for stocks with < 50 trading days (defensive)
                    (COALESCE(s.trading_days, 0) < 50
                     OR COALESCE(s.live_close, s.prev_close) > CASE
                        WHEN s.ma50_sum > 0 AND s.close_50th > 0
                        THEN (s.ma50_sum - s.close_50th + COALESCE(s.live_close, s.prev_close)) / 50.0
                        ELSE s.ma50 END) as pass_ma50,
                    COALESCE(s.live_close, s.prev_close) >= 0.75 * GREATEST(s.high_52w, COALESCE(s.live_high, s.high_52w)) as pass_52w,
                    CASE WHEN COALESCE(s.live_volume, 0) > 0 THEN
                        (s.turnover_19d_sum + COALESCE(s.live_volume, 0)::double precision * COALESCE(s.live_close, s.prev_close)) /
                        GREATEST(s.turnover_19d_count + 1, 1) / 10000000.0
                    ELSE s.liq_cr END as liq
                FROM stock_daily_summary s
                WHERE s.is_etf = false AND s.prev_close > 0
            ),
            -- Fresh IPOs (< 50 trading days, not in summary) — same gates as Path B.
            -- MA filters always pass (no MA available).
            fresh_ipos AS (
                SELECT cd.security_id,
                       MAX(cd.high) as ath,
                       ROUND((AVG(cd.close * cd.volume) / 10000000.0)::numeric, 2) as liq_cr
                FROM candles_daily cd
                JOIN stock_universe u2 ON u2.security_id = cd.security_id
                WHERE cd.volume > 0
                  AND u2.is_active = true
                  AND COALESCE(u2.is_etf, false) = false
                  AND NOT EXISTS (
                      SELECT 1 FROM stock_daily_summary s2 WHERE s2.security_id = cd.security_id
                  )
                GROUP BY cd.security_id
                HAVING COUNT(*) < 50
                   AND COUNT(*) >= 1
                   AND MIN(cd.date) >= CURRENT_DATE - 365
            ),
            fresh_pass AS (
                SELECT fr.security_id, fr.liq_cr,
                       latest.close >= 0.75 * fr.ath as pass_52w
                FROM fresh_ipos fr
                JOIN stock_universe u ON u.security_id = fr.security_id
                JOIN LATERAL (
                    SELECT cd.close
                    FROM candles_daily cd
                    WHERE cd.security_id = fr.security_id AND cd.volume > 0
                    ORDER BY cd.date DESC
                    LIMIT 1
                ) latest ON true
                WHERE COALESCE(u.company_name, '') NOT LIKE '%%AMC - %%'
                  AND u.symbol NOT SIMILAR TO '%%(GOLD|SILVER|NIFTY|BANKBEES|LIQUID|DEBT)%%'
            )
            SELECT
                (SELECT COUNT(*) FROM summary_base
                 WHERE pass_ma200 AND pass_52w AND liq > 0.5)
                + (SELECT COUNT(*) FROM fresh_pass WHERE pass_52w AND liq_cr > 0.5) as ma200_only,
                (SELECT COUNT(*) FROM summary_base
                 WHERE pass_ma200 AND pass_ma50 AND pass_52w AND liq > 0.5)
                + (SELECT COUNT(*) FROM fresh_pass WHERE pass_52w AND liq_cr > 0.5) as ma50_and_ma200
        """)
        row = cur.fetchone()
        return {
            "ma200_only": row["ma200_only"] if row else 0,
            "ma50_and_ma200": row["ma50_and_ma200"] if row else 0,
        }
    except Exception as e:
        print(f"❌ Scan counts failed: {e}")
        return {"ma200_only": 0, "ma50_and_ma200": 0}
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# LIVE PRICES — refresh from candles_daily (same source as charts)
# ═══════════════════════════════════════════════════

def _refresh_prices_from_supabase(stocks):
    """Overwrite ChartInk close/pchange with live data from candles_daily.
    This ensures the sidebar list always matches the chart."""
    if not stocks:
        return

    sids = [s["security_id"] for s in stocks if s.get("security_id")]
    if not sids:
        return

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        # Get latest REAL candle + previous REAL candle to compute real pchange
        # Skips holiday/weekend duplicate candles (volume=0)
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (security_id) security_id, date, close, volume
                FROM candles_daily
                WHERE security_id = ANY(%s) AND volume > 0
                ORDER BY security_id, date DESC
            ),
            prev AS (
                SELECT DISTINCT ON (security_id) security_id, close as prev_close
                FROM candles_daily
                WHERE security_id = ANY(%s) AND volume > 0
                    AND (security_id, date) NOT IN (SELECT security_id, date FROM latest)
                ORDER BY security_id, date DESC
            )
            SELECT l.security_id, l.close, l.volume,
                CASE WHEN p.prev_close > 0
                    THEN ROUND(((l.close - p.prev_close) / p.prev_close * 100)::numeric, 2)
                    ELSE 0
                END as pchange
            FROM latest l
            LEFT JOIN prev p ON l.security_id = p.security_id
        """, (sids, sids))

        price_map = {}
        for r in cur.fetchall():
            price_map[str(r["security_id"])] = {
                "close": float(r["close"]),
                "pchange": float(r["pchange"]),
                "volume": int(r["volume"] or 0),
            }

        updated = 0
        for s in stocks:
            sid = str(s.get("security_id", ""))
            if sid in price_map:
                s["close"] = price_map[sid]["close"]
                s["pchange"] = price_map[sid]["pchange"]
                s["volume"] = price_map[sid]["volume"]
                updated += 1

        print(f"💰 Prices refreshed: {updated}/{len(stocks)} stocks from candles_daily")
    except Exception as e:
        print(f"❌ Price refresh error: {e}")
    finally:
        if conn:
            close_db(conn)


# ═══════════════════════════════════════════════════
# RESOLVE + CACHE
# ═══════════════════════════════════════════════════

def _resolve_and_cache(include_ma50, stocks, total, source="summary_live", elapsed_ms=None, summary_fresh=None):
    """Cache scan results. Stocks already have all fields from Supabase SQL."""
    now_ist = datetime.now(IST)
    entry = _cache_entry(include_ma50)
    entry["stocks"] = stocks
    entry["total"] = total
    entry["fetched_at"] = now_ist
    entry["fetched_date"] = now_ist.strftime("%Y-%m-%d")
    entry["after_4pm"] = now_ist.hour >= 16
    entry["source"] = source
    entry["elapsed_ms"] = elapsed_ms
    entry["summary_fresh"] = summary_fresh
    return stocks


# ═══════════════════════════════════════════════════
# SCANNER ENDPOINT (v2 — summary + live candle)
# ═══════════════════════════════════════════════════

@screener_bp.route("/api/screener/scan", methods=["GET"])
def run_scan():
    started = time.perf_counter()
    force = request.args.get("refresh", "").lower() == "true"
    include_ma50 = request.args.get("ma50", "").lower() == "true"
    mode_key = _scan_cache_key(include_ma50)
    cache_entry = _cache_entry(include_ma50)

    # 1. In-memory cache hit → instant response
    if not force and _is_cache_valid(include_ma50):
        return jsonify({
            "stocks": cache_entry["stocks"], "total": cache_entry["total"],
            "cached": True,
            "fetched_at": cache_entry["fetched_at"].strftime("%Y-%m-%d %H:%M IST"),
            "conditions": CONDITIONS + (["Above 50 SMA"] if include_ma50 else []),
            "scan_mode": mode_key,
            "source": cache_entry["source"] or "memory_cache",
            "elapsed_ms": cache_entry["elapsed_ms"],
            "served_in_ms": round((time.perf_counter() - started) * 1000, 1),
            "summary_fresh": cache_entry["summary_fresh"],
        })

    summary_fresh = _is_summary_fresh()

    # 2. If summary is not fresh, trigger background refresh (non-blocking)
    #    User still gets results from stale summary (~1.5s) while refresh runs silently
    if not summary_fresh:
        print("⚠️ Summary not fresh — triggering background refresh, serving stale data now")
        _trigger_background_refresh()

    # 3. Always use fast summary path — never the legacy 30s ARRAY_AGG query
    result = _scan_from_summary(include_ma50)
    source = "summary_live" if summary_fresh else "summary_live_stale_refreshing"

    if not result or not result.get("stocks"):
        # Summary returned 0 stocks — serve last cache if available
        if cache_entry["stocks"]:
            print("⚠️ Fast scan returned 0 stocks — serving last cached result")
            return jsonify({
                "stocks": cache_entry["stocks"], "total": cache_entry["total"],
                "cached": True, "stale": True,
                "fetched_at": cache_entry["fetched_at"].strftime("%Y-%m-%d %H:%M IST") if cache_entry["fetched_at"] else None,
                "conditions": CONDITIONS + (["Above 50 SMA"] if include_ma50 else []),
                "scan_mode": mode_key,
                "source": "memory_cache_stale_refreshing",
                "elapsed_ms": cache_entry["elapsed_ms"],
                "served_in_ms": round((time.perf_counter() - started) * 1000, 1),
                "summary_fresh": False,
            })
        # No cache either — return empty with refreshing flag
        return jsonify({
            "stocks": [], "total": 0,
            "conditions": CONDITIONS + (["Above 50 SMA"] if include_ma50 else []),
            "scan_mode": mode_key,
            "source": "empty_refreshing",
            "summary_fresh": False,
            "refreshing": True,
            "served_in_ms": round((time.perf_counter() - started) * 1000, 1),
        })

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    cleaned = _resolve_and_cache(
        include_ma50,
        result["stocks"],
        result.get("total", 0),
        source=source,
        elapsed_ms=elapsed_ms,
        summary_fresh=summary_fresh,
    )

    # Log daily scan count (background — don't block response)
    try:
        gainers = len([s for s in cleaned if (float(s.get("pchange", 0) or 0)) > 0])
        losers = len([s for s in cleaned if (float(s.get("pchange", 0) or 0)) < 0])
        avg_chg = sum(float(s.get("pchange", 0) or 0) for s in cleaned) / max(len(cleaned), 1)
        _log_scan_count(len(cleaned), gainers, losers, avg_chg)
    except Exception:
        pass

    return jsonify({
        "stocks": cleaned, "total": len(cleaned),
        "cached": False,
        "fetched_at": cache_entry["fetched_at"].strftime("%Y-%m-%d %H:%M IST"),
        "conditions": CONDITIONS + (["Above 50 SMA"] if include_ma50 else []),
        "scan_mode": mode_key,
        "source": source,
        "elapsed_ms": elapsed_ms,
        "served_in_ms": elapsed_ms,
        "summary_fresh": summary_fresh,
    })


# ═══════════════════════════════════════════════════
# ADMIN: Manual summary refresh
# ═══════════════════════════════════════════════════

@screener_bp.route("/api/screener/refresh-summary", methods=["POST"])
def refresh_summary_endpoint():
    """Admin endpoint to manually trigger summary table refresh."""
    count = _refresh_summary()
    if count > 0:
        # Invalidate in-memory cache so next scan uses fresh summary
        _clear_scan_cache()
        return jsonify({"ok": True, "stocks_refreshed": count})
    return jsonify({"error": "Summary refresh failed"}), 500


@screener_bp.route("/api/screener/scan-counts", methods=["GET"])
def scan_counts_endpoint():
    """Fast counts for both scan modes — used by landing page cards.
    Returns {ma200_only: N, ma50_and_ma200: N} with live prices.
    Single call — no pre-check DB round-trips."""
    counts = _get_scan_counts()
    return jsonify(counts)


@screener_bp.route("/api/screener/day-movers", methods=["GET"])
def day_movers_endpoint():
    """Top gainers + top losers. Default returns 5+5 for the landing
    cards; pass ?limit=N (max 500) for a richer set used by the dedicated
    Top Gainers / Top Losers page (so its 1%+/3%+/5%+ filter buttons have
    enough data to chew on instead of hitting the empty-state on every
    slow day). Uses summary on trading days, falls back to last-2-dates
    comparison on holidays/weekends."""
    try:
        raw_limit = int(request.args.get("limit", 5))
    except (TypeError, ValueError):
        raw_limit = 5
    limit = max(1, min(raw_limit, 500))
    conn = None
    try:
        conn = get_db()
        if not conn:
            return jsonify({"gainers": [], "losers": [], "error": "DB unavailable"}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '8000'")

        rows = []

        # Try trading-day query first (uses live_close from trigger)
        if _cfg_is_trading_day():
            cur.execute("""
                WITH movers AS (
                    SELECT s.security_id, s.symbol as nsecode, s.company_name as name,
                        COALESCE(s.live_close, s.prev_close) as close,
                        s.prev_close as prev_close,
                        s.liq_cr as liq_cr,
                        COALESCE(u.valvo_sector, u.sector) as sector,
                        CASE WHEN s.prev_close > 0
                            THEN ROUND(((COALESCE(s.live_close, s.prev_close) - s.prev_close) / s.prev_close * 100)::numeric, 2)
                            ELSE 0 END as pchange
                    FROM stock_daily_summary s
                    LEFT JOIN stock_universe u ON s.security_id = u.security_id
                    WHERE s.is_etf = false AND s.prev_close > 0 AND s.liq_cr > 0.5
                )
                (SELECT * FROM movers WHERE pchange > 0 ORDER BY pchange DESC LIMIT %s)
                UNION ALL
                (SELECT * FROM movers WHERE pchange < 0 ORDER BY pchange ASC LIMIT %s)
            """, (limit, limit))
            rows = [dict(r) for r in cur.fetchall()]

        # Fallback: if no movers (non-trading day OR market not open yet),
        # compare last 2 trading dates from candles_daily
        if not rows:
            cur.execute("""
                WITH dates AS (
                    SELECT DISTINCT date FROM candles_daily
                    WHERE date <= CURRENT_DATE AND volume > 0
                    ORDER BY date DESC LIMIT 2
                ),
                latest AS (
                    SELECT security_id, close FROM candles_daily
                    WHERE date = (SELECT date FROM dates ORDER BY date DESC LIMIT 1)
                ),
                prev AS (
                    SELECT security_id, close as prev_close FROM candles_daily
                    WHERE date = (SELECT date FROM dates ORDER BY date ASC LIMIT 1)
                ),
                movers AS (
                    SELECT l.security_id, su.symbol as nsecode, su.company_name as name,
                        l.close,
                        p.prev_close as prev_close,
                        s.liq_cr as liq_cr,
                        COALESCE(su.valvo_sector, su.sector) as sector,
                        CASE WHEN p.prev_close > 0
                            THEN ROUND(((l.close - p.prev_close) / p.prev_close * 100)::numeric, 2)
                            ELSE 0 END as pchange
                    FROM latest l
                    JOIN prev p ON l.security_id = p.security_id
                    JOIN stock_universe su ON l.security_id = su.security_id
                    JOIN stock_daily_summary s ON l.security_id = s.security_id
                    WHERE COALESCE(su.is_etf, false) = false AND s.liq_cr > 0.5
                )
                (SELECT * FROM movers WHERE pchange > 0 ORDER BY pchange DESC LIMIT %s)
                UNION ALL
                (SELECT * FROM movers WHERE pchange < 0 ORDER BY pchange ASC LIMIT %s)
            """, (limit, limit))
            rows = [dict(r) for r in cur.fetchall()]

        gainers = [r for r in rows if float(r["pchange"]) > 0]
        losers = [r for r in rows if float(r["pchange"]) < 0]
        return jsonify({"gainers": gainers, "losers": losers})
    except Exception as e:
        print(f"❌ Day movers failed: {e}")
        import traceback; traceback.print_exc()
        print(f"[screener] error: {e}")
        return jsonify({"gainers": [], "losers": [], "error": "Internal error"}), 500
    finally:
        if conn:
            close_db(conn)


# ═══════════════════════════════════════════════════
# ETF SCANNER — list active ETFs with key metrics
# ═══════════════════════════════════════════════════

# (pattern, category) — first match wins. Compiled at import time.
# Order is deliberate: more specific themes first, broader ones later, so
# e.g. "Nifty India Defence" stops at `defence` and never falls into `psu`.
# We mostly use substring containment (no \b) because ETF names regularly
# embed keywords in compound tokens — "Midcap150", "Ssensex", "GSEC10ABSL",
# "MOLOWVOL" — where word boundaries would silently fail.
import re as _re
_ETF_RULES = [
    (_re.compile(r"defence|defense"),                                       "defence"),
    (_re.compile(r"\brail"),                                                "railway"),
    (_re.compile(r"\bgold\b|bullion"),                                      "gold"),
    (_re.compile(r"silver"),                                                "silver"),
    (_re.compile(r"liquid|overnight|cash"),                                 "liquid"),
    (_re.compile(r"bharat\s*bond|gsec|g-sec|gilt|govt"),                    "bond"),
    (_re.compile(r"bfsi|financial|capital\s*market|cap\.?\s*markets|insurance"), "financial"),
    (_re.compile(r"realty|real\s*estate"),                                  "realty"),
    (_re.compile(r"metal"),                                                 "metal"),
    (_re.compile(r"chemical"),                                              "chemicals"),
    (_re.compile(r"pharma|healthcare|hospital"),                            "pharma"),
    (_re.compile(r"\bauto\b|automotive|ev\s*&|ev\s*new"),                   "auto"),
    (_re.compile(r"manufactur|\bmfg\b|make[\s-]?in[\s-]?india|makeindia"),  "manufacturing"),
    (_re.compile(r"internet|digital|fang"),                                 "internet"),
    (_re.compile(r"\bpower\b"),                                             "power"),
    (_re.compile(r"infra"),                                                 "infra"),
    (_re.compile(r"\benergy\b|\boil\b|\bgas\b"),                            "energy"),
    (_re.compile(r"commodit"),                                              "commodity"),
    (_re.compile(r"tourism|hospitalit"),                                    "tourism"),
    (_re.compile(r"\bpsu\b|\bpse\b|cpse|psbk|public\s*sector"),             "psu"),
    (_re.compile(r"bank"),                                                  "bank"),
    (_re.compile(r"consumption|consumer|fmcg|\bcons\b|new\s*age\s*cons"),   "consumption"),
    (_re.compile(r"\bit\s*etf|tech\s*etf|technology|infotech|software"),    "it"),
    # Broad market — Nifty 50/100/200/500/Next 50, Sensex, Total Market,
    # Top-N Equal Weight, Bharat 22, BSE 200/500, Service Sector, MNC.
    (_re.compile(r"nifty\s*50|nifty\s*100|nifty\s*200|nifty\s*500|nifty\s*next\s*50|sensex|total\s*market|bse\s*200|bse\s*500|bharat\s*22|top\s*\d+|services?\s*sector|largemidcap|\bmnc\b"), "broad"),
    # Factor / smart-beta / IPO / div leaders / equal weight / momentum
    (_re.compile(r"alpha|low\s*vol|quality|momentum|momtm|value|dividend|div\s*leaders?|esg|eq[\s.]*weight|equal[\s-]*weight|select\s*ipo|growth\s*sectors"), "factor"),
    # Mid / small caps. Match midsmall, mid 150, m150, midcpnifty, mc 150 too.
    (_re.compile(r"midcap|mid\s*cap|mid\s*150|midsmall|midcpnifty|m[\s-]?150|mc\s*150"), "midcap"),
    (_re.compile(r"smallcap|small\s*cap|\bsml\b"),                          "smallcap"),
    # International — country / global benchmarks
    (_re.compile(r"msci|china|japan|taiwan|hang|nasdaq|s&p\s*500|\bus\s|world|global|nyse"), "international"),
]
def _classify_etf(name, symbol=None):
    # Combine name + symbol so theme-bearing tickers (MIDSMALL, BANKPSU,
    # GROWWRAIL) classify even when the AMC's filed company_name is generic.
    s = " ".join(filter(None, [(name or "").lower(), (symbol or "").lower()]))
    if not s.strip():
        return "other"
    for pat, cat in _ETF_RULES:
        if pat.search(s):
            return cat
    return "other"


@screener_bp.route("/api/screener/etfs", methods=["GET"])
def etfs_endpoint():
    """All active ETFs with day-change + multi-period returns + 52W + MAs +
    liquidity. Universe is small (~320), so this runs as a single direct
    query against stock_daily_summary — no caching needed.

    Categorization is done in Python (see _classify_etf) so the keyword
    rules are easy to read and extend. Categories include defence, railway,
    realty, metal, chemicals, financial, bond, manufacturing, internet,
    power, etc. — the frontend filters on these.

    Response: { etfs: [...], total, fetched_at }
    """
    import time as _time
    started = _time.perf_counter()
    conn = None
    try:
        conn = get_db()
        if not conn:
            return jsonify({"etfs": [], "error": "DB unavailable"}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '8000'")
        cur.execute("""
            SELECT
                s.security_id,
                s.symbol AS nsecode,
                s.company_name AS name,
                COALESCE(s.live_close, s.prev_close) AS close,
                s.prev_close,
                s.high_52w, s.low_52w,
                s.ma50, s.ma200,
                s.close_5d, s.close_20d, s.close_60d, s.close_252d,
                s.liq_cr,
                CASE WHEN s.prev_close > 0
                    THEN ROUND(((COALESCE(s.live_close, s.prev_close) - s.prev_close) / s.prev_close * 100)::numeric, 2)
                    ELSE 0 END AS pchange,
                CASE WHEN s.close_5d  > 0
                    THEN ROUND(((COALESCE(s.live_close, s.prev_close) - s.close_5d)  / s.close_5d  * 100)::numeric, 2) END AS ret_5d,
                CASE WHEN s.close_20d > 0
                    THEN ROUND(((COALESCE(s.live_close, s.prev_close) - s.close_20d) / s.close_20d * 100)::numeric, 2) END AS ret_20d,
                CASE WHEN s.close_60d > 0
                    THEN ROUND(((COALESCE(s.live_close, s.prev_close) - s.close_60d) / s.close_60d * 100)::numeric, 2) END AS ret_60d,
                CASE WHEN s.close_252d > 0
                    THEN ROUND(((COALESCE(s.live_close, s.prev_close) - s.close_252d) / s.close_252d * 100)::numeric, 2) END AS ret_252d
            FROM stock_daily_summary s
            JOIN stock_universe u ON u.security_id = s.security_id
            WHERE s.is_etf = TRUE
              AND u.is_active = TRUE
              AND s.prev_close IS NOT NULL
              AND s.prev_close > 0
            ORDER BY s.liq_cr DESC NULLS LAST, s.symbol ASC
        """)
        etfs = [dict(r) for r in cur.fetchall()]
        for r in etfs:
            r["category"] = _classify_etf(r.get("name"), r.get("nsecode"))

        elapsed_ms = round((_time.perf_counter() - started) * 1000, 1)
        return jsonify({
            "etfs": etfs,
            "total": len(etfs),
            "elapsed_ms": elapsed_ms,
        })
    except Exception as e:
        print(f"❌ ETFs endpoint failed: {e}")
        import traceback; traceback.print_exc()
        return jsonify({"etfs": [], "error": "Internal error"}), 500
    finally:
        if conn:
            close_db(conn)


# ═══════════════════════════════════════════════════
# LIVE PRICE REFRESH — frontend calls every 30s
# ═══════════════════════════════════════════════════

@screener_bp.route("/api/screener/refresh-prices", methods=["GET"])
def refresh_screener_prices():
    """Refresh prices for all cached screener stocks from candles_daily.
    Returns updated stocks with live close + pchange from websocket data.
    Frontend should call this every 30s during market hours."""
    stocks = _cache.get("stocks", [])
    if not stocks:
        return jsonify({"error": "No scan data — run scan first", "stocks": []}), 404

    _refresh_prices_from_supabase(stocks)
    return jsonify({
        "stocks": stocks,
        "total": len(stocks),
        "refreshed": True,
        "fetched_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"),
    })


# ═══════════════════════════════════════════════════
# DEBUG
# ═══════════════════════════════════════════════════

@screener_bp.route("/api/screener/debug", methods=["GET"])
def screener_debug():
    stocks = _cache.get("stocks", [])
    sma = [s for s in stocks if s.get("liquidity_source") == "sma"]
    pending = [s for s in stocks if s.get("liquidity_source") != "sma"]
    return jsonify({
        "cache_total": len(stocks),
        "cache_date": _cache.get("fetched_date"),
        "data_source": "supabase_websocket",
        "liquidity_sma": len(sma),
        "liquidity_pending": len(pending),
        "pending_names": [s["nsecode"] for s in pending[:20]],
        "top_10_liquidity": sorted(
            [{"n": s["nsecode"], "l": s["liquidity"]} for s in stocks if s.get("liquidity")],
            key=lambda x: x["l"], reverse=True
        )[:10],
    })


# ═══════════════════════════════════════════════════
# SYNC MISSING STOCKS
# Downloads instruments CSV, finds stocks in
# index_constituents with no security_id, inserts them
# ═══════════════════════════════════════════════════

@screener_bp.route("/api/admin/sync-missing-stocks", methods=["POST"])
def sync_missing_stocks():
    import csv, io, requests as http_req
    from database.database import get_db, close_db
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT
                ic.stock_symbol,
                ic.stock_name,
                ic.security_id as existing_security_id,
                su.security_id as stock_universe_security_id
            FROM index_constituents ic
            LEFT JOIN stock_universe su
              ON su.symbol = ic.stock_symbol
             AND su.is_active = true
            WHERE ic.security_id IS NULL
               OR BTRIM(COALESCE(ic.security_id, '')) = ''
               OR ic.security_id = 'UNMAPPED'
               OR (su.security_id IS NOT NULL AND ic.security_id IS DISTINCT FROM su.security_id)
            ORDER BY ic.stock_symbol
        """)
        missing = [dict(r) for r in cur.fetchall()]
        if not missing:
            return jsonify({"message": "No missing stocks", "synced": 0})
        missing_symbols = {m["stock_symbol"] for m in missing}
        missing_names = {m["stock_symbol"]: m["stock_name"] for m in missing}
        stock_universe_sid_map = {
            m["stock_symbol"]: m["stock_universe_security_id"]
            for m in missing if m.get("stock_universe_security_id")
        }

        resp = http_req.get("https://images.dhan.co/api/instruments/nse_equities.csv", timeout=30)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        instrument_map = {}
        for row in reader:
            sym = (row.get("SEM_TRADING_SYMBOL") or row.get("SEM_CUSTOM_SYMBOL") or "").strip().upper()
            sid = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
            name = (row.get("SM_SYMBOL_NAME") or row.get("SEM_INSTRUMENT_NAME") or "").strip()
            series = (row.get("SEM_SERIES") or "").strip()
            if sym and sid and series in ("EQ", ""):
                instrument_map[sym] = {"security_id": sid, "company_name": name}

        synced, not_found = [], []
        for sym in missing_symbols:
            if sym in stock_universe_sid_map:
                sid = stock_universe_sid_map[sym]
                name = missing_names.get(sym, sym)
                cur.execute("""
                    UPDATE index_constituents
                    SET security_id = %s, updated_at = NOW()
                    WHERE stock_symbol = %s
                      AND security_id IS DISTINCT FROM %s
                """, (sid, sym, sid))
                synced.append({"symbol": sym, "security_id": sid, "name": name, "source": "stock_universe"})
            elif sym in instrument_map:
                d = instrument_map[sym]
                sid = d["security_id"]
                name = d["company_name"] or missing_names.get(sym, sym)
                cur.execute("SELECT 1 FROM stock_universe WHERE security_id = %s", (sid,))
                if not cur.fetchone():
                    cur.execute("""
                        INSERT INTO stock_universe (security_id, symbol, company_name, exchange, is_active)
                        VALUES (%s, %s, %s, 'NSE_EQ', true) ON CONFLICT (security_id) DO NOTHING
                    """, (sid, sym, name))
                cur.execute("""
                    UPDATE index_constituents
                    SET security_id = %s, updated_at = NOW()
                    WHERE stock_symbol = %s
                      AND security_id IS DISTINCT FROM %s
                """, (sid, sym, sid))
                synced.append({"symbol": sym, "security_id": sid, "name": name, "source": "instrument_csv"})
            else:
                not_found.append(sym)
        conn.commit()
        return jsonify({"message": f"Synced {len(synced)}, {len(not_found)} not found", "synced": synced, "not_found": not_found, "total_instruments": len(instrument_map)})
    except Exception as e:
        if conn:
            try: conn.rollback()
            except: pass
        print(f"[watchlist] stock lookup error: {e}")
        return jsonify({"error": "Failed to check watchlists"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# NEW WATCHLIST SYSTEM — Multiple named watchlists with 7 pin slots
# ═══════════════════════════════════════════════════════════════

@screener_bp.route("/api/watchlists", methods=["GET"])
def list_watchlists():
    """List all watchlists with item counts"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT w.*, COUNT(wi.id) as item_count
            FROM watchlists w
            LEFT JOIN watchlist_items wi ON w.id = wi.watchlist_id
            WHERE w.user_id = %s
            GROUP BY w.id
            ORDER BY w.pin_slot NULLS LAST, w.sort_order, w.created_at
        """, (g.user_id,))
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"watchlists": rows})
    except Exception as e:
        print(f"[watchlist] list error: {e}")
        return jsonify({"error": "Failed to load watchlists", "watchlists": []}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists", methods=["POST"])
@limiter.limit("10 per minute")
def create_watchlist():
    """Create a new watchlist"""
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    if len(name) > MAX_NAME_LENGTH:
        return jsonify({"error": f"Name too long (max {MAX_NAME_LENGTH} chars)"}), 400
    pin_slot = data.get("pin_slot")  # null = not pinned
    color = data.get("color", "#0A84FF")
    if len(color) > 7:
        color = "#0A84FF"
    conn = get_db()
    try:
        cur = conn.cursor()
        # Enforce watchlist count limit
        cur.execute("SELECT COUNT(*) as cnt FROM watchlists WHERE user_id = %s", (g.user_id,))
        if cur.fetchone()["cnt"] >= MAX_WATCHLISTS_PER_USER:
            return jsonify({"error": f"Maximum {MAX_WATCHLISTS_PER_USER} watchlists allowed"}), 400
        cur.execute(
            """INSERT INTO watchlists (name, pin_slot, color, user_id)
               VALUES (%s, %s, %s, %s) RETURNING id, name, pin_slot, color""",
            (name, pin_slot, color, g.user_id)
        )
        row = dict(cur.fetchone())
        conn.commit()
        return jsonify({"created": True, "watchlist": row})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] create error: {e}")
        return jsonify({"error": "Failed to create watchlist"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>", methods=["PUT"])
@limiter.limit("20 per minute")
def update_watchlist(wl_id):
    """Update watchlist name, color, pin_slot"""
    data = request.json or {}
    conn = get_db()
    try:
        cur = conn.cursor()
        sets, vals = [], []
        if "name" in data:
            name = str(data["name"]).strip()
            if len(name) > MAX_NAME_LENGTH:
                return jsonify({"error": f"Name too long (max {MAX_NAME_LENGTH} chars)"}), 400
            sets.append("name = %s"); vals.append(name)
        if "color" in data:
            color = str(data["color"])[:7]
            sets.append("color = %s"); vals.append(color)
        if "pin_slot" in data:
            # Unpin any existing list in this slot first
            if data["pin_slot"]:
                cur.execute("UPDATE watchlists SET pin_slot = NULL WHERE pin_slot = %s AND id != %s AND user_id = %s",
                            (data["pin_slot"], wl_id, g.user_id))
            sets.append("pin_slot = %s"); vals.append(data["pin_slot"])
        if not sets:
            return jsonify({"error": "Nothing to update"}), 400
        sets.append("updated_at = NOW()")
        vals.append(wl_id)
        vals.append(g.user_id)
        cur.execute(f"UPDATE watchlists SET {', '.join(sets)} WHERE id = %s AND user_id = %s RETURNING *", vals)
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Watchlist not found"}), 404
        return jsonify({"updated": True, "watchlist": dict(row)})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] update error: {e}")
        return jsonify({"error": "Failed to update watchlist"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>", methods=["DELETE"])
@limiter.limit("5 per minute")
def delete_watchlist(wl_id):
    """Delete a watchlist and all its items (CASCADE)"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlists WHERE id = %s AND user_id = %s RETURNING id, name", (wl_id, g.user_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"deleted": True, "id": wl_id, "name": row["name"]})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] delete error: {e}")
        return jsonify({"error": "Failed to delete watchlist"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>/items", methods=["GET"])
def get_watchlist_items(wl_id):
    """Get all items in a specific watchlist"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT wi.*, w.name as watchlist_name, w.color as watchlist_color
            FROM watchlist_items wi
            JOIN watchlists w ON w.id = wi.watchlist_id
            WHERE wi.watchlist_id = %s AND w.user_id = %s
            ORDER BY wi.added_at DESC
        """, (wl_id, g.user_id))
        items = [dict(r) for r in cur.fetchall()]
        return jsonify({"items": items, "total": len(items), "watchlist_id": wl_id})
    except Exception as e:
        print(f"[watchlist] get items error: {e}")
        return jsonify({"error": "Failed to load items", "items": []}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>/items/enriched", methods=["GET"])
def get_watchlist_items_enriched(wl_id):
    """Get watchlist items enriched with latest CMP, change%, liquidity, 52W from candles_daily."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM watchlists WHERE id = %s AND user_id = %s", (wl_id, g.user_id))
        if not cur.fetchone():
            return jsonify({"error": "Watchlist not found"}), 404

        # Reads price / 52W / liquidity straight from stock_daily_summary
        # (precomputed once daily — small table, indexed on security_id)
        # instead of running three separate scans of candles_daily (6.56M rows)
        # for prices / range52 / liq. That removes the main contributor to
        # the 1-3s hang on watchlist open. live_close is updated every
        # ~10s during market hours by the websocket worker, so the page
        # still reflects intraday moves.
        cur.execute("""
            WITH items AS (
                SELECT wi.id, wi.symbol, wi.company_name, wi.security_id, wi.notes, wi.added_at, wi.section_name, wi.sort_order, wi.flagged
                FROM watchlist_items wi
                JOIN watchlists w ON w.id = wi.watchlist_id
                WHERE wi.watchlist_id = %s AND w.user_id = %s
            ),
            -- Nearest upcoming results board meeting per watchlist entry.
            -- Same filter/ordering as /api/positions so the Next Results card
            -- and the watchlist row show the same date for the same stock.
            forthcoming AS (
                SELECT DISTINCT ON (fr.security_id)
                    fr.security_id,
                    fr.meeting_date AS next_result_date,
                    fr.purpose AS next_result_purpose
                FROM forthcoming_results fr
                WHERE fr.security_id IN (SELECT security_id FROM items WHERE security_id IS NOT NULL)
                  AND fr.meeting_date >= CURRENT_DATE
                  AND (fr.purpose ILIKE '%%result%%'
                       OR fr.raw_purpose ILIKE '%%financial result%%'
                       OR fr.raw_purpose ILIKE '%%audited%%'
                       OR fr.raw_purpose ILIKE '%%quarterly result%%')
                ORDER BY fr.security_id,
                         fr.meeting_date ASC,
                         (fr.purpose ILIKE '%%financial result%%') DESC,
                         (fr.purpose ILIKE '%%result%%') DESC
            ),
            -- Mirror of `forthcoming` for the just-passed announcement so the
            -- watchlist row can render a "RESULTS Xd ago" pill within the
            -- 10-day post-results window.
            recent AS (
                SELECT DISTINCT ON (fr.security_id)
                    fr.security_id,
                    fr.meeting_date AS recent_result_date,
                    fr.purpose AS recent_result_purpose
                FROM forthcoming_results fr
                WHERE fr.security_id IN (SELECT security_id FROM items WHERE security_id IS NOT NULL)
                  AND fr.meeting_date < CURRENT_DATE
                  AND fr.meeting_date >= CURRENT_DATE - INTERVAL '10 days'
                  AND (fr.purpose ILIKE '%%result%%'
                       OR fr.raw_purpose ILIKE '%%financial result%%'
                       OR fr.raw_purpose ILIKE '%%audited%%'
                       OR fr.raw_purpose ILIKE '%%quarterly result%%')
                ORDER BY fr.security_id,
                         fr.meeting_date DESC,
                         (fr.purpose ILIKE '%%financial result%%') DESC,
                         (fr.purpose ILIKE '%%result%%') DESC
            ),
            -- Fallback for stocks with no BSE forthcoming-intimation row.
            -- Uses the date our pipeline first ingested the latest quarterly
            -- row as a proxy for "results dropped". Coalesced into recent_*
            -- below; only wins when `recent` CTE has nothing for the security.
            fq_latest AS (
                SELECT security_id, MAX(period_end_date) AS q
                FROM financials_quarterly
                WHERE security_id IN (SELECT security_id FROM items WHERE security_id IS NOT NULL)
                GROUP BY security_id
            ),
            fq_recent AS (
                SELECT fq.security_id,
                       MIN(fq.created_at)::date AS reported_at,
                       MAX(fq.period_end_date)  AS period_end
                FROM financials_quarterly fq
                JOIN fq_latest l ON l.security_id = fq.security_id AND l.q = fq.period_end_date
                WHERE fq.created_at >= CURRENT_DATE - INTERVAL '10 days'
                GROUP BY fq.security_id
            ),
            -- Primary theme/wave per security. Picks is_primary=true first,
            -- then highest exposure_score as fallback.
            theme_primary AS (
                SELECT DISTINCT ON (st.security_id)
                    st.security_id,
                    t.slug          AS theme_slug,
                    t.name          AS theme_name,
                    w.slug          AS wave_slug,
                    w.name          AS wave_name,
                    w.accent_color  AS wave_accent,
                    w.sort_order    AS wave_sort
                FROM stock_themes_v2 st
                JOIN themes_v2 t ON t.slug = st.theme_slug
                JOIN waves_v2  w ON w.slug = t.wave_slug
                WHERE st.security_id IN (SELECT security_id FROM items WHERE security_id IS NOT NULL)
                ORDER BY st.security_id,
                         st.is_primary DESC NULLS LAST,
                         st.exposure_score DESC NULLS LAST
            ),
            ipo_fallback AS (
                SELECT
                    i.security_id,
                    latest.close AS ipo_close,
                    prev.close   AS ipo_prev_close,
                    h52.high_52w AS ipo_high_52w,
                    h52.low_52w  AS ipo_low_52w,
                    h52.liq_cr   AS ipo_liq_cr
                FROM items i
                LEFT JOIN LATERAL (
                    SELECT cd.close, cd.date
                    FROM candles_daily cd
                    WHERE cd.security_id = i.security_id AND cd.volume > 0
                    ORDER BY cd.date DESC LIMIT 1
                ) latest ON TRUE
                LEFT JOIN LATERAL (
                    SELECT cd.close
                    FROM candles_daily cd
                    WHERE cd.security_id = i.security_id
                      AND cd.volume > 0
                      AND cd.date < latest.date
                    ORDER BY cd.date DESC LIMIT 1
                ) prev ON TRUE
                LEFT JOIN LATERAL (
                    SELECT MAX(cd.high) AS high_52w,
                           MIN(cd.low)  AS low_52w,
                           ROUND((AVG(cd.close * cd.volume) / 10000000.0)::numeric, 2) AS liq_cr
                    FROM candles_daily cd
                    WHERE cd.security_id = i.security_id AND cd.volume > 0
                ) h52 ON TRUE
                WHERE i.security_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM stock_daily_summary sds2
                      WHERE sds2.security_id = i.security_id
                  )
            )
            SELECT i.*,
                COALESCE(sds.live_close, sds.prev_close, ip.ipo_close) AS cmp,
                COALESCE(sds.prev_close, ip.ipo_prev_close) AS prev_close,
                CASE WHEN COALESCE(sds.prev_close, ip.ipo_prev_close) > 0
                     THEN ROUND(((COALESCE(sds.live_close, sds.prev_close, ip.ipo_close)
                                  - COALESCE(sds.prev_close, ip.ipo_prev_close))
                                 / COALESCE(sds.prev_close, ip.ipo_prev_close) * 100)::numeric, 2)
                     END AS change_pct,
                COALESCE(sds.high_52w, ip.ipo_high_52w) AS high_52w,
                COALESCE(sds.low_52w, ip.ipo_low_52w)   AS low_52w,
                CASE WHEN COALESCE(sds.high_52w, ip.ipo_high_52w) > COALESCE(sds.low_52w, ip.ipo_low_52w)
                     THEN ROUND(((COALESCE(sds.live_close, sds.prev_close, ip.ipo_close)
                                  - COALESCE(sds.low_52w, ip.ipo_low_52w))
                                 / (COALESCE(sds.high_52w, ip.ipo_high_52w) - COALESCE(sds.low_52w, ip.ipo_low_52w))
                                 * 100)::numeric, 1)
                     END AS range_52w_pct,
                COALESCE(sds.liq_cr, ip.ipo_liq_cr) AS liq_cr,
                fo.market_cap_cr AS mcap_cr,
                COALESCE(su.valvo_sector, su.sector) as sector, su.industry,
                f.next_result_date, f.next_result_purpose,
                CASE WHEN f.next_result_date IS NOT NULL
                     THEN (f.next_result_date - CURRENT_DATE)::int
                     ELSE NULL END AS next_result_days_left,
                COALESCE(rp.recent_result_date, fr.reported_at) AS recent_result_date,
                COALESCE(rp.recent_result_purpose,
                         CASE WHEN fr.reported_at IS NOT NULL
                              THEN 'Quarterly Results (' || fr.period_end::text || ')'
                         END) AS recent_result_purpose,
                CASE WHEN COALESCE(rp.recent_result_date, fr.reported_at) IS NOT NULL
                     THEN (CURRENT_DATE - COALESCE(rp.recent_result_date, fr.reported_at))::int
                     ELSE NULL END AS recent_result_days_ago,
                tp.theme_slug, tp.theme_name,
                tp.wave_slug, tp.wave_name, tp.wave_accent, tp.wave_sort
            FROM items i
            LEFT JOIN stock_daily_summary sds ON i.security_id = sds.security_id
            LEFT JOIN ipo_fallback ip ON i.security_id = ip.security_id
            LEFT JOIN stock_universe su ON i.security_id = su.security_id
            LEFT JOIN fundamentals_overview fo ON i.security_id = fo.security_id
            LEFT JOIN forthcoming f ON i.security_id = f.security_id
            LEFT JOIN recent rp ON i.security_id = rp.security_id
            LEFT JOIN fq_recent fr ON i.security_id = fr.security_id
            LEFT JOIN theme_primary tp ON i.security_id = tp.security_id
            ORDER BY i.added_at DESC
        """, (wl_id, g.user_id))
        items = []
        for r in cur.fetchall():
            row = dict(r)
            if row.get("added_at"): row["added_at"] = str(row["added_at"])
            if row.get("last_date"): row["last_date"] = str(row["last_date"])
            if row.get("next_result_date") and hasattr(row["next_result_date"], "isoformat"):
                row["next_result_date"] = row["next_result_date"].isoformat()
            if row.get("recent_result_date") and hasattr(row["recent_result_date"], "isoformat"):
                row["recent_result_date"] = row["recent_result_date"].isoformat()
            # Trading-days countdown alongside the calendar-days field. The
            # Watchlist EarningsBadge prefers this so "results in 3d" means
            # 3 trading sessions, not 3 calendar days that happen to span
            # a weekend.
            row["next_result_trading_days_left"] = (
                trading_days_until(row["next_result_date"])
                if row.get("next_result_date") else None
            )
            for k in ["cmp", "prev_close", "change_pct", "high_52w", "low_52w", "range_52w_pct", "liq_cr", "mcap_cr"]:
                if row.get(k) is not None: row[k] = float(row[k])
            items.append(row)
        return jsonify({"items": items, "total": len(items), "watchlist_id": wl_id})
    except Exception as e:
        print(f"[watchlist] enriched error: {e}")
        return jsonify({"error": "Failed to load enriched items", "items": []}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>/items", methods=["POST"])
@limiter.limit("60 per minute")
def add_watchlist_item(wl_id):
    """Add a stock to a watchlist"""
    data = request.json or {}
    symbol = data.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    notes = data.get("notes") or ""
    if len(notes) > MAX_NOTES_LENGTH:
        return jsonify({"error": f"Notes too long (max {MAX_NOTES_LENGTH} chars)"}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM watchlists WHERE id = %s AND user_id = %s", (wl_id, g.user_id))
        if not cur.fetchone():
            return jsonify({"error": "Watchlist not found"}), 404
        # Enforce item count limit
        cur.execute("SELECT COUNT(*) as cnt FROM watchlist_items WHERE watchlist_id = %s", (wl_id,))
        if cur.fetchone()["cnt"] >= MAX_ITEMS_PER_WATCHLIST:
            return jsonify({"error": f"Maximum {MAX_ITEMS_PER_WATCHLIST} items per watchlist"}), 400
        cur.execute(
            """INSERT INTO watchlist_items (watchlist_id, symbol, company_name, security_id, notes, user_id)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (watchlist_id, symbol) DO NOTHING
               RETURNING id""",
            (wl_id, symbol, data.get("company_name", "")[:200], data.get("security_id", ""), notes, g.user_id)
        )
        row = cur.fetchone()
        conn.commit()
        if row:
            return jsonify({"added": True, "id": row["id"], "symbol": symbol, "watchlist_id": wl_id})
        return jsonify({"added": False, "message": "Already in this watchlist", "symbol": symbol})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] add item error: {e}")
        return jsonify({"error": "Failed to add item"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>/items/<symbol>", methods=["DELETE"])
@limiter.limit("30 per minute")
def remove_watchlist_item(wl_id, symbol):
    """Remove a stock from a watchlist"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""DELETE FROM watchlist_items WHERE watchlist_id = %s AND symbol = %s
                    AND watchlist_id IN (SELECT id FROM watchlists WHERE user_id = %s)
                    RETURNING id""",
                    (wl_id, symbol.upper(), g.user_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"removed": True, "symbol": symbol.upper(), "watchlist_id": wl_id})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] remove error: {e}")
        return jsonify({"error": "Failed to remove item"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>/items/<symbol>/notes", methods=["PUT"])
@limiter.limit("20 per minute")
def update_item_notes(wl_id, symbol):
    """Update notes for a stock in a watchlist"""
    data = request.json or {}
    notes = data.get("notes", "")
    if len(notes) > MAX_NOTES_LENGTH:
        return jsonify({"error": f"Notes too long (max {MAX_NOTES_LENGTH} chars)"}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE watchlist_items SET notes = %s WHERE watchlist_id = %s AND symbol = %s
               AND watchlist_id IN (SELECT id FROM watchlists WHERE user_id = %s)
               RETURNING id""",
            (notes, wl_id, symbol.upper(), g.user_id)
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"updated": True, "symbol": symbol.upper(), "notes": notes})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] notes error: {e}")
        return jsonify({"error": "Failed to update notes"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/pinned", methods=["GET"])
def get_pinned_watchlists():
    """Get all 7 pinned watchlists with their items — used by the pinned tabs UI"""
    conn = get_db()
    try:
        cur = conn.cursor()

        # Auto-create a default watchlist for users who have none
        cur.execute("SELECT COUNT(*) AS cnt FROM watchlists WHERE user_id = %s", (g.user_id,))
        if cur.fetchone()["cnt"] == 0:
            cur.execute("""
                INSERT INTO watchlists (user_id, name, pin_slot, color)
                VALUES (%s, 'My Watchlist', 1, '#0A84FF')
                RETURNING id
            """, (g.user_id,))
            wl_id = cur.fetchone()["id"]
            # Seed with popular Nifty 50 stocks so the page isn't empty
            seed_stocks = [
                ("RELIANCE", "Reliance Industries"),
                ("TCS", "Tata Consultancy Services"),
                ("HDFCBANK", "HDFC Bank"),
                ("INFY", "Infosys"),
                ("ICICIBANK", "ICICI Bank"),
            ]
            for i, (sym, name) in enumerate(seed_stocks):
                cur.execute(
                    "SELECT security_id FROM stock_universe WHERE symbol = %s LIMIT 1",
                    (sym,),
                )
                row = cur.fetchone()
                sec_id = row["security_id"] if row else None
                cur.execute("""
                    INSERT INTO watchlist_items (watchlist_id, symbol, company_name, security_id, sort_order, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (watchlist_id, symbol) DO NOTHING
                """, (wl_id, sym, name, sec_id, i, g.user_id))
            conn.commit()

        # Get all pinned watchlists
        cur.execute("""
            SELECT w.id, w.name, w.pin_slot, w.color
            FROM watchlists w
            WHERE w.pin_slot IS NOT NULL AND w.user_id = %s
            ORDER BY w.pin_slot
        """, (g.user_id,))
        watchlists = [dict(r) for r in cur.fetchall()]

        # Get ALL items for pinned watchlists in one query
        wl_ids = [w["id"] for w in watchlists]
        items_by_wl = {}
        if wl_ids:
            cur.execute("""
                SELECT wi.watchlist_id, wi.symbol, wi.company_name, wi.security_id, wi.notes, wi.added_at, wi.section_name, wi.sort_order
                FROM watchlist_items wi
                WHERE wi.watchlist_id = ANY(%s)
                ORDER BY wi.sort_order ASC, wi.added_at DESC
            """, (wl_ids,))
            for row in cur.fetchall():
                r = dict(row)
                wl_id = r.pop("watchlist_id")
                items_by_wl.setdefault(wl_id, []).append(r)

        # Attach items to each watchlist
        for w in watchlists:
            w["items"] = items_by_wl.get(w["id"], [])
            w["count"] = len(w["items"])

        return jsonify({"pinned": watchlists})
    except Exception as e:
        print(f"[watchlist] pinned error: {e}")
        return jsonify({"error": "Failed to load pinned watchlists", "pinned": []}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/quick-add", methods=["POST"])
@limiter.limit("60 per minute")
def quick_add_to_watchlist():
    """Add a stock to a watchlist by pin_slot (for quick-add dropdown)"""
    data = request.json or {}
    pin_slot = data.get("pin_slot")
    symbol = data.get("symbol", "").strip().upper()
    if not pin_slot or not symbol:
        return jsonify({"error": "pin_slot and symbol required"}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name FROM watchlists WHERE pin_slot = %s AND user_id = %s", (pin_slot, g.user_id))
        wl = cur.fetchone()
        if not wl:
            return jsonify({"error": f"No watchlist in pin slot {pin_slot}"}), 404
        # Enforce item count limit
        cur.execute("SELECT COUNT(*) as cnt FROM watchlist_items WHERE watchlist_id = %s", (wl["id"],))
        if cur.fetchone()["cnt"] >= MAX_ITEMS_PER_WATCHLIST:
            return jsonify({"error": f"Maximum {MAX_ITEMS_PER_WATCHLIST} items per watchlist"}), 400
        cur.execute(
            """INSERT INTO watchlist_items (watchlist_id, symbol, company_name, security_id, user_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (watchlist_id, symbol) DO NOTHING
               RETURNING id""",
            (wl["id"], symbol, data.get("company_name", ""), data.get("security_id", ""), g.user_id)
        )
        row = cur.fetchone()
        conn.commit()
        if row:
            return jsonify({"added": True, "symbol": symbol, "watchlist": wl["name"], "pin_slot": pin_slot})
        return jsonify({"added": False, "message": "Already in this watchlist"})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] quick-add error: {e}")
        return jsonify({"error": "Failed to add item"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>/items/<symbol>/flag", methods=["PUT"])
@limiter.limit("60 per minute")
def update_item_flag(wl_id, symbol):
    """Toggle the red flag on a watchlist item — used by the right-click
    context menu to mark a stock as priority for execution."""
    data = request.json or {}
    flagged = bool(data.get("flagged", False))
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE watchlist_items SET flagged = %s
            WHERE watchlist_id = %s AND symbol = %s
            AND watchlist_id IN (SELECT id FROM watchlists WHERE user_id = %s)
            RETURNING id
        """, (flagged, wl_id, symbol.upper(), g.user_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Item not found"}), 404
        return jsonify({"updated": True, "symbol": symbol.upper(), "flagged": flagged})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] flag error: {e}")
        return jsonify({"error": "Failed to update flag"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>/items/<symbol>/section", methods=["PUT"])
@limiter.limit("20 per minute")
def update_item_section(wl_id, symbol):
    """Move a watchlist item to a section (or clear section with null)."""
    data = request.json or {}
    section_name = data.get("section_name")
    if section_name and len(section_name) > MAX_SECTION_LENGTH:
        return jsonify({"error": f"Section name too long (max {MAX_SECTION_LENGTH} chars)"}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE watchlist_items SET section_name = %s
            WHERE watchlist_id = %s AND symbol = %s
            AND watchlist_id IN (SELECT id FROM watchlists WHERE user_id = %s)
            RETURNING id
        """, (section_name, wl_id, symbol.upper(), g.user_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Item not found"}), 404
        return jsonify({"updated": True, "symbol": symbol.upper(), "section_name": section_name})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] section error: {e}")
        return jsonify({"error": "Failed to update section"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/<int:wl_id>/reorder", methods=["PUT"])
@limiter.limit("10 per minute")
def reorder_watchlist(wl_id):
    """Reorder items in a watchlist. Body: { items: [{ symbol, sort_order, section_name }] }"""
    data = request.json or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "items required"}), 400
    if len(items) > MAX_REORDER_ITEMS:
        return jsonify({"error": f"Too many items (max {MAX_REORDER_ITEMS})"}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM watchlists WHERE id = %s AND user_id = %s", (wl_id, g.user_id))
        if not cur.fetchone():
            return jsonify({"error": "Watchlist not found"}), 404
        for item in items:
            cur.execute("""
                UPDATE watchlist_items SET sort_order = %s, section_name = %s
                WHERE watchlist_id = %s AND symbol = %s
                AND watchlist_id IN (SELECT id FROM watchlists WHERE user_id = %s)
            """, (item.get("sort_order", 0), item.get("section_name"), wl_id, item["symbol"], g.user_id))
        conn.commit()
        return jsonify({"reordered": True, "count": len(items)})
    except Exception as e:
        conn.rollback()
        print(f"[watchlist] reorder error: {e}")
        return jsonify({"error": "Failed to reorder items"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/watchlists/stock/<symbol>", methods=["GET"])
def get_stock_watchlists(symbol):
    """Get which watchlists contain a specific stock — used for ★ state"""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT w.id, w.name, w.pin_slot, w.color
            FROM watchlist_items wi
            JOIN watchlists w ON w.id = wi.watchlist_id
            WHERE wi.symbol = %s AND w.user_id = %s
            ORDER BY w.pin_slot NULLS LAST
        """, (symbol.upper(), g.user_id))
        watchlists = [dict(r) for r in cur.fetchall()]
        return jsonify({"symbol": symbol.upper(), "in_watchlists": watchlists})
    except Exception as e:
        print(f"[watchlist] stock lookup error: {e}")
        return jsonify({"error": "Failed to check watchlists"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════
# SECTORAL ANALYSIS — MA health dashboard from candles_indices
# ═══════════════════════════════════════════════════════════════

CURATED_INDICES = {
    # ── Broad Market (7) ──
    "NIFTY": {"group": "broad", "display": "Nifty 50"},
    "NIFTY 500": {"group": "broad", "display": "Nifty 500"},
    "NIFTY NEXT 50": {"group": "broad", "display": "Next 50"},
    "NIFTY MID100 FREE": {"group": "broad", "display": "Midcap 100"},
    "NIFTY MIDSMALLCAP 400": {"group": "broad", "display": "MidSmall 400"},
    "NIFTY SMALLCAP 100": {"group": "broad", "display": "Smallcap 100"},
    "NIFTY MICROCAP250": {"group": "broad", "display": "Microcap 250"},
    # ── Sectoral (30) ──
    "BANKNIFTY": {"group": "sectoral", "display": "Bank Nifty"},
    "FINNIFTY": {"group": "sectoral", "display": "Fin Nifty"},
    "NIFTY PSU BANK": {"group": "sectoral", "display": "PSU Bank"},
    "NIFTY PVT BANK": {"group": "sectoral", "display": "Pvt Bank"},
    "NIFTY METAL": {"group": "sectoral", "display": "Metal"},
    "NIFTY AUTO": {"group": "sectoral", "display": "Auto"},
    "NIFTYIT": {"group": "sectoral", "display": "IT"},
    "NIFTY PHARMA": {"group": "sectoral", "display": "Pharma"},
    "NIFTY REALTY": {"group": "sectoral", "display": "Realty"},
    "NIFTY ENERGY": {"group": "sectoral", "display": "Energy"},
    "NIFTY FMCG": {"group": "sectoral", "display": "FMCG"},
    "NIFTY MEDIA": {"group": "sectoral", "display": "Media"},
    "NIFTY COMMODITIES": {"group": "sectoral", "display": "Commodities"},
    "NIFTY HEALTHCARE": {"group": "sectoral", "display": "Healthcare"},
    "NIFTY OIL AND GAS": {"group": "sectoral", "display": "Oil & Gas"},
    "NIFTY CONSR DURBL": {"group": "sectoral", "display": "Consumer Durables"},
    "NIFTY CONSUMPTION": {"group": "sectoral", "display": "Consumption"},
    "NIFTY IND DEFENCE": {"group": "sectoral", "display": "Defence"},
    "NIFTYINFRA": {"group": "sectoral", "display": "Infra"},
    "NIFTYCPSE": {"group": "sectoral", "display": "CPSE"},
    "NIFTYPSE": {"group": "sectoral", "display": "PSE"},
    "NIFTY SERV SECTOR": {"group": "sectoral", "display": "Services"},
    "Nifty Capital Mkt": {"group": "sectoral", "display": "Capital Mkt"},
    "NIFTY INDIA MFG": {"group": "sectoral", "display": "Manufacturing"},
    "NIFTY MNC": {"group": "sectoral", "display": "MNC"},
    "NIFTY IND DIGITAL": {"group": "sectoral", "display": "India Digital"},
    "NIFTY EV": {"group": "sectoral", "display": "EV"},
    "Nifty Housing": {"group": "sectoral", "display": "Housing"},
    "Nifty Mobility": {"group": "sectoral", "display": "Mobility"},
    "Nifty Trans Logis": {"group": "sectoral", "display": "Transport & Logistics"},
}

_INDEX_SYMBOL_BY_UPPER = {symbol.upper(): symbol for symbol in CURATED_INDICES}
_INDEX_SYMBOL_ALIASES = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY MIDCAP 100": "NIFTY MID100 FREE",
    "NIFTY MIDCAP100": "NIFTY MID100 FREE",
    "NIFTY MICROCAP 250": "NIFTY MICROCAP250",
    "NIFTY IT": "NIFTYIT",
    "NIFTY INFRASTRUCTURE": "NIFTYINFRA",
    "NIFTY PRIVATE BANK": "NIFTY PVT BANK",
    "NIFTY CONSUMER DURABLES": "NIFTY CONSR DURBL",
    "NIFTY INDIA DEFENCE": "NIFTY IND DEFENCE",
    "NIFTY SERVICES SECTOR": "NIFTY SERV SECTOR",
    "NIFTY HEALTHCARE INDEX": "NIFTY HEALTHCARE",
    "NIFTY OIL & GAS": "NIFTY OIL AND GAS",
    "NIFTY CAPITAL MARKETS": "Nifty Capital Mkt",
    "NIFTY TRANSPORTATION & LOGISTICS": "Nifty Trans Logis",
    "NIFTY INDIA MANUFACTURING": "NIFTY INDIA MFG",
}

def _normalize_index_symbol(raw_symbol):
    symbol = " ".join(str(raw_symbol or "NIFTY SMALLCAP 100").strip().split())
    if not symbol:
        symbol = "NIFTY SMALLCAP 100"
    lookup = symbol.upper()
    return _INDEX_SYMBOL_ALIASES.get(lookup) or _INDEX_SYMBOL_BY_UPPER.get(lookup) or symbol

@screener_bp.route("/api/screener/sectoral", methods=["GET"])
def sectoral_analysis():
    """Compute MA health for 37 curated indices from candles_indices."""
    conn = get_db()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '15000'")  # 15s max
        symbols = list(CURATED_INDICES.keys())

        # Get last 200 closes per symbol in one efficient query
        # Date limit: 200 trading days ≈ ~300 calendar days (weekends + holidays)
        cur.execute("""
            WITH ranked AS (
                SELECT symbol, date, close,
                    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) as rn
                FROM candles_indices
                WHERE symbol = ANY(%s)
                  AND date >= CURRENT_DATE - 310
            )
            SELECT symbol,
                MAX(CASE WHEN rn = 1 THEN date END) as latest_date,
                MAX(CASE WHEN rn = 1 THEN close END) as cmp,
                -- 1-week ago close (5 trading days)
                MAX(CASE WHEN rn = 6 THEN close END) as close_1w,
                -- 1-month ago close (~22 trading days)
                MAX(CASE WHEN rn = 22 THEN close END) as close_1m,
                -- MAs
                AVG(CASE WHEN rn <= 5 THEN close END) as ma5,
                AVG(CASE WHEN rn <= 10 THEN close END) as ma10,
                AVG(CASE WHEN rn <= 20 THEN close END) as ma20,
                AVG(CASE WHEN rn <= 50 THEN close END) as ma50,
                AVG(CASE WHEN rn <= 200 THEN close END) as ma200,
                -- Yesterday's close + MAs (shifted by one trading day) for day-over-day delta
                MAX(CASE WHEN rn = 2 THEN close END) as prev_close,
                AVG(CASE WHEN rn BETWEEN 2 AND 6   THEN close END) as prev_ma5,
                AVG(CASE WHEN rn BETWEEN 2 AND 11  THEN close END) as prev_ma10,
                AVG(CASE WHEN rn BETWEEN 2 AND 21  THEN close END) as prev_ma20,
                AVG(CASE WHEN rn BETWEEN 2 AND 51  THEN close END) as prev_ma50,
                AVG(CASE WHEN rn BETWEEN 2 AND 201 THEN close END) as prev_ma200,
                COUNT(*) FILTER (WHERE rn <= 200) as candle_count
            FROM ranked
            WHERE rn <= 201
            GROUP BY symbol
        """, (symbols,))

        rows = cur.fetchall()
        results = []
        summary = {
            "above_200": 0, "above_50": 0, "above_20": 0, "above_5": 0,
            "above_200_prev": 0, "above_50_prev": 0, "above_20_prev": 0, "above_5_prev": 0,
            "total": 0,
        }

        for r in rows:
            sym = r["symbol"]
            if sym not in CURATED_INDICES:
                continue

            meta = CURATED_INDICES[sym]
            cmp = float(r["cmp"] or 0)
            ma5 = float(r["ma5"] or 0)
            ma10 = float(r["ma10"] or 0)
            ma20 = float(r["ma20"] or 0)
            ma50 = float(r["ma50"] or 0)
            ma200 = float(r["ma200"] or 0)
            close_1w = float(r["close_1w"] or cmp)
            close_1m = float(r["close_1m"] or cmp)

            above = {
                "ma5": cmp > ma5,
                "ma10": cmp > ma10,
                "ma20": cmp > ma20,
                "ma50": cmp > ma50,
                "ma200": cmp > ma200,
            }
            score = sum(above.values())

            prev_close = float(r["prev_close"] or 0)
            prev_above = None
            if prev_close > 0:
                prev_above = {
                    "ma5":   prev_close > float(r["prev_ma5"]   or 0),
                    "ma20":  prev_close > float(r["prev_ma20"]  or 0),
                    "ma50":  prev_close > float(r["prev_ma50"]  or 0),
                    "ma200": prev_close > float(r["prev_ma200"] or 0),
                }

            chg_1w = round((cmp - close_1w) / close_1w * 100, 2) if close_1w else 0
            chg_1m = round((cmp - close_1m) / close_1m * 100, 2) if close_1m else 0
            pct_200 = round((cmp - ma200) / ma200 * 100, 1) if ma200 else 0

            results.append({
                "symbol": sym,
                "display": meta["display"],
                "group": meta["group"],
                "cmp": round(cmp, 1),
                "latest_date": str(r["latest_date"]),
                "ma5": round(ma5, 1),
                "ma10": round(ma10, 1),
                "ma20": round(ma20, 1),
                "ma50": round(ma50, 1),
                "ma200": round(ma200, 1),
                "above": above,
                "score": score,
                "pct_from_200ma": pct_200,
                "chg_1w": chg_1w,
                "chg_1m": chg_1m,
                "candles": r["candle_count"],
            })

            summary["total"] += 1
            if above["ma200"]: summary["above_200"] += 1
            if above["ma50"]: summary["above_50"] += 1
            if above["ma20"]: summary["above_20"] += 1
            if above["ma5"]: summary["above_5"] += 1
            if prev_above:
                if prev_above["ma200"]: summary["above_200_prev"] += 1
                if prev_above["ma50"]:  summary["above_50_prev"]  += 1
                if prev_above["ma20"]:  summary["above_20_prev"]  += 1
                if prev_above["ma5"]:   summary["above_5_prev"]   += 1

        # Sort by score desc, then by pct_from_200ma desc
        results.sort(key=lambda x: (-x["score"], -x["pct_from_200ma"]))

        # Top 3 strongest and weakest
        strongest = [r["display"] for r in results[:3]]
        weakest = [r["display"] for r in results[-3:]]

        return jsonify({
            "indices": results,
            "summary": summary,
            "strongest": strongest,
            "weakest": weakest,
            "data_date": results[0]["latest_date"] if results else None,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/screener/sector-breadth", methods=["GET"])
def sector_breadth():
    """Sector breadth heatmap — % of constituent stocks above 5/20/50/200 MAs
    for each curated SECTORAL index. Aggregates index_constituents joined with
    the last 200 trading days of candles_daily for the resolved security IDs."""
    conn = get_db()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '20000'")  # 20s max
        sectoral_symbols = [s for s, m in CURATED_INDICES.items() if m.get("group") == "sectoral"]

        cur.execute("""
            WITH resolved AS (
                SELECT
                    ic.index_symbol,
                    COALESCE(
                        su_symbol.security_id,
                        su_isin.security_id,
                        NULLIF(BTRIM(ic.security_id), ''),
                        NULL
                    ) AS security_id
                FROM index_constituents ic
                LEFT JOIN LATERAL (
                    SELECT su.security_id FROM stock_universe su
                    WHERE su.symbol = ic.stock_symbol AND su.is_active = true
                    LIMIT 1
                ) su_symbol ON TRUE
                LEFT JOIN LATERAL (
                    SELECT su.security_id FROM stock_universe su
                    WHERE ic.isin IS NOT NULL AND BTRIM(ic.isin) <> ''
                      AND su.isin = ic.isin AND su.is_active = true
                    LIMIT 1
                ) su_isin ON TRUE
                WHERE ic.index_symbol = ANY(%s)
            ),
            sector_stocks AS (
                SELECT DISTINCT index_symbol, security_id
                FROM resolved
                WHERE security_id IS NOT NULL AND security_id <> 'UNMAPPED'
            ),
            ranked AS (
                SELECT cd.security_id, cd.close, cd.date,
                       ROW_NUMBER() OVER (PARTITION BY cd.security_id ORDER BY cd.date DESC) AS rn
                FROM candles_daily cd
                INNER JOIN (SELECT DISTINCT security_id FROM sector_stocks) s
                    ON cd.security_id = s.security_id
                WHERE cd.date >= CURRENT_DATE - 310
            ),
            stock_mas AS (
                SELECT security_id,
                    MAX(CASE WHEN rn = 1 THEN close END) AS cmp,
                    MAX(CASE WHEN rn = 2 THEN close END) AS prev_close,
                    MAX(CASE WHEN rn = 1 THEN date END) AS latest_date,
                    AVG(CASE WHEN rn <= 5 THEN close END) AS ma5,
                    AVG(CASE WHEN rn <= 20 THEN close END) AS ma20,
                    AVG(CASE WHEN rn <= 50 THEN close END) AS ma50,
                    AVG(CASE WHEN rn <= 200 THEN close END) AS ma200,
                    COUNT(*) FILTER (WHERE rn <= 200) AS candle_count
                FROM ranked
                WHERE rn <= 200
                GROUP BY security_id
            )
            SELECT
                ss.index_symbol,
                COUNT(*) AS mapped,
                COUNT(*) FILTER (WHERE sm.cmp IS NOT NULL) AS with_price,
                COUNT(*) FILTER (WHERE sm.cmp IS NOT NULL AND sm.ma5  IS NOT NULL AND sm.cmp > sm.ma5)  AS above_5,
                COUNT(*) FILTER (WHERE sm.cmp IS NOT NULL AND sm.ma20 IS NOT NULL AND sm.cmp > sm.ma20) AS above_20,
                COUNT(*) FILTER (WHERE sm.cmp IS NOT NULL AND sm.ma50 IS NOT NULL AND sm.cmp > sm.ma50) AS above_50,
                COUNT(*) FILTER (WHERE sm.cmp IS NOT NULL AND sm.ma200 IS NOT NULL AND sm.cmp > sm.ma200) AS above_200,
                COUNT(*) FILTER (WHERE sm.cmp IS NOT NULL AND sm.prev_close IS NOT NULL AND sm.cmp > sm.prev_close) AS advances,
                COUNT(*) FILTER (WHERE sm.cmp IS NOT NULL AND sm.prev_close IS NOT NULL AND sm.cmp < sm.prev_close) AS declines,
                COUNT(*) FILTER (WHERE sm.ma5  IS NOT NULL) AS sample_5,
                COUNT(*) FILTER (WHERE sm.ma20 IS NOT NULL) AS sample_20,
                COUNT(*) FILTER (WHERE sm.ma50 IS NOT NULL) AS sample_50,
                COUNT(*) FILTER (WHERE sm.ma200 IS NOT NULL) AS sample_200,
                MAX(sm.latest_date) AS latest_date
            FROM sector_stocks ss
            LEFT JOIN stock_mas sm ON ss.security_id = sm.security_id
            GROUP BY ss.index_symbol
        """, (sectoral_symbols,))

        rows = cur.fetchall()
        by_sym = {r["index_symbol"]: r for r in rows}

        def _pct(num, denom):
            if not denom:
                return None
            return round((num or 0) / denom * 100, 1)

        sectors = []
        latest_overall = None
        for sym in sectoral_symbols:
            r = by_sym.get(sym)
            meta = CURATED_INDICES[sym]
            if not r or not r["mapped"]:
                sectors.append({
                    "symbol": sym,
                    "display": meta["display"],
                    "total": 0,
                    "above_5": 0, "above_20": 0, "above_50": 0, "above_200": 0,
                    "above_5_pct": None, "above_20_pct": None,
                    "above_50_pct": None, "above_200_pct": None,
                    "advances": 0, "declines": 0, "adv_pct": None,
                })
                continue
            mapped = int(r["mapped"] or 0)
            ld = r.get("latest_date")
            if ld and (latest_overall is None or str(ld) > str(latest_overall)):
                latest_overall = str(ld)
            adv = int(r["advances"] or 0)
            dec = int(r["declines"] or 0)
            ad_total = adv + dec
            sectors.append({
                "symbol": sym,
                "display": meta["display"],
                "total": mapped,
                "above_5":   int(r["above_5"] or 0),
                "above_20":  int(r["above_20"] or 0),
                "above_50":  int(r["above_50"] or 0),
                "above_200": int(r["above_200"] or 0),
                "above_5_pct":   _pct(r["above_5"],   r["sample_5"]),
                "above_20_pct":  _pct(r["above_20"],  r["sample_20"]),
                "above_50_pct":  _pct(r["above_50"],  r["sample_50"]),
                "above_200_pct": _pct(r["above_200"], r["sample_200"]),
                "advances": adv,
                "declines": dec,
                "adv_pct": round(adv / ad_total * 100, 1) if ad_total > 0 else None,
            })

        return jsonify({
            "sectors": sectors,
            "as_of": latest_overall,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[screener] sector_breadth error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ============================================================
# INDEX TICKER — Lightweight live bar for Dashboard/Scoring/Screener
# ============================================================

@screener_bp.route("/api/screener/index-ticker", methods=["GET"])
def index_ticker():
    """
    Returns CMP, daily % change, and last 20 closes for sparkline
    for a given index symbol (default: NIFTY SMALLCAP 100).
    Designed to be polled every 10s from the frontend.
    """
    symbol = _normalize_index_symbol(request.args.get("symbol", "NIFTY SMALLCAP 100"))
    conn = get_db()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 500
    try:
        cur = conn.cursor()

        # Get last 22 closes (today + yesterday for chg_1d + 20 for sparkline)
        cur.execute("""
            SELECT date, close
            FROM candles_indices
            WHERE symbol = %s
            ORDER BY date DESC
            LIMIT 22
        """, (symbol,))
        rows = cur.fetchall()

        if not rows or len(rows) < 2:
            return jsonify({"error": f"No data for {symbol}"}), 404

        cmp = float(rows[0]["close"])
        prev_close = float(rows[1]["close"])
        chg_1d = round((cmp - prev_close) / prev_close * 100, 2) if prev_close else 0
        latest_date = str(rows[0]["date"])

        # Last 20 closes in chronological order for sparkline
        sparkline = [float(r["close"]) for r in reversed(rows[:20])]

        meta = CURATED_INDICES.get(symbol, {})
        display = meta.get("display", symbol)

        return jsonify({
            "symbol": symbol,
            "display": display,
            "cmp": round(cmp, 1),
            "prev_close": round(prev_close, 1),
            "chg_1d": chg_1d,
            "latest_date": latest_date,
            "sparkline": sparkline,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ============================================================
# INDEX CHART DATA — For Sectoral Analysis chart overlay
# ============================================================

@screener_bp.route("/api/screener/index-chart/<symbol>", methods=["GET"])
def get_index_chart(symbol):
    """
    Fetch OHLCV candle data for an index from candles_indices table.
    Query params:
      - days: number of trading days to fetch (default 365)
    Returns array of {time, open, high, low, close, volume}
    """
    symbol = _normalize_index_symbol(symbol)
    days = request.args.get("days", 365, type=int)
    days = min(days, 5000)  # cap at 5000

    conn = get_db()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT date, open, high, low, close, volume
            FROM candles_indices
            WHERE symbol = %s
            ORDER BY date DESC
            LIMIT %s
        """, (symbol, days))

        rows = cur.fetchall()
        if not rows:
            return jsonify({"error": "No data for symbol: " + symbol, "candles": []}), 404

        # Return in chronological order (oldest first) for chart rendering
        candles = []
        for r in reversed(rows):
            candles.append({
                "time": r["date"].strftime("%Y-%m-%d") if hasattr(r["date"], "strftime") else str(r["date"]),
                "open": round(float(r["open"] or 0), 2),
                "high": round(float(r["high"] or 0), 2),
                "low": round(float(r["low"] or 0), 2),
                "close": round(float(r["close"] or 0), 2),
                "volume": int(r["volume"] or 0),
            })

        return jsonify({
            "symbol": symbol,
            "candles": candles,
            "count": len(candles),
        })
    except Exception as e:
        print(f"Index chart fetch failed for {symbol}: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ============================================================
# INDEX CONSTITUENTS — Fetch from NSE & store in Supabase
# ============================================================

# NSE API index name mapping → our internal symbol
_NSE_INDEX_MAP = {
    "NIFTY 50": "NIFTY",
    "NIFTY 500": "NIFTY 500",
    "NIFTY NEXT 50": "NIFTY NEXT 50",
    "NIFTY MIDCAP 100": "NIFTY MID100 FREE",
    "NIFTY MIDSMALLCAP 400": "NIFTY MIDSMALLCAP 400",
    "NIFTY SMALLCAP 100": "NIFTY SMALLCAP 100",
    "NIFTY MICROCAP 250": "NIFTY MICROCAP250",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "NIFTY PSU BANK": "NIFTY PSU BANK",
    "NIFTY PRIVATE BANK": "NIFTY PVT BANK",
    "NIFTY METAL": "NIFTY METAL",
    "NIFTY AUTO": "NIFTY AUTO",
    "NIFTY IT": "NIFTYIT",
    "NIFTY PHARMA": "NIFTY PHARMA",
    "NIFTY REALTY": "NIFTY REALTY",
    "NIFTY ENERGY": "NIFTY ENERGY",
    "NIFTY FMCG": "NIFTY FMCG",
    "NIFTY MEDIA": "NIFTY MEDIA",
    "NIFTY COMMODITIES": "NIFTY COMMODITIES",
    "NIFTY HEALTHCARE INDEX": "NIFTY HEALTHCARE",
    "NIFTY OIL & GAS": "NIFTY OIL AND GAS",
    "NIFTY CONSUMER DURABLES": "NIFTY CONSR DURBL",
    "NIFTY CONSUMPTION": "NIFTY CONSUMPTION",
    "NIFTY INDIA DEFENCE": "NIFTY IND DEFENCE",
    "NIFTY INFRASTRUCTURE": "NIFTYINFRA",
    "NIFTY CPSE": "NIFTYCPSE",
    "NIFTY PSE": "NIFTYPSE",
    "NIFTY SERVICES SECTOR": "NIFTY SERV SECTOR",
    "NIFTY MNC": "NIFTY MNC",
    "NIFTY INDIA DIGITAL": "NIFTY IND DIGITAL",
    "NIFTY EV & NEW AGE AUTOMOTIVE": "NIFTY EV",
    "NIFTY HOUSING": "Nifty Housing",
    "NIFTY MOBILITY": "Nifty Mobility",
    "NIFTY TRANSPORTATION & LOGISTICS": "Nifty Trans Logis",
    "NIFTY INDIA MANUFACTURING": "NIFTY INDIA MFG",
    "NIFTY CAPITAL MARKETS": "Nifty Capital Mkt",
}


def _fetch_nse_index(session, index_name):
    """Fetch constituents for a single index from NSE API."""
    import time as _t
    import urllib.parse

    url = f"https://www.nseindia.com/api/equity-stockIndices?index={urllib.parse.quote(index_name)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/market-data/live-equity-market",
    }

    try:
        r = session.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"  ❌ {index_name}: HTTP {r.status_code}")
            return []

        data = r.json()
        stocks = []
        items = data.get("data", [])

        # First item is the index summary, skip it
        total_ffmc = sum(float(it.get("ffmc", 0) or 0) for it in items if it.get("meta"))
        
        for it in items:
            meta = it.get("meta")
            if not meta:
                continue

            sym = meta.get("symbol", it.get("symbol", ""))
            name = meta.get("companyName", "")
            industry = meta.get("industry", "")
            ffmc = float(it.get("ffmc", 0) or 0)
            weight = round((ffmc / total_ffmc * 100), 2) if total_ffmc > 0 and ffmc > 0 else None

            stocks.append({
                "symbol": sym,
                "name": name,
                "industry": industry,
                "weight": weight,
                "isin": meta.get("isin", ""),
            })

        print(f"  ✅ {index_name}: {len(stocks)} stocks")
        return stocks

    except Exception as e:
        print(f"  ❌ {index_name}: {e}")
        return []


@screener_bp.route("/api/screener/refresh-constituents", methods=["POST"])
def refresh_constituents():
    """
    Fetch constituent stocks for all curated indices from NSE API.
    Stores in index_constituents table with upsert.
    Call this once after deploy, then monthly for rebalancing.
    Takes ~60-90 seconds (37 indices, 1.5s delay between each).
    """
    import time as _t

    conn = get_db()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 500

    try:
        cur = conn.cursor()

        # Step 1: Initialize NSE session (get cookies)
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })

        print("🔄 Initializing NSE session...")
        try:
            session.get("https://www.nseindia.com", timeout=10)
        except Exception:
            pass  # Session cookies set even on timeout
        _t.sleep(1)

        # Step 2: Fetch each index
        results = {}
        total_inserted = 0
        errors = []

        # Get display names from CURATED_INDICES
        display_map = {v: k for k, v in _NSE_INDEX_MAP.items()}

        for nse_name, our_symbol in _NSE_INDEX_MAP.items():
            display = CURATED_INDICES.get(our_symbol, {}).get("display", nse_name)
            print(f"📊 Fetching: {nse_name} → {our_symbol}")

            stocks = _fetch_nse_index(session, nse_name)
            _t.sleep(1.5)  # Rate limit — NSE blocks fast requests

            if not stocks:
                errors.append(nse_name)
                continue

            # Step 3: Match security_id from stock_universe
            symbols = [s["symbol"] for s in stocks]
            sec_id_map = {}
            try:
                cur.execute(
                    "SELECT symbol, security_id FROM stock_universe WHERE symbol = ANY(%s)",
                    (symbols,)
                )
                for row in cur.fetchall():
                    sec_id_map[row["symbol"]] = row["security_id"]
            except Exception:
                pass

            # Step 4: Upsert into index_constituents
            count = 0
            for s in stocks:
                try:
                    cur.execute("""
                        INSERT INTO index_constituents 
                            (index_symbol, index_display, stock_symbol, stock_name, sector, weightage, isin, security_id, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (index_symbol, stock_symbol)
                        DO UPDATE SET 
                            stock_name = EXCLUDED.stock_name,
                            sector = EXCLUDED.sector,
                            weightage = EXCLUDED.weightage,
                            isin = EXCLUDED.isin,
                            security_id = EXCLUDED.security_id,
                            index_display = EXCLUDED.index_display,
                            updated_at = NOW()
                    """, (
                        our_symbol, display,
                        s["symbol"], s["name"], s["industry"],
                        s["weight"], s.get("isin", ""),
                        sec_id_map.get(s["symbol"]),
                    ))
                    count += 1
                except Exception as e:
                    print(f"    Insert error for {s['symbol']}: {e}")

            conn.commit()
            total_inserted += count
            results[our_symbol] = count

        return jsonify({
            "success": True,
            "total_inserted": total_inserted,
            "indices_fetched": len(results),
            "indices_failed": len(errors),
            "errors": errors,
            "details": results,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ============================================================
# INDEX CONSTITUENTS — Fetch stocks for a given index
# ============================================================

@screener_bp.route("/api/screener/index-constituents/<index_symbol>", methods=["GET"])
def get_index_constituents(index_symbol):
    """
    Returns constituent stocks for an index with latest price data.
    Resolves constituent security IDs from stock_universe first so
    placeholder IDs in index_constituents do not hide valid stocks.
    """
    index_symbol = _normalize_index_symbol(index_symbol)
    conn = get_db()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 500
    try:
        cur = conn.cursor()

        # Get constituents with latest price data + weekly change + MA trend
        # Optimized: target_sids CTE limits all queries to only this index's stocks
        cur.execute("""
            WITH resolved_constituents AS (
                SELECT
                    ic.stock_symbol,
                    ic.stock_name,
                    ic.sector,
                    ic.weightage,
                    ic.isin,
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
                    cd.security_id, cd.close as week_ago_close
                FROM candles_daily cd
                INNER JOIN target_sids t ON cd.security_id = t.security_id
                WHERE cd.date >= CURRENT_DATE - 14
                  AND cd.date <= CURRENT_DATE - 4
                ORDER BY cd.security_id, cd.date DESC
            ),
            ma_data AS (
                SELECT security_id,
                    AVG(close) FILTER (WHERE rn <= 5) as ma5,
                    AVG(close) FILTER (WHERE rn <= 10) as ma10,
                    AVG(close) FILTER (WHERE rn <= 20) as ma20,
                    AVG(close) FILTER (WHERE rn <= 50) as ma50
                FROM (
                    SELECT cd.security_id, cd.close,
                        ROW_NUMBER() OVER (PARTITION BY cd.security_id ORDER BY cd.date DESC) as rn
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
                rc.isin,
                rc.resolved_security_id AS security_id,
                lp.close as cmp,
                lp.day_change,
                lp.date as price_date,
                ROUND(((lp.close - wa.week_ago_close) / NULLIF(wa.week_ago_close, 0) * 100)::numeric, 2) as week_change,
                md.ma5, md.ma10, md.ma20, md.ma50,
                CASE WHEN lp.close > md.ma5 AND lp.close > md.ma10 AND lp.close > md.ma20 THEN 'uptrend'
                     WHEN lp.close < md.ma5 AND lp.close < md.ma10 AND lp.close < md.ma20 THEN 'downtrend'
                     ELSE 'mixed' END as ma_trend
            FROM resolved_constituents rc
            LEFT JOIN latest_prices lp ON rc.resolved_security_id = lp.security_id
            LEFT JOIN week_ago wa ON rc.resolved_security_id = wa.security_id
            LEFT JOIN ma_data md ON rc.resolved_security_id = md.security_id
            WHERE rc.resolved_security_id IS NOT NULL
              AND rc.resolved_security_id <> 'UNMAPPED'
            ORDER BY rc.weightage DESC NULLS LAST, rc.stock_symbol
        """, (index_symbol,))

        rows = cur.fetchall()
        stocks = []
        for r in rows:
            cmp = float(r["cmp"]) if r["cmp"] else None
            ma5 = float(r["ma5"]) if r.get("ma5") else None
            ma10 = float(r["ma10"]) if r.get("ma10") else None
            ma20 = float(r["ma20"]) if r.get("ma20") else None
            ma50 = float(r["ma50"]) if r.get("ma50") else None
            # Count how many MAs the price is above
            ma_above = sum(1 for ma in [ma5, ma10, ma20, ma50] if ma and cmp and cmp > ma)
            day_chg = float(r["day_change"]) if r["day_change"] else None
            stocks.append({
                "symbol": r["stock_symbol"],
                "name": r["stock_name"],
                "sector": r["sector"],
                "weightage": float(r["weightage"]) if r["weightage"] else None,
                "security_id": r["security_id"],
                "cmp": cmp,
                "change": day_chg,
                "pchange": day_chg,
                "change_pct": day_chg,
                "week_change": float(r["week_change"]) if r.get("week_change") else None,
                "ma_trend": r.get("ma_trend", "mixed"),
                "ma_above": ma_above,
                "isin": r["isin"],
            })

        # Get index display name
        display = CURATED_INDICES.get(index_symbol, {}).get("display", index_symbol)

        return jsonify({
            "index_symbol": index_symbol,
            "index_display": display,
            "stocks": stocks,
            "count": len(stocks),
            "has_charts": len([s for s in stocks if s["security_id"]]),
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[watchlist] stock lookup error: {e}")
        return jsonify({"error": "Failed to check watchlists"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# SCANNER DAILY LOG — track stock counts over time
# ═══════════════════════════════════════════════════

def _log_scan_count(stock_count, gainers, losers, avg_change):
    """Log today's scan count to scanner_daily_log (upsert — one row per date)"""
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        today = datetime.now(IST).strftime("%Y-%m-%d")
        cur.execute("""
            INSERT INTO scanner_daily_log (scan_date, stock_count, gainers, losers, avg_change)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (scan_date) DO UPDATE SET
                stock_count = EXCLUDED.stock_count,
                gainers = EXCLUDED.gainers,
                losers = EXCLUDED.losers,
                avg_change = EXCLUDED.avg_change,
                updated_at = NOW()
        """, (today, stock_count, gainers, losers, round(avg_change, 2)))
        conn.commit()
    except Exception as e:
        print(f"⚠ Scanner log failed: {e}")
    finally:
        close_db(conn)


@screener_bp.route("/api/screener/history", methods=["GET"])
def scanner_history():
    """Return last N days of scanner counts for the sparkline chart"""
    days = min(int(request.args.get("days", 60)), 180)
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT scan_date, stock_count, gainers, losers, avg_change
            FROM scanner_daily_log
            ORDER BY scan_date DESC
            LIMIT %s
        """, (days,))
        rows = cur.fetchall()
        return jsonify({
            "history": [
                {
                    "date": str(r["scan_date"]),
                    "count": r["stock_count"],
                    "gainers": r["gainers"],
                    "losers": r["losers"],
                    "avg_change": float(r["avg_change"]) if r["avg_change"] else 0,
                }
                for r in reversed(rows)
            ]
        })
    except Exception as e:
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error", "history": []}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════
# TOP MOVERS — Week / Month / Quarter gainers & losers
# Cached for 60s — data only changes every ~30s during market hours
# ═══════════════════════════════════════════════════
_movers_cache = {}  # key: "period_direction_limit" → { "data": ..., "ts": ... }

@screener_bp.route("/api/screener/top-movers", methods=["GET"])
def top_movers():
    """Return top gainers & losers for week (5d), month (20d), quarter (60d).
    v2: Uses pre-computed summary + live candle for instant results."""
    period = request.args.get("period", "month")
    direction = request.args.get("direction", "gainers")
    limit = min(int(request.args.get("limit", 50)), 100)
    min_price = float(request.args.get("min_price", 30))
    min_vol = float(request.args.get("min_volume", 50000))
    min_change = float(request.args.get("min_change", 0))

    period_map = {"week": 5, "month": 20, "quarter": 60, "half": 120, "year": 252}
    days = period_map.get(period, 20)

    # Map period to pre-computed column name
    period_col_map = {"week": "close_5d", "month": "close_20d", "quarter": "close_60d",
                      "half": "close_120d", "year": "close_252d"}
    past_col = period_col_map.get(period, "close_20d")

    # Cache hit — return immediately (1h TTL: period gain doesn't move much
    # intra-hour, and the user explicitly asked for hourly cadence so we don't
    # churn the % display every minute). Cache key bumped to v2 since the
    # response shape now includes sector + prev_close.
    cache_key = f"v2_{period}_{direction}_{limit}_{min_change}"
    cached = _movers_cache.get(cache_key)
    if cached and (datetime.now(IST) - cached["ts"]).total_seconds() < 3600:
        return jsonify(cached["data"])

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")  # 5s max (was 15s)

        # If summary is not fresh, trigger background refresh (non-blocking)
        if not _is_summary_fresh():
            _trigger_background_refresh()

        # Fast path: summary only — uses trigger-synced live_close for current price
        cur.execute(f"""
            SELECT s.symbol, s.company_name, s.security_id,
                COALESCE(s.live_close, s.prev_close) as close,
                s.prev_close as prev_close,
                s.{past_col} as past_close,
                ROUND(((COALESCE(s.live_close, s.prev_close) - s.{past_col}) / NULLIF(s.{past_col}, 0) * 100)::numeric, 2) as change_pct,
                ROUND((s.vol_29d_sum / GREATEST(s.vol_29d_count, 1))::numeric) as avg_volume,
                ROUND((s.turnover_29d_sum / GREATEST(s.turnover_29d_count, 1) / 10000000.0)::numeric, 1) as liq_cr,
                COALESCE(u.valvo_sector, u.sector) as sector
            FROM stock_daily_summary s
            LEFT JOIN stock_universe u ON s.security_id = u.security_id
            WHERE s.is_etf = false
              AND s.{past_col} > 0
              AND COALESCE(s.live_close, s.prev_close) >= %s
              AND s.vol_29d_sum / GREATEST(s.vol_29d_count, 1) >= %s
              AND ABS((COALESCE(s.live_close, s.prev_close) - s.{past_col}) / NULLIF(s.{past_col}, 0) * 100) >= %s
            ORDER BY change_pct {"DESC" if direction == "gainers" else "ASC"}
            LIMIT %s
        """, (min_price, min_vol, min_change, limit))
        rows = [dict(r) for r in cur.fetchall()]

        result = {
            "period": period, "days": days, "direction": direction,
            "count": len(rows),
            "stocks": [{
                "symbol": r["symbol"], "name": r["company_name"],
                "security_id": r["security_id"],
                "close": float(r["close"]),
                "prev_close": float(r["prev_close"] or 0),
                "past_close": float(r["past_close"]),
                "change_pct": float(r["change_pct"]),
                "avg_volume": float(r["avg_volume"] or 0),
                "liq_cr": float(r["liq_cr"] or 0),
                "sector": r["sector"] or "Uncategorized",
            } for r in rows]
        }
        _movers_cache[cache_key] = {"data": result, "ts": datetime.now(IST)}
        return jsonify(result)
    except Exception as e:
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error", "stocks": []}), 500
    finally:
        close_db(conn)


def _top_movers_legacy(cur, conn, period, direction, days, limit, min_price, min_vol, min_change):
    """Legacy 3-CTE fallback for top-movers when summary is stale."""
    cur.execute("SET LOCAL statement_timeout = '15000'")
    past_window_end = days
    past_window_start = days + 15
    cur.execute(f"""
        WITH latest AS (
            SELECT DISTINCT ON (security_id) security_id, close as latest_close
            FROM candles_daily WHERE date >= CURRENT_DATE - 10
            ORDER BY security_id, date DESC
        ),
        prev AS (
            SELECT DISTINCT ON (security_id) security_id, close as prev_close
            FROM candles_daily
            WHERE date <= CURRENT_DATE - 1
              AND date >= CURRENT_DATE - 7
            ORDER BY security_id, date DESC
        ),
        past AS (
            SELECT DISTINCT ON (security_id) security_id, close as past_close
            FROM candles_daily
            WHERE date <= CURRENT_DATE - INTERVAL '{past_window_end} days'
              AND date >= CURRENT_DATE - INTERVAL '{past_window_start} days'
            ORDER BY security_id, date DESC
        ),
        vol AS (
            SELECT security_id, AVG(volume) as avg_vol,
                ROUND((AVG(volume * close) / 10000000)::numeric, 1) as liq_cr
            FROM candles_daily WHERE date >= CURRENT_DATE - 30 AND volume > 0
            GROUP BY security_id
        )
        SELECT u.symbol, u.company_name, u.security_id,
            l.latest_close as close,
            COALESCE(pr.prev_close, p.past_close) as prev_close,
            p.past_close,
            ROUND(((l.latest_close - p.past_close) / NULLIF(p.past_close, 0) * 100)::numeric, 2) as change_pct,
            ROUND(v.avg_vol::numeric) as avg_volume, v.liq_cr,
            COALESCE(u.valvo_sector, u.sector) as sector
        FROM latest l
        JOIN past p ON l.security_id = p.security_id
        LEFT JOIN prev pr ON l.security_id = pr.security_id
        JOIN vol v ON l.security_id = v.security_id
        JOIN stock_universe u ON l.security_id = u.security_id
        WHERE COALESCE(u.is_etf, false) = false
          AND p.past_close > 0 AND l.latest_close >= %s AND v.avg_vol >= %s
          AND ABS((l.latest_close - p.past_close) / NULLIF(p.past_close, 0) * 100) >= %s
        ORDER BY change_pct {"DESC" if direction == "gainers" else "ASC"}
        LIMIT %s
    """, (min_price, min_vol, min_change, limit))
    rows = [dict(r) for r in cur.fetchall()]
    result = {
        "period": period, "days": days, "direction": direction,
        "count": len(rows),
        "stocks": [{
            "symbol": r["symbol"], "name": r["company_name"],
            "security_id": r["security_id"],
            "close": float(r["close"]),
            "prev_close": float(r["prev_close"] or 0),
            "past_close": float(r["past_close"]),
            "change_pct": float(r["change_pct"]),
            "avg_volume": float(r["avg_volume"] or 0),
            "liq_cr": float(r["liq_cr"] or 0),
            "sector": r["sector"] or "Uncategorized",
        } for r in rows]
    }
    _movers_cache[f"v2_{period}_{direction}_{limit}_{min_change}"] = {"data": result, "ts": datetime.now(IST)}
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════
#   CUSTOM SCANNER — On-demand computation across all stocks
#   /api/scanner/custom
# ═══════════════════════════════════════════════════════════════

@screener_bp.route("/api/scanner/custom", methods=["POST"])
def custom_scanner():
    """
    Scans all active stocks with user-defined filters.
    Computes: 52W high/low proximity, MAs, liquidity, relative strength.
    Takes ~3-5 seconds due to on-demand computation across 2400+ stocks.
    """
    filters = request.json or {}
    conn = get_db()
    try:
        cur = conn.cursor()

        # ── Step 1: Compute all metrics using TRADING DAYS (not calendar days) ──
        # Array approach: ARRAY_AGG sorted by date DESC, then slice [1:N] for last N trading days
        # This is both more accurate AND faster than calendar-day subtraction (~1.7s vs ~3s)
        cur.execute("""
            WITH arrayed AS (
                SELECT
                    cd.security_id,
                    ARRAY_AGG(cd.close ORDER BY cd.date DESC) as cl,
                    ARRAY_AGG(cd.high ORDER BY cd.date DESC) as hi,
                    ARRAY_AGG(cd.low ORDER BY cd.date DESC) as lo,
                    ARRAY_AGG(cd.volume * cd.close ORDER BY cd.date DESC) as turnover,
                    ARRAY_AGG(ABS(cd.high - cd.low) / NULLIF(cd.close, 0) * 100 ORDER BY cd.date DESC) as ranges,
                    MAX(cd.date) as last_date
                FROM candles_daily cd
                WHERE cd.date >= CURRENT_DATE - 400  -- 400 calendar days covers 252+ trading days
                GROUP BY cd.security_id
                HAVING array_length(ARRAY_AGG(cd.close ORDER BY cd.date DESC), 1) >= 20
            ),
            sc100 AS (
                SELECT ARRAY_AGG(close ORDER BY date DESC) as sc_cl
                FROM candles_indices
                WHERE symbol = 'NIFTY SMALLCAP 100' AND date >= CURRENT_DATE - 400
            )
            SELECT
                a.security_id,
                su.symbol,
                su.company_name,
                a.cl[1] as cmp,
                (SELECT MAX(v) FROM UNNEST(a.hi[1:252]) v) as high_52w,
                (SELECT MIN(v) FROM UNNEST(a.lo[1:252]) v) as low_52w,
                ROUND(((a.cl[1] - (SELECT MAX(v) FROM UNNEST(a.hi[1:252]) v))
                    / NULLIF((SELECT MAX(v) FROM UNNEST(a.hi[1:252]) v), 0) * 100)::numeric, 2) as pct_from_high,
                ROUND(((a.cl[1] - (SELECT MIN(v) FROM UNNEST(a.lo[1:252]) v))
                    / NULLIF((SELECT MIN(v) FROM UNNEST(a.lo[1:252]) v), 0) * 100)::numeric, 2) as pct_from_low,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:5]) v), 0) as above_ma5,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:10]) v), 0) as above_ma10,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:20]) v), 0) as above_ma20,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:50]) v), 0) as above_ma50,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:200]) v), 0) as above_ma200,
                ROUND(((SELECT AVG(v) FROM UNNEST(a.turnover[1:20]) v) / 10000000.0)::numeric, 2) as liq_cr,
                ROUND(((SELECT AVG(v) FROM UNNEST(a.ranges[1:20]) v))::numeric, 2) as adr,
                -- Relative strength: stock return minus SC100 return (array-indexed = trading days)
                CASE WHEN a.cl[6] > 0 AND sc.sc_cl[6] > 0 THEN
                    ROUND((((a.cl[1] / a.cl[6] - 1) - (sc.sc_cl[1] / sc.sc_cl[6] - 1)) * 100)::numeric, 2)
                END as rs_1w,
                CASE WHEN a.cl[64] > 0 AND sc.sc_cl[64] > 0 THEN
                    ROUND((((a.cl[1] / a.cl[64] - 1) - (sc.sc_cl[1] / sc.sc_cl[64] - 1)) * 100)::numeric, 2)
                END as rs_3m,
                CASE WHEN a.cl[127] > 0 AND sc.sc_cl[127] > 0 THEN
                    ROUND((((a.cl[1] / a.cl[127] - 1) - (sc.sc_cl[1] / sc.sc_cl[127] - 1)) * 100)::numeric, 2)
                END as rs_6m,
                a.last_date
            FROM arrayed a
            JOIN stock_universe su ON a.security_id = su.security_id
            CROSS JOIN sc100 sc
            WHERE (SELECT AVG(v) FROM UNNEST(a.turnover[1:20]) v) / 10000000.0 > 0.01
        """)
        all_stocks = [dict(r) for r in cur.fetchall()]

        # Fix date serialization
        for s in all_stocks:
            if s.get("last_date"):
                s["last_date"] = str(s["last_date"])
            # Rename company_name → name for frontend
            if "company_name" in s:
                s["name"] = s.pop("company_name")

        # ── Step 2: Get sector mapping from index_constituents ──
        cur.execute("""
            SELECT stock_symbol, index_symbol
            FROM index_constituents
            WHERE index_symbol NOT IN ('NIFTY', 'NIFTY NEXT 50', 'NIFTY MID100 FREE',
                'NIFTY SMALLCAP 100', 'NIFTY MICROCAP250', 'NIFTY 500', 'NIFTY MIDSMALLCAP 400')
        """)
        sector_map = {}
        for r in cur.fetchall():
            sector_map[r["stock_symbol"]] = r["index_symbol"].replace("NIFTY ", "")

        # ── Step 3: Get leading sectors (index above 20MA) ──
        # Use 35 calendar days to ensure ~20+ trading days for the MA
        cur.execute("""
            WITH idx_arr AS (
                SELECT symbol,
                    (ARRAY_AGG(close ORDER BY date DESC))[1] as latest_close,
                    (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:20]) v) as ma20
                FROM candles_indices
                WHERE date >= CURRENT_DATE - 40
                GROUP BY symbol
                HAVING array_length(ARRAY_AGG(close ORDER BY date DESC), 1) >= 20
            )
            SELECT symbol,
                   latest_close > ma20 as above_20ma
            FROM idx_arr
            WHERE symbol LIKE 'NIFTY %'
            AND symbol NOT IN ('NIFTY', 'NIFTY NEXT 50', 'NIFTY MID100 FREE',
                'NIFTY SMALLCAP 100', 'NIFTY MICROCAP250', 'NIFTY 500', 'NIFTY MIDSMALLCAP 400')
        """)
        leading = {}
        all_sectors = []
        for r in cur.fetchall():
            name = r["symbol"].replace("NIFTY ", "")
            leading[name] = r["above_20ma"]
            all_sectors.append({"name": name, "leading": r["above_20ma"]})
        all_sectors.sort(key=lambda x: (not x["leading"], x["name"]))

        # ── Step 4: Attach sector and market cap estimate ──
        # Market cap from index membership
        cur.execute("""
            SELECT stock_symbol, index_symbol FROM index_constituents
            WHERE index_symbol IN ('NIFTY', 'NIFTY NEXT 50', 'NIFTY MID100 FREE',
                'NIFTY SMALLCAP 100', 'NIFTY MICROCAP250', 'NIFTY 500', 'NIFTY MIDSMALLCAP 400')
        """)
        mcap_map = {}
        mcap_tiers = {
            'NIFTY': 150000, 'NIFTY NEXT 50': 50000, 'NIFTY MID100 FREE': 20000,
            'NIFTY SMALLCAP 100': 8000, 'NIFTY 500': 10000,
            'NIFTY MIDSMALLCAP 400': 6000, 'NIFTY MICROCAP250': 1500,
        }
        for r in cur.fetchall():
            sym = r["stock_symbol"]
            tier = mcap_tiers.get(r["index_symbol"], 5000)
            if sym not in mcap_map or tier > mcap_map[sym]:
                mcap_map[sym] = tier

        # ── Upcoming earnings (forthcoming_results) lookup, batched ──
        # One query for every candidate so the per-stock filter check is O(1).
        # Same filter the position manager / watchlist use so the days-left
        # number is consistent across the app.
        candidate_sids = [str(s["security_id"]) for s in all_stocks if s.get("security_id")]
        next_result_map = {}  # security_id -> {date, days_left, purpose}
        recent_result_map = {}  # security_id -> {date, days_ago, purpose}
        if candidate_sids:
            try:
                cur.execute(
                    """
                    SELECT DISTINCT ON (fr.security_id)
                        fr.security_id,
                        fr.meeting_date,
                        fr.purpose,
                        (fr.meeting_date - CURRENT_DATE)::int AS days_left
                    FROM forthcoming_results fr
                    WHERE fr.security_id = ANY(%s)
                      AND fr.meeting_date >= CURRENT_DATE
                      AND (fr.purpose ILIKE '%%result%%'
                           OR fr.raw_purpose ILIKE '%%financial result%%'
                           OR fr.raw_purpose ILIKE '%%audited%%'
                           OR fr.raw_purpose ILIKE '%%quarterly result%%')
                    ORDER BY fr.security_id,
                             fr.meeting_date ASC,
                             (fr.purpose ILIKE '%%financial result%%') DESC,
                             (fr.purpose ILIKE '%%result%%') DESC
                    """,
                    (candidate_sids,),
                )
                for r in cur.fetchall():
                    md = r.get("meeting_date")
                    next_result_map[str(r["security_id"])] = {
                        "date": md.isoformat() if md and hasattr(md, "isoformat") else md,
                        "days_left": r.get("days_left"),
                        # Trading-day countdown — see /watchlists/.../items/enriched
                        # for rationale. Keep the calendar field for back-compat.
                        "trading_days_left": trading_days_until(md),
                        "purpose": r.get("purpose"),
                    }
            except Exception as e:
                print(f"⚠ screener forthcoming_results lookup: {e}")

            # Mirror lookup for the just-passed result so the screener row can
            # render a "RESULTS Xd ago" pill within the 10-day post-results
            # window.
            try:
                cur.execute(
                    """
                    SELECT DISTINCT ON (fr.security_id)
                        fr.security_id,
                        fr.meeting_date,
                        fr.purpose,
                        (CURRENT_DATE - fr.meeting_date)::int AS days_ago
                    FROM forthcoming_results fr
                    WHERE fr.security_id = ANY(%s)
                      AND fr.meeting_date < CURRENT_DATE
                      AND fr.meeting_date >= CURRENT_DATE - INTERVAL '10 days'
                      AND (fr.purpose ILIKE '%%result%%'
                           OR fr.raw_purpose ILIKE '%%financial result%%'
                           OR fr.raw_purpose ILIKE '%%audited%%'
                           OR fr.raw_purpose ILIKE '%%quarterly result%%')
                    ORDER BY fr.security_id,
                             fr.meeting_date DESC,
                             (fr.purpose ILIKE '%%financial result%%') DESC,
                             (fr.purpose ILIKE '%%result%%') DESC
                    """,
                    (candidate_sids,),
                )
                for r in cur.fetchall():
                    md = r.get("meeting_date")
                    recent_result_map[str(r["security_id"])] = {
                        "date": md.isoformat() if md and hasattr(md, "isoformat") else md,
                        "days_ago": r.get("days_ago"),
                        "purpose": r.get("purpose"),
                    }
            except Exception as e:
                print(f"⚠ screener recent results lookup: {e}")

            # Fallback for stocks BSE never advertised an intimation for: use
            # financials_quarterly's first-ingested date as a proxy. Only fills
            # gaps — recent_result_map (forthcoming) wins where it exists.
            try:
                cur.execute(
                    """
                    WITH latest AS (
                        SELECT security_id, MAX(period_end_date) AS q
                        FROM financials_quarterly
                        WHERE security_id = ANY(%s)
                        GROUP BY security_id
                    )
                    SELECT fq.security_id,
                           MIN(fq.created_at)::date AS reported_at,
                           MAX(fq.period_end_date)  AS period_end,
                           (CURRENT_DATE - MIN(fq.created_at)::date)::int AS days_ago
                    FROM financials_quarterly fq
                    JOIN latest l ON l.security_id = fq.security_id AND l.q = fq.period_end_date
                    WHERE fq.created_at >= CURRENT_DATE - INTERVAL '10 days'
                    GROUP BY fq.security_id
                    """,
                    (candidate_sids,),
                )
                for r in cur.fetchall():
                    sid = str(r["security_id"])
                    if sid in recent_result_map:
                        continue  # forthcoming row already won
                    md = r.get("reported_at")
                    period_end = r.get("period_end")
                    recent_result_map[sid] = {
                        "date": md.isoformat() if md and hasattr(md, "isoformat") else md,
                        "days_ago": r.get("days_ago"),
                        "purpose": f"Quarterly Results ({period_end})" if period_end else "Quarterly Results",
                    }
            except Exception as e:
                print(f"⚠ screener fq fallback: {e}")

        for s in all_stocks:
            s["sector"] = sector_map.get(s["symbol"], None)
            s["mcap_cr"] = mcap_map.get(s["symbol"], None)
            s["in_leading_sector"] = leading.get(s.get("sector"), False) if s["sector"] else False
            nr = next_result_map.get(str(s.get("security_id"))) if s.get("security_id") else None
            s["next_result_date"] = nr["date"] if nr else None
            s["next_result_purpose"] = nr["purpose"] if nr else None
            s["next_result_days_left"] = nr["days_left"] if nr else None
            s["next_result_trading_days_left"] = nr["trading_days_left"] if nr else None
            rr = recent_result_map.get(str(s.get("security_id"))) if s.get("security_id") else None
            s["recent_result_date"] = rr["date"] if rr else None
            s["recent_result_purpose"] = rr["purpose"] if rr else None
            s["recent_result_days_ago"] = rr["days_ago"] if rr else None
            # Convert Decimal to float for JSON
            for k in ["cmp", "high_52w", "low_52w", "pct_from_high", "pct_from_low",
                       "liq_cr", "adr", "rs_1w", "rs_3m", "rs_6m"]:
                if s[k] is not None:
                    s[k] = float(s[k])

        # ── Step 5: Apply filters ──
        results = all_stocks

        # 52W high proximity: "within X% of 52W high"
        if filters.get("max_pct_from_high") is not None:
            max_pct = float(filters["max_pct_from_high"])
            results = [s for s in results if s["pct_from_high"] is not None and s["pct_from_high"] >= -abs(max_pct)]

        # 52W low distance: "moved at least X% from 52W low"
        if filters.get("min_pct_from_low") is not None:
            min_pct = float(filters["min_pct_from_low"])
            results = [s for s in results if s["pct_from_low"] is not None and s["pct_from_low"] >= min_pct]

        # MA filters
        for ma in ["ma5", "ma10", "ma20", "ma50", "ma200"]:
            if filters.get(f"above_{ma}"):
                key = f"above_{ma}"
                results = [s for s in results if s.get(key)]

        # Liquidity
        if filters.get("min_liquidity") is not None:
            min_liq = float(filters["min_liquidity"])
            results = [s for s in results if (s["liq_cr"] or 0) >= min_liq]

        # Market cap
        if filters.get("min_mcap") is not None:
            min_mcap = float(filters["min_mcap"])
            results = [s for s in results if (s["mcap_cr"] or 0) >= min_mcap]
        if filters.get("max_mcap") is not None:
            max_mcap = float(filters["max_mcap"])
            results = [s for s in results if (s["mcap_cr"] or 0) <= max_mcap]

        # Relative strength
        if filters.get("rs_1w_positive"):
            results = [s for s in results if (s["rs_1w"] or 0) > 0]
        if filters.get("rs_3m_positive"):
            results = [s for s in results if (s["rs_3m"] or 0) > 0]
        if filters.get("rs_6m_positive"):
            results = [s for s in results if (s["rs_6m"] or 0) > 0]

        # Leading sectors
        if filters.get("leading_sectors_only"):
            results = [s for s in results if s["in_leading_sector"]]

        # Specific sectors
        if filters.get("sectors") and len(filters["sectors"]) > 0:
            selected = set(filters["sectors"])
            results = [s for s in results if s.get("sector") in selected]

        # Earnings filters — two modes:
        #   reports_within_days: keep only stocks reporting in <= N days (event hunters)
        #   exclude_reports_within_days: drop stocks reporting in <= N days (event avoiders)
        # Both inclusive on the lower bound (today = 0 days).
        if filters.get("reports_within_days") is not None:
            try:
                n = int(filters["reports_within_days"])
                results = [s for s in results
                           if s.get("next_result_days_left") is not None
                           and s["next_result_days_left"] <= n]
            except (TypeError, ValueError):
                pass
        if filters.get("exclude_reports_within_days") is not None:
            try:
                n = int(filters["exclude_reports_within_days"])
                results = [s for s in results
                           if s.get("next_result_days_left") is None
                           or s["next_result_days_left"] > n]
            except (TypeError, ValueError):
                pass

        # Sort by liquidity desc (most liquid first)
        sort_by = filters.get("sort_by", "liq_cr")
        sort_desc = filters.get("sort_desc", True)
        results.sort(key=lambda s: float(s.get(sort_by) or 0), reverse=sort_desc)

        # Limit
        limit = min(int(filters.get("limit", 100)), 500)
        results = results[:limit]

        return jsonify({
            "results": results,
            "total_scanned": len(all_stocks),
            "total_matched": len(results),
            "sectors": all_sectors,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[watchlist] stock lookup error: {e}")
        return jsonify({"error": "Failed to check watchlists"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════
#   SAVED SCANNERS — CRUD for custom scanner presets
# ═══════════════════════════════════════════════════════════════

@screener_bp.route("/api/scanner/presets", methods=["GET"])
def list_presets():
    """List all saved scanner presets."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM saved_scanners WHERE user_id = %s ORDER BY pinned DESC, created_at DESC", (g.user_id,))
        return jsonify({"presets": [dict(r) for r in cur.fetchall()]})
    except Exception as e:
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/scanner/presets", methods=["POST"])
def save_preset():
    """Save a new scanner preset."""
    data = request.json or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    filters = data.get("filters", {})
    description = data.get("description", "")
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO saved_scanners (name, filters, description, user_id)
            VALUES (%s, %s, %s, %s)
            RETURNING id, name, filters, description, pinned, created_at
        """, (name, __import__('json').dumps(filters), description, g.user_id))
        conn.commit()
        row = dict(cur.fetchone())
        return jsonify({"preset": row}), 201
    except Exception as e:
        conn.rollback()
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/scanner/presets/<int:preset_id>", methods=["DELETE"])
def delete_preset(preset_id):
    """Delete a saved scanner preset."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM saved_scanners WHERE id = %s AND user_id = %s RETURNING id", (preset_id, g.user_id))
        conn.commit()
        if cur.fetchone():
            return jsonify({"deleted": True})
        return jsonify({"error": "Not found"}), 404
    except Exception as e:
        conn.rollback()
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/scanner/presets/<int:preset_id>/pin", methods=["PATCH"])
def toggle_pin(preset_id):
    """Toggle pin status (pinned shows on hub dashboard)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE saved_scanners SET pinned = NOT pinned WHERE id = %s AND user_id = %s
            RETURNING id, pinned
        """, (preset_id, g.user_id))
        conn.commit()
        row = cur.fetchone()
        if row:
            return jsonify({"id": row["id"], "pinned": row["pinned"]})
        return jsonify({"error": "Not found"}), 404
    except Exception as e:
        conn.rollback()
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/scanner/presets/<int:preset_id>/ran", methods=["PATCH"])
def mark_ran(preset_id):
    """Update last_run metadata + store sector breakdown for history chart."""
    data = request.json or {}
    conn = get_db()
    try:
        cur = conn.cursor()
        count = data.get("count", 0)
        sectors = data.get("sector_breakdown", {})

        # Update preset metadata
        cur.execute("""
            UPDATE saved_scanners SET last_run_at = NOW(), last_run_count = %s WHERE id = %s AND user_id = %s
        """, (count, preset_id, g.user_id))

        # Upsert run history (one row per day per preset)
        cur.execute("""
            INSERT INTO scanner_run_history (preset_id, run_date, total_count, sector_breakdown)
            VALUES (%s, CURRENT_DATE, %s, %s)
            ON CONFLICT (preset_id, run_date)
            DO UPDATE SET total_count = EXCLUDED.total_count,
                          sector_breakdown = EXCLUDED.sector_breakdown,
                          created_at = NOW()
        """, (preset_id, count, __import__('json').dumps(sectors)))

        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/scanner/presets/<int:preset_id>/history", methods=["GET"])
def preset_history(preset_id):
    """Get last 30 days of run history for a preset — for the mini chart."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT srh.run_date, srh.total_count, srh.sector_breakdown
            FROM scanner_run_history srh
            JOIN saved_scanners ss ON ss.id = srh.preset_id
            WHERE srh.preset_id = %s AND ss.user_id = %s
            ORDER BY srh.run_date DESC
            LIMIT 30
        """, (preset_id, g.user_id))
        rows = [dict(r) for r in cur.fetchall()]
        # Reverse to chronological order
        rows.reverse()
        return jsonify({"history": rows, "preset_id": preset_id})
    except Exception as e:
        print(f"[watchlist] stock lookup error: {e}")
        return jsonify({"error": "Failed to check watchlists"}), 500
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════
#   EXPLORE A STOCK — Comprehensive single-stock intelligence
#   /api/explore/stock/<symbol>
# ═══════════════════════════════════════════════════════════════

@screener_bp.route("/api/explore/stock/<symbol>", methods=["GET"])
def explore_stock(symbol):
    """Get comprehensive data for a single stock: technical + AI insights."""
    conn = get_db()
    try:
        cur = conn.cursor()
        symbol = symbol.upper().strip()

        # ── Step 1: Find the stock ──
        cur.execute("SELECT security_id, symbol, company_name FROM stock_universe WHERE symbol = %s AND is_active = true", (symbol,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": f"Stock '{symbol}' not found"}), 404
        sec_id = row["security_id"]
        company_name = row["company_name"]

        # ── Step 2: Check cache for today ──
        cur.execute("SELECT technical, ai_insights FROM explore_cache WHERE symbol = %s AND cache_date = CURRENT_DATE", (symbol,))
        cached = cur.fetchone()
        if cached and cached.get("technical"):
            result = cached["technical"]
            result["ai_insights"] = cached.get("ai_insights")
            result["cached"] = True
            track_stock_view(getattr(g, "user_id", None), symbol)
            return jsonify(result)

        # ── Step 3: Compute technical data ──
        cur.execute("""
            WITH arrayed AS (
                SELECT
                    ARRAY_AGG(close ORDER BY date DESC) as cl,
                    ARRAY_AGG(high ORDER BY date DESC) as hi,
                    ARRAY_AGG(low ORDER BY date DESC) as lo,
                    ARRAY_AGG(volume ORDER BY date DESC) as vol,
                    ARRAY_AGG(volume * close ORDER BY date DESC) as turnover,
                    ARRAY_AGG(date ORDER BY date DESC) as dates,
                    ARRAY_AGG(ABS(high - low) / NULLIF(close, 0) * 100 ORDER BY date DESC) as ranges
                FROM candles_daily
                WHERE security_id = %s AND date >= CURRENT_DATE - 400
            ),
            sc100 AS (
                SELECT ARRAY_AGG(close ORDER BY date DESC) as sc_cl
                FROM candles_indices WHERE symbol = 'NIFTY SMALLCAP 100' AND date >= CURRENT_DATE - 400
            )
            SELECT
                a.cl[1] as cmp,
                a.cl[2] as prev_close,
                a.dates[1] as last_date,
                -- 52W range
                (SELECT MAX(v) FROM UNNEST(a.hi[1:252]) v) as high_52w,
                (SELECT MIN(v) FROM UNNEST(a.lo[1:252]) v) as low_52w,
                -- MA values
                (SELECT AVG(v) FROM UNNEST(a.cl[1:5]) v) as ma5_val,
                (SELECT AVG(v) FROM UNNEST(a.cl[1:10]) v) as ma10_val,
                (SELECT AVG(v) FROM UNNEST(a.cl[1:20]) v) as ma20_val,
                (SELECT AVG(v) FROM UNNEST(a.cl[1:50]) v) as ma50_val,
                (SELECT AVG(v) FROM UNNEST(a.cl[1:200]) v) as ma200_val,
                -- Above/below
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:5]) v), 0) as above_ma5,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:10]) v), 0) as above_ma10,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:20]) v), 0) as above_ma20,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:50]) v), 0) as above_ma50,
                a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:200]) v), 0) as above_ma200,
                -- Liquidity (20d avg turnover in Cr)
                ROUND(((SELECT AVG(v) FROM UNNEST(a.turnover[1:20]) v) / 10000000.0)::numeric, 1) as liq_cr,
                -- ADR
                ROUND(((SELECT AVG(v) FROM UNNEST(a.ranges[1:20]) v))::numeric, 2) as adr,
                -- Day change
                CASE WHEN a.cl[2] > 0 THEN ROUND(((a.cl[1] - a.cl[2]) / a.cl[2] * 100)::numeric, 2) END as day_change,
                -- RS vs SC100
                CASE WHEN a.cl[6] > 0 AND sc.sc_cl[6] > 0 THEN
                    ROUND((((a.cl[1] / a.cl[6] - 1) - (sc.sc_cl[1] / sc.sc_cl[6] - 1)) * 100)::numeric, 2)
                END as rs_1w,
                CASE WHEN a.cl[64] > 0 AND sc.sc_cl[64] > 0 THEN
                    ROUND((((a.cl[1] / a.cl[64] - 1) - (sc.sc_cl[1] / sc.sc_cl[64] - 1)) * 100)::numeric, 2)
                END as rs_3m,
                CASE WHEN a.cl[127] > 0 AND sc.sc_cl[127] > 0 THEN
                    ROUND((((a.cl[1] / a.cl[127] - 1) - (sc.sc_cl[1] / sc.sc_cl[127] - 1)) * 100)::numeric, 2)
                END as rs_6m,
                -- Total candles
                array_length(a.cl, 1) as total_candles
            FROM arrayed a CROSS JOIN sc100 sc
        """, (sec_id,))
        tech = cur.fetchone()
        if not tech:
            return jsonify({"error": "No price data available"}), 404
        tech = dict(tech)

        # Convert types for JSON
        for k in list(tech.keys()):
            if tech[k] is not None and hasattr(tech[k], '__float__'):
                tech[k] = float(tech[k])
        if tech.get("last_date"):
            tech["last_date"] = str(tech["last_date"])

        # ── Step 4: Get sector from index_constituents ──
        cur.execute("""
            SELECT index_symbol FROM index_constituents
            WHERE stock_symbol = %s
            AND index_symbol NOT IN ('NIFTY', 'NIFTY NEXT 50', 'NIFTY MID100 FREE',
                'NIFTY SMALLCAP 100', 'NIFTY MICROCAP250', 'NIFTY 500', 'NIFTY MIDSMALLCAP 400')
            LIMIT 1
        """, (symbol,))
        sector_row = cur.fetchone()
        sector = sector_row["index_symbol"].replace("NIFTY ", "") if sector_row else None

        # ── Step 5: Get MCAP tier from index membership ──
        mcap_tiers = {
            "NIFTY": 200000, "NIFTY NEXT 50": 80000, "NIFTY MID100 FREE": 30000,
            "NIFTY MIDSMALLCAP 400": 8000, "NIFTY SMALLCAP 100": 12000,
            "NIFTY MICROCAP250": 3000,
        }
        cur.execute("""
            SELECT index_symbol FROM index_constituents WHERE stock_symbol = %s
        """, (symbol,))
        mcap_cr = 5000  # default
        for r in cur.fetchall():
            tier = mcap_tiers.get(r["index_symbol"], 5000)
            mcap_cr = max(mcap_cr, tier)
        mcap_label = "Large Cap" if mcap_cr >= 80000 else "Mid Cap" if mcap_cr >= 12000 else "Small Cap" if mcap_cr >= 5000 else "Micro Cap"

        # ── Step 6: Get sector MA health (is sector leading?) ──
        sector_health = None
        if sector_row:
            cur.execute("""
                WITH idx_arr AS (
                    SELECT
                        (ARRAY_AGG(close ORDER BY date DESC))[1] as latest,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:5]) v) as sma5,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:10]) v) as sma10,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:20]) v) as sma20,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:50]) v) as sma50,
                        (SELECT AVG(v) FROM UNNEST((ARRAY_AGG(close ORDER BY date DESC))[1:200]) v) as sma200
                    FROM candles_indices
                    WHERE symbol = %s AND date >= CURRENT_DATE - 400
                )
                SELECT latest,
                    latest > sma5 as above5, latest > sma10 as above10,
                    latest > sma20 as above20, latest > sma50 as above50,
                    latest > sma200 as above200
                FROM idx_arr
            """, (sector_row["index_symbol"],))
            sh = cur.fetchone()
            if sh:
                sh = dict(sh)
                score = sum(1 for k in ["above5","above10","above20","above50","above200"] if sh.get(k))
                sector_health = {
                    "score": score,
                    "above": {k: sh.get(k, False) for k in ["above5","above10","above20","above50","above200"]},
                    "verdict": "Strong" if score >= 4 else "Healthy" if score >= 3 else "Mixed" if score >= 2 else "Weak",
                }

        # ── Build result ──
        cmp = tech.get("cmp", 0)
        high52 = tech.get("high_52w", 0)
        low52 = tech.get("low_52w", 0)
        pct_from_high = round(((cmp - high52) / high52) * 100, 1) if high52 else 0
        pct_from_low = round(((cmp - low52) / low52) * 100, 1) if low52 else 0
        range_position = round(((cmp - low52) / (high52 - low52)) * 100, 1) if high52 != low52 else 50

        liq = float(tech.get("liq_cr") or 0)
        liq_verdict = "Institutional Grade" if liq >= 50 else "Moderate" if liq >= 10 else "Low — risky at your portfolio size"

        mas_above = sum(1 for k in ["above_ma5","above_ma10","above_ma20","above_ma50","above_ma200"] if tech.get(k))
        ma_verdict = "All Aligned" if mas_above == 5 else "Strong" if mas_above >= 4 else "Mixed" if mas_above >= 2 else "Weak"

        result = {
            "symbol": symbol,
            "company_name": company_name,
            "security_id": sec_id,
            "cmp": cmp,
            "day_change": tech.get("day_change"),
            "last_date": tech.get("last_date"),
            "sector": sector,
            "mcap_cr": mcap_cr,
            "mcap_label": mcap_label,
            # MAs
            "mas": {
                "ma5": {"value": round(float(tech.get("ma5_val") or 0), 2), "above": tech.get("above_ma5", False)},
                "ma10": {"value": round(float(tech.get("ma10_val") or 0), 2), "above": tech.get("above_ma10", False)},
                "ma20": {"value": round(float(tech.get("ma20_val") or 0), 2), "above": tech.get("above_ma20", False)},
                "ma50": {"value": round(float(tech.get("ma50_val") or 0), 2), "above": tech.get("above_ma50", False)},
                "ma200": {"value": round(float(tech.get("ma200_val") or 0), 2), "above": tech.get("above_ma200", False)},
            },
            "mas_above_count": mas_above,
            "ma_verdict": ma_verdict,
            # 52W
            "high_52w": high52,
            "low_52w": low52,
            "pct_from_high": pct_from_high,
            "pct_from_low": pct_from_low,
            "range_position": range_position,
            # Liquidity
            "liq_cr": liq,
            "liq_verdict": liq_verdict,
            "liq_sufficient": liq >= 50,
            # ADR
            "adr": tech.get("adr"),
            # RS
            "rs": {"rs_1w": tech.get("rs_1w"), "rs_3m": tech.get("rs_3m"), "rs_6m": tech.get("rs_6m")},
            # Sector health
            "sector_health": sector_health,
        }

        # ── Cache technical data ──
        import json
        cur.execute("""
            INSERT INTO explore_cache (symbol, cache_date, technical)
            VALUES (%s, CURRENT_DATE, %s)
            ON CONFLICT (symbol, cache_date)
            DO UPDATE SET technical = EXCLUDED.technical, created_at = NOW()
        """, (symbol, json.dumps(result)))
        conn.commit()

        track_stock_view(getattr(g, "user_id", None), symbol)
        return jsonify(result)

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[screener] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@screener_bp.route("/api/explore/stock/<symbol>/insights", methods=["GET"])
def explore_insights(symbol):
    """Get AI-generated insights for a stock (cached per day)."""
    import os, json
    conn = get_db()
    try:
        cur = conn.cursor()
        symbol = symbol.upper().strip()

        # Check cache
        cur.execute("SELECT ai_insights FROM explore_cache WHERE symbol = %s AND cache_date = CURRENT_DATE", (symbol,))
        cached = cur.fetchone()
        if cached and cached.get("ai_insights"):
            return jsonify({"insights": cached["ai_insights"], "cached": True})

        # Get company name
        cur.execute("SELECT company_name FROM stock_universe WHERE symbol = %s", (symbol,))
        su = cur.fetchone()
        company_name = su["company_name"] if su else symbol

        # Get sector
        cur.execute("""
            SELECT index_symbol FROM index_constituents
            WHERE stock_symbol = %s
            AND index_symbol NOT IN ('NIFTY', 'NIFTY NEXT 50', 'NIFTY MID100 FREE',
                'NIFTY SMALLCAP 100', 'NIFTY MICROCAP250', 'NIFTY 500', 'NIFTY MIDSMALLCAP 400')
            LIMIT 1
        """, (symbol,))
        sec_row = cur.fetchone()
        sector = sec_row["index_symbol"].replace("NIFTY ", "") if sec_row else "Unknown"

        # Call Claude API with web search
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return jsonify({"insights": None, "error": "API key not configured"}), 200

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        prompt = f"""Analyze {company_name} (NSE: {symbol}) in the {sector} sector for an Indian equity portfolio manager.

Provide a structured analysis in EXACTLY this JSON format (no markdown, no backticks, pure JSON):
{{
  "recent_results": "2-3 sentence summary of the most recent quarterly results — revenue, profit, YoY growth, margins. Be specific with numbers.",
  "triggers": ["Trigger 1: specific catalyst or development", "Trigger 2: ...", "Trigger 3: ..."],
  "sectoral_tailwind": "2-3 sentences on whether the {sector} sector has structural tailwinds right now — government policies, global trends, demand drivers.",
  "sub_industry": "The specific sub-industry within {sector} (e.g., 'Aluminium Smelting' within Metal, 'Two-Wheelers' within Auto)",
  "risk_factors": ["Risk 1", "Risk 2"]
}}

Use the most recent data available. Be factual and specific — no vague statements. Include actual numbers where possible."""

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text from response
        insights_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                insights_text += block.text

        # Parse JSON
        insights = None
        try:
            # Strip any markdown fences
            clean = insights_text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()
            if clean.startswith("json"):
                clean = clean[4:].strip()
            insights = json.loads(clean)
        except Exception:
            # If JSON parse fails, return raw text
            insights = {"raw": insights_text, "parse_error": True}

        # Cache insights
        cur.execute("""
            UPDATE explore_cache SET ai_insights = %s
            WHERE symbol = %s AND cache_date = CURRENT_DATE
        """, (json.dumps(insights), symbol))
        conn.commit()

        return jsonify({"insights": insights, "cached": False})

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[screener] error: {e}")
        return jsonify({"insights": None, "error": "Internal error"}), 200
    finally:
        close_db(conn)
