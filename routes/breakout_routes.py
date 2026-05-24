"""
Breakout Success Intelligence routes
====================================
Public surface (mounted under @app.before_request require_auth):

  GET   /api/breakout/dashboard/options
            → preset slice keys (mcap_tiers + liq_tiers from config) for the UI.

  GET   /api/breakout/edge-stats?window=5|10|20&lookback=
        &sector&liq_min&liq_max&mcap_min&mcap_max
            → path-aware FAILED/MODERATE/SUCCESSFUL rollup per window.

  GET   /api/breakout/events?window=10&from=&to=&outcome=&sector=
        &liq_min&liq_max&mcap_min&mcap_max&limit=50&offset=0
            → paged list of breakouts joined with stock_universe symbol/name.

  GET   /api/breakout/pivots/by-stock/<security_id>
            → all pivots for a single stock (for ValvoChart overlay).

  GET   /api/breakout/squat-summary?window=5|10|20
        &sector&liq_min&liq_max&mcap_min&mcap_max
            → simple one-row counts for the dashboard Squat tile: last N
              distinct breakout dates → {total, no/weak/strong, ratio}.

  GET   /api/breakout/config
  PATCH /api/breakout/config           (admin-only)
            → JSONB knob — runtime adjustable detection thresholds.
"""
import json
from datetime import date, datetime, timedelta
from flask import Blueprint, jsonify, request, g
from extensions import limiter
from database.database import get_db, close_db
from services.breakout_detection import load_config
from services.breakout_edge_stats import edge_stats

breakout_bp = Blueprint("breakout", __name__)

# Admin emails (mirror admin_routes.py / settings_routes.py guard)
ADMIN_EMAILS = {"rohit@thevalvo.com"}


