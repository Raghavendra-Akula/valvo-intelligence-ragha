"""
explore_routes.py — Endpoints powering the Explore V2 landing.

Exposes:
  GET  /api/explore/trending?limit=20         — global, top symbols today
  GET  /api/explore/recently-viewed?limit=4   — per-user, latest distinct

Both endpoints enrich symbols with display metadata (company_name, last
close, day_change_pct) by joining stock_view_daily / stock_view_events
with stock_universe + the latest two rows of candles_eod.

A best-effort `track_stock_view(user_id, symbol)` helper writes to both
tracking tables; it's invoked from /api/explore/stock/<symbol> after a
successful response. Any failure is swallowed so the user-facing
endpoint isn't affected.

Schemas live in Backend/database/create_stock_view_tracking.sql.
"""
import random

from flask import Blueprint, g, jsonify, request

from database.database import close_db, get_db
from extensions import limiter

explore_bp = Blueprint("explore", __name__)


# ─── Curated fallback ───────────────────────────────────────────────
# When the live aggregate is empty (cold start, table missing, etc.) we
# return a randomized sample from this pool so the Trending grid never
# renders blank. Pool size > typical request limit (20) so successive
# loads feel fresh.
FALLBACK_TRENDING = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "BSE", "TATAMOTORS", "VEDL", "HINDCOPPER", "SAIL",
    "ADANIPOWER", "WIPRO", "ITC", "SBIN", "AXISBANK",
    "LT", "MARUTI", "BAJFINANCE", "ASIANPAINT", "TITAN",
    "HCLTECH", "TECHM", "SUNPHARMA", "ULTRACEMCO", "POWERGRID",
    "NTPC", "COALINDIA", "ONGC", "BHARTIARTL", "TATASTEEL",
]


def _bare_records(symbols):
    """Symbol-only records for when the DB or enrichment fails — keeps the
    UI rendering instead of going blank."""
    return [{
        "symbol": s, "company_name": s, "security_id": None,
        "last_close": None, "day_change_pct": None,
    } for s in symbols]


def _random_fallback(limit):
    return random.sample(FALLBACK_TRENDING, min(limit, len(FALLBACK_TRENDING)))


def _enrich_symbols(cur, symbols):
    """Look up company_name + latest close + day-change% for a symbol list.
    Returns list-of-dicts in the same order as `symbols`. Missing rows
    are dropped silently — better to show 5 cards than a broken row."""
    if not symbols:
        return []
    cur.execute(
        """
        SELECT symbol, company_name, security_id
        FROM stock_universe
        WHERE symbol = ANY(%s) AND is_active = true
        """,
        (symbols,),
    )
    meta = {r["symbol"]: r for r in cur.fetchall()}

    sec_ids = [r["security_id"] for r in meta.values() if r.get("security_id")]
    last_close_by_sid = {}
    day_chg_by_sid = {}
    if sec_ids:
        cur.execute(
            """
            WITH ranked AS (
                SELECT
                    security_id,
                    close,
                    ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
                FROM candles_eod
                WHERE security_id = ANY(%s)
            )
            SELECT security_id,
                   MAX(close) FILTER (WHERE rn = 1) AS last_close,
                   MAX(close) FILTER (WHERE rn = 2) AS prev_close
            FROM ranked
            WHERE rn <= 2
            GROUP BY security_id
            """,
            (sec_ids,),
        )
        for r in cur.fetchall():
            sid = r["security_id"]
            last = r.get("last_close")
            prev = r.get("prev_close")
            last_close_by_sid[sid] = last
            if last is not None and prev:
                day_chg_by_sid[sid] = round(float(last - prev) / float(prev) * 100, 2)

    out = []
    for sym in symbols:
        m = meta.get(sym)
        if not m:
            continue
        sid = m.get("security_id")
        out.append({
            "symbol": sym,
            "company_name": m.get("company_name") or sym,
            "security_id": sid,
            "last_close": last_close_by_sid.get(sid),
            "day_change_pct": day_chg_by_sid.get(sid),
        })
    return out