def _parse_float(name):
    v = request.args.get(name)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_int(name, default=None):
    v = request.args.get(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ──────────────────────────────────────────────────────────────────────────────
# /api/breakout/dashboard/options — surface tier presets to the UI
# ──────────────────────────────────────────────────────────────────────────────

@breakout_bp.route("/api/breakout/dashboard/options", methods=["GET"])
@limiter.limit("30 per minute")
def breakout_dashboard_options():
    cfg = load_config()
    return jsonify({
        "windows": [5, 10, 20],
        "mcap_tiers": cfg.get("mcap_tiers", {}),
        "liq_tiers": cfg.get("liq_tiers", {}),
    })


# ──────────────────────────────────────────────────────────────────────────────
# /api/breakout/edge-stats — rigorous edge-quality math (Wilson CIs, R, regime)
# ──────────────────────────────────────────────────────────────────────────────

@breakout_bp.route("/api/breakout/edge-stats", methods=["GET"])
@limiter.limit("60 per minute")
def breakout_edge_stats():
    """v2 dashboard math: Wilson CIs on win rate, R-multiples (O'Neil 7%
    structural stop), expectancy / profit factor, path-honesty diagnostic,
    and breadth-regime stratification of returns. Read-only over breakout_events.
    """
    window = _parse_int("window", 10)
    # Path-outcome only resolves 5d and 10d; collapse 20 → 10.
    if window not in (5, 10):
        window = 10
    lookback = _parse_int("lookback", 90) or 90
    lookback = max(14, min(lookback, 365))
    try:
        payload = edge_stats(
            window_days=window,
            as_of_date=date.today(),
            lookback_days=lookback,
            liq_min=_parse_float("liq_min"),
            liq_max=_parse_float("liq_max"),
            mcap_min=_parse_float("mcap_min"),
            mcap_max=_parse_float("mcap_max"),
            sector=request.args.get("sector"),
        )
        return jsonify({"payload": payload})
    except Exception as e:
        print(f"❌ breakout/edge-stats error: {e}")
        return jsonify({"error": "Internal error"}), 500


# ──────────────────────────────────────────────────────────────────────────────
# /api/breakout/events — paged history of breakouts
# ──────────────────────────────────────────────────────────────────────────────

@breakout_bp.route("/api/breakout/events", methods=["GET"])
@limiter.limit("60 per minute")
def breakout_events():
    window = _parse_int("window", 10)
    if window not in (5, 10, 20):
        window = 10
    # All three windows now have path-aware columns.
    limit = max(1, min(_parse_int("limit", 50), 500))
    offset = max(0, _parse_int("offset", 0))

    where = []
    params: list = []

    date_from = request.args.get("from")
    date_to = request.args.get("to")
    if date_from:
        where.append("e.breakout_date >= %s"); params.append(date_from)
    if date_to:
        where.append("e.breakout_date <= %s"); params.append(date_to)

    # Outcome filter — accepts either a specific bucket name or a group alias
    # ("win" / "flat" / "loss") which expands to the matching bucket array.
    # ``failed_pre_5pct_drop`` lives as two sub-buckets in the DB (close + wick);
    # ``flat`` rolls in the modest 5–7% success row to match the merged view
    # the frontend renders.
    OUTCOME_GROUPS = {
        "win":  ("success_strong_gt_10", "success_7_to_10"),
        "flat": ("moderate_reversed", "success_5_to_7"),
        "loss": (
            "failed_d1_reversal",
            "failed_pre_5pct_drop_close",
            "failed_pre_5pct_drop_wick",
            "failed_no_5pct_in_window",
        ),
    }
    outcome = (request.args.get("outcome") or "").strip()
    if outcome:
        if outcome in OUTCOME_GROUPS:
            where.append(f"e.path_outcome_{window}d = ANY(%s::text[])")
            params.append(list(OUTCOME_GROUPS[outcome]))
        else:
            where.append(f"e.path_outcome_{window}d = %s"); params.append(outcome)

    sector = request.args.get("sector")
    if sector:
        where.append("e.sector = %s"); params.append(sector)

    for k, op in (("liq_min", ">="), ("liq_max", "<")):
        v = _parse_float(k)
        if v is not None:
            where.append(f"e.liq_cr_at_breakout {op} %s"); params.append(v)
    for k, op in (("mcap_min", ">="), ("mcap_max", "<")):
        v = _parse_float(k)
        if v is not None:
            where.append(f"e.mcap_cr_at_breakout {op} %s"); params.append(v)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '15000'")
        # Total count for pagination
        cur.execute(f"SELECT COUNT(*) AS n FROM breakout_events e {where_sql}", params)
        total = int((cur.fetchone() or {}).get("n", 0))

        cur.execute(
            f"""
            SELECT e.id, e.security_id, su.symbol, su.company_name,
                   e.breakout_date, e.breakout_close, e.pivot_price, e.entry_price,
                   e.volume, e.gap_up_pct, e.gap_extended,
                   e.gain_d1_pct, e.gain_d2_pct,
                   e.path_outcome_5d,  e.peak_pct_5d,  e.final_vs_pivot_5d,  e.hit_5pct_day_5d,  e.below_pivot_day_5d,
                   e.path_outcome_10d, e.peak_pct_10d, e.final_vs_pivot_10d, e.hit_5pct_day_10d, e.below_pivot_day_10d,
                   e.path_outcome_20d, e.peak_pct_20d, e.final_vs_pivot_20d, e.hit_5pct_day_20d, e.below_pivot_day_20d,
                   e.squat_grade, e.squat_giveback_pct,
                   e.liq_cr_at_breakout, e.mcap_cr_at_breakout,
                   e.sector, e.industry,
                   p.range_high, p.range_low, p.range_width_pct,
                   p.base_length_days, p.tightness_grade, p.base_quality
              FROM breakout_events e
              JOIN breakout_pivots p   ON p.id = e.pivot_id
              LEFT JOIN stock_universe su ON su.security_id = e.security_id
              {where_sql}
             ORDER BY e.breakout_date DESC, e.id DESC
             LIMIT %s OFFSET %s
            """,
            params + [limit, offset],
        )
        rows = cur.fetchall()
        events = []
        for r in rows:
            events.append({
                "id": r["id"],
                "security_id": r["security_id"],
                "symbol": r.get("symbol"),
                "company_name": r.get("company_name"),
                "breakout_date": str(r["breakout_date"]),
                "breakout_close": float(r["breakout_close"]),
                "pivot_price": float(r["pivot_price"]),
                "entry_price": float(r["entry_price"]),
                "volume": int(r["volume"] or 0),
                "gap_up_pct": float(r["gap_up_pct"] or 0),
                "gap_extended": bool(r["gap_extended"]),
                "gain_d1_pct": _flt(r, "gain_d1_pct"),
                "gain_d2_pct": _flt(r, "gain_d2_pct"),
                "squat_grade": r.get("squat_grade"),
                "squat_giveback_pct": _flt(r, "squat_giveback_pct"),
                "windows": {
                    "5d":  {
                        "path_outcome":    r.get("path_outcome_5d"),
                        "peak_pct":        _flt(r, "peak_pct_5d"),
                        "final_vs_pivot":  _flt(r, "final_vs_pivot_5d"),
                        "hit_5pct_day":    r.get("hit_5pct_day_5d"),
                        "below_pivot_day": r.get("below_pivot_day_5d"),
                    },
                    "10d": {
                        "path_outcome":    r.get("path_outcome_10d"),
                        "peak_pct":        _flt(r, "peak_pct_10d"),
                        "final_vs_pivot":  _flt(r, "final_vs_pivot_10d"),
                        "hit_5pct_day":    r.get("hit_5pct_day_10d"),
                        "below_pivot_day": r.get("below_pivot_day_10d"),
                    },
                    "20d": {
                        "path_outcome":    r.get("path_outcome_20d"),
                        "peak_pct":        _flt(r, "peak_pct_20d"),
                        "final_vs_pivot":  _flt(r, "final_vs_pivot_20d"),
                        "hit_5pct_day":    r.get("hit_5pct_day_20d"),
                        "below_pivot_day": r.get("below_pivot_day_20d"),
                    },
                },
                "liq_cr": float(r["liq_cr_at_breakout"]) if r.get("liq_cr_at_breakout") is not None else None,
                "mcap_cr": float(r["mcap_cr_at_breakout"]) if r.get("mcap_cr_at_breakout") is not None else None,
                "sector": r.get("sector"),
                "industry": r.get("industry"),
                "base": {
                    "range_high": float(r["range_high"]),
                    "range_low":  float(r["range_low"]),
                    "width_pct":  float(r["range_width_pct"]),
                    "length_days": int(r["base_length_days"]),
                    "tightness":  r.get("tightness_grade"),
                    "quality":    int(r.get("base_quality") or 0),
                },
            })
        return jsonify({"events": events, "total": total, "limit": limit, "offset": offset, "window": window})
    except Exception as e:
        print(f"❌ breakout/events error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


def _flt(row, key):
    v = row.get(key)
    return float(v) if v is not None else None


# ──────────────────────────────────────────────────────────────────────────────
# /api/breakout/pivots/by-stock/<security_id> — overlay data for ValvoChart
# ──────────────────────────────────────────────────────────────────────────────

@breakout_bp.route("/api/breakout/pivots/by-stock/<security_id>", methods=["GET"])
@limiter.limit("120 per minute")
def pivots_by_stock(security_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT p.id, p.base_start_date, p.base_end_date,
                   p.range_high, p.range_low, p.range_width_pct,
                   p.base_length_days, p.tightness_grade, p.base_quality,
                   p.status, p.triggered_at, p.expired_at, p.invalidated_at,
                   e.breakout_date, e.entry_price,
                   e.path_outcome_5d, e.path_outcome_10d,
                   e.peak_pct_5d, e.peak_pct_10d,
                   e.squat_grade
              FROM breakout_pivots p
              LEFT JOIN breakout_events e ON e.pivot_id = p.id
             WHERE p.security_id = %s
             ORDER BY p.base_end_date DESC
             LIMIT 50
            """,
            (security_id,),
        )
        rows = cur.fetchall()
        pivots = []
        for r in rows:
            pivots.append({
                "id": r["id"],
                "base_start_date": str(r["base_start_date"]),
                "base_end_date":   str(r["base_end_date"]),
                "range_high": float(r["range_high"]),
                "range_low":  float(r["range_low"]),
                "range_width_pct": float(r["range_width_pct"]),
                "base_length_days": int(r["base_length_days"]),
                "tightness_grade": r.get("tightness_grade"),
                "base_quality":  int(r.get("base_quality") or 0),
                "status": r["status"],
                "triggered_at":   str(r["triggered_at"]) if r.get("triggered_at") else None,
                "expired_at":     str(r["expired_at"]) if r.get("expired_at") else None,
                "invalidated_at": str(r["invalidated_at"]) if r.get("invalidated_at") else None,
                "breakout_date":  str(r["breakout_date"]) if r.get("breakout_date") else None,
                "entry_price":    float(r["entry_price"]) if r.get("entry_price") is not None else None,
                "path_outcome_5d":  r.get("path_outcome_5d"),
                "path_outcome_10d": r.get("path_outcome_10d"),
                "peak_pct_5d":      _flt(r, "peak_pct_5d"),
                "peak_pct_10d":     _flt(r, "peak_pct_10d"),
                "squat_grade":      r.get("squat_grade"),
            })
        return jsonify({"pivots": pivots, "count": len(pivots)})
    except Exception as e:
        print(f"❌ breakout/pivots/by-stock error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ──────────────────────────────────────────────────────────────────────────────
# /api/breakout/squat-summary — simple single-window squat counts
# ──────────────────────────────────────────────────────────────────────────────

@breakout_bp.route("/api/breakout/squat-summary", methods=["GET"])
@limiter.limit("60 per minute")
def squat_summary():
    """Simple single-window squat counts for the dashboard tile.

    Query params:
      window — trading-day lookback (5 / 10 / 20). Pulls the last N distinct
               breakout dates from breakout_events and aggregates by squat
               grade. The dashboard's top window selector drives this.

    Returns:
      {window_days, total_breakouts, no_squat, weak_squat, strong_squat,
       squat_count, squat_ratio}
    """
    window = _parse_int("window", 10)
    if window not in (5, 10, 20):
        window = 10

    where = ["squat_grade IS NOT NULL"]
    params: list = []

    sector = request.args.get("sector")
    if sector:
        where.append("sector = %s"); params.append(sector)
    for k, op in (("liq_min", ">="), ("liq_max", "<")):
        v = _parse_float(k)
        if v is not None:
            where.append(f"liq_cr_at_breakout {op} %s"); params.append(v)
    for k, op in (("mcap_min", ">="), ("mcap_max", "<")):
        v = _parse_float(k)
        if v is not None:
            where.append(f"mcap_cr_at_breakout {op} %s"); params.append(v)

    extra_sql = (" AND " + " AND ".join(where[1:])) if len(where) > 1 else ""

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '10000'")
        sql = f"""
            WITH last_n AS (
              SELECT DISTINCT breakout_date
                FROM breakout_events
               ORDER BY breakout_date DESC
               LIMIT %s
            )
            SELECT
              COUNT(*)                                                        AS total,
              COUNT(*) FILTER (WHERE squat_grade = 'no_squat')                AS no_squat,
              COUNT(*) FILTER (WHERE squat_grade = 'weak_squat')              AS weak_squat,
              COUNT(*) FILTER (WHERE squat_grade = 'strong_squat')            AS strong_squat
              FROM breakout_events
             WHERE breakout_date IN (SELECT breakout_date FROM last_n)
               AND squat_grade IS NOT NULL
               {extra_sql}
        """
        cur.execute(sql, [window, *params])
        r = cur.fetchone() or {}

        total  = int(r.get("total") or 0)
        no     = int(r.get("no_squat") or 0)
        weak   = int(r.get("weak_squat") or 0)
        strong = int(r.get("strong_squat") or 0)
        squats = weak + strong
        ratio  = round(squats / total, 4) if total else None

        return jsonify({
            "window_days":     window,
            "total_breakouts": total,
            "no_squat":        no,
            "weak_squat":      weak,
            "strong_squat":    strong,
            "squat_count":     squats,
            "squat_ratio":     ratio,
        })
    except Exception as e:
        print(f"❌ breakout/squat-summary error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ──────────────────────────────────────────────────────────────────────────────
# /api/breakout/config — JSONB knob (admin-only PATCH)
# ──────────────────────────────────────────────────────────────────────────────

@breakout_bp.route("/api/breakout/config", methods=["GET"])
@limiter.limit("30 per minute")
def get_breakout_config():
    cfg = load_config()
    return jsonify({"config": cfg})


@breakout_bp.route("/api/breakout/config", methods=["PATCH"])
@limiter.limit("10 per minute")
def patch_breakout_config():
    user_email = (getattr(g, "user_email", None) or "").lower()
    if user_email not in ADMIN_EMAILS:
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "JSON object expected"}), 400
    cur_cfg = load_config()
    # Shallow merge — caller sends only the keys they want to change
    for k, v in body.items():
        cur_cfg[k] = v
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE breakout_config
               SET payload = %s::jsonb, updated_at = NOW(), updated_by = %s
             WHERE id = 1
            """,
            (json.dumps(cur_cfg, default=str), user_email or None),
        )
        conn.commit()
        return jsonify({"config": cur_cfg, "updated": True})
    except Exception as e:
        print(f"❌ breakout/config PATCH error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)