def track_stock_view(user_id, symbol):
    """Best-effort log of a stock view. Silent on failure so the caller's
    primary response is never broken."""
    if not user_id or not symbol:
        return
    sym = str(symbol).upper().strip()
    conn = None
    try:
        conn = get_db()
        if not conn:
            return
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO stock_view_events (user_id, symbol) VALUES (%s, %s)",
            (user_id, sym),
        )
        cur.execute(
            """
            INSERT INTO stock_view_daily (view_date, symbol, view_count, last_viewed)
            VALUES (CURRENT_DATE, %s, 1, NOW())
            ON CONFLICT (view_date, symbol)
            DO UPDATE SET view_count = stock_view_daily.view_count + 1,
                          last_viewed = NOW()
            """,
            (sym,),
        )
        conn.commit()
    except Exception as exc:
        print(f"[explore] track_stock_view failed for {sym}: {exc}")
    finally:
        if conn is not None:
            close_db(conn)


@explore_bp.route("/api/explore/trending", methods=["GET"])
@limiter.limit("60 per minute")
def trending():
    """Top trending stock symbols today (global, daily). Falls back to a
    randomized sample of the curated pool when the live aggregate is
    empty (or the tracking table doesn't exist yet) so the page never
    renders blank."""
    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 50))
    except (TypeError, ValueError):
        limit = 20

    conn = get_db()
    if not conn:
        # No DB at all — best we can do is the static random fallback.
        pool = _random_fallback(limit)
        return jsonify({"results": _bare_records(pool), "source": "static"})

    try:
        cur = conn.cursor()

        # Live aggregate read — wrapped because the tracking table may not
        # exist yet (migration not run). On any failure we rollback the
        # aborted transaction state and fall through to the fallback.
        live = []
        try:
            cur.execute(
                """
                SELECT symbol
                FROM stock_view_daily
                WHERE view_date = CURRENT_DATE
                ORDER BY view_count DESC, last_viewed DESC
                LIMIT %s
                """,
                (limit,),
            )
            live = [r["symbol"] for r in cur.fetchall()]
        except Exception as inner_exc:
            print(f"[explore] /trending: skipping live read ({inner_exc})")
            try:
                conn.rollback()
            except Exception:
                pass

        if live:
            # Top-N from live data; backfill from fallback if thin.
            merged, seen = [], set()
            for sym in (*live, *FALLBACK_TRENDING):
                if sym in seen:
                    continue
                merged.append(sym)
                seen.add(sym)
                if len(merged) >= limit:
                    break
            source = "live"
        else:
            # Cold start / no views yet today → random sample from the
            # curated pool so the page feels fresh on each visit.
            merged = _random_fallback(limit)
            source = "fallback"

        try:
            results = _enrich_symbols(cur, merged)
        except Exception as enrich_exc:
            print(f"[explore] /trending: enrich failed ({enrich_exc})")
            try:
                conn.rollback()
            except Exception:
                pass
            results = []

        # Final guard — if enrichment couldn't resolve anything (symbols
        # missing from stock_universe, etc.), surface bare symbol records
        # so the UI still renders cards instead of going blank.
        if not results:
            results = _bare_records(merged)
            source = "minimal" if source == "live" else source

        return jsonify({
            "results": results,
            "source": source,
            "live_count": len(live),
        })
    except Exception as exc:
        print(f"[explore] /trending failed: {exc}")
        pool = _random_fallback(limit)
        return jsonify({"results": _bare_records(pool), "source": "static", "error": str(exc)})
    finally:
        close_db(conn)


@explore_bp.route("/api/explore/recently-viewed", methods=["GET"])
@limiter.limit("60 per minute")
def recently_viewed():
    """Latest distinct symbols the current user has opened."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"results": []})
    try:
        limit = max(1, min(int(request.args.get("limit", 4)), 20))
    except (TypeError, ValueError):
        limit = 4

    conn = get_db()
    if not conn:
        return jsonify({"results": []})
    try:
        cur = conn.cursor()
        # DISTINCT ON keeps the most recent view per symbol; outer ORDER BY
        # restores reverse-chronological order across symbols.
        try:
            cur.execute(
                """
                SELECT symbol FROM (
                    SELECT DISTINCT ON (symbol) symbol, viewed_at
                    FROM stock_view_events
                    WHERE user_id = %s
                    ORDER BY symbol, viewed_at DESC
                ) s
                ORDER BY viewed_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            symbols = [r["symbol"] for r in cur.fetchall()]
        except Exception as inner_exc:
            print(f"[explore] /recently-viewed: skipping live read ({inner_exc})")
            try:
                conn.rollback()
            except Exception:
                pass
            return jsonify({"results": []})

        return jsonify({"results": _enrich_symbols(cur, symbols)})
    except Exception as exc:
        print(f"[explore] /recently-viewed failed: {exc}")
        return jsonify({"results": []})
    finally:
        close_db(conn)
