"""
Breakout Success Intelligence — detection + outcome service

Pipeline (idempotent, called once per trading day after EOD close):

  1. detect_pivots_for_universe(as_of_date)
       Stage 2 filter → window scan N=3..25 → UPSERT breakout_pivots.
       Re-evaluates active pivots for invalidation/expiry.

  2. evaluate_pivots_for_breakouts(as_of_date)
       JOIN active pivots × today's candle. Where close > pivot * (1+buf):
       INSERT breakout_events with breadth + liq + mcap snapshots and the
       breakout-day intraday squat classification, then mark
       pivot.status = 'triggered'.

  3. update_outcomes(as_of_date)
       Path-aware outcome resolution. For each unresolved event with
       enough forward bars, walk T+1..T+window_n and write
       path_outcome_5d / path_outcome_10d / path_outcome_20d via
       services.breakout_path_outcomes.classify_path_outcome. Also
       populates gain_d1_pct, gain_d2_pct.

All SQL strings escape every literal '%' as '%%' (incl. inside comments) per
psycopg2 conventions. Reads breakout_config once per cron run via load_config().
"""
import json
import statistics
from datetime import date, timedelta
from typing import Optional

from database.database import get_db
from services.breakout_path_outcomes import classify_path_outcome


# ──────────────────────────────────────────────────────────────────────────────
# Config loader — single source of truth for tunable knobs
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    "tightness_thresholds": {"tight": 0.05, "normal": 0.08, "loose": 0.12},
    "min_base_length": 3,
    "max_base_length": 25,
    "short_base_max_width": 0.12,
    "pivot_buffer_pct": 0.005,
    "stage2_required": True,
    "stage2_dist_from_52w_high": 0.25,
    "stage2_above_52w_low": 0.30,
    "pivot_max_age_days": 30,
    "invalidation_pct": 0.02,
    "gap_extended_pct": 0.03,
    "mcap_tiers": {
        "largecap":  {"min": 80000, "max": None},
        "midcap":    {"min": 12000, "max": 80000},
        "smallcap":  {"min": 5000,  "max": 12000},
        "microcap":  {"min": 0,     "max": 5000},
    },
    "min_liq_for_eligibility": 10.0,
    "default_dashboard_min_liq": 0.5,
    # Breakout-day intraday squat classification (see _classify_squat).
    "squat_no_squat_giveback_max":      0.02,
    "squat_weak_giveback_min":          0.03,
    "squat_weak_giveback_max":          0.05,
    "squat_weak_close_min_above_pivot": 0.03,
    # Path-outcome classifier thresholds (see breakout_path_outcomes.classify_path_outcome).
    "path_outcome_d1_spike_pct":             0.03,
    "path_outcome_pre_5pct_window_days":     3,
    "path_outcome_target_5pct":              0.05,
    "path_outcome_target_7pct":              0.07,
    "path_outcome_target_10pct":             0.10,
    "path_outcome_success_5to7_final_min":   0.03,
    "path_outcome_success_7to10_final_min":  0.05,
    "path_outcome_success_strong_final_min": 0.07,
}


def _path_outcome_cfg(cfg: dict) -> dict:
    """Extract the eight path-outcome threshold knobs from the global config
    and remap them to the keys expected by classify_path_outcome()."""
    return {
        "d1_spike_pct":             cfg.get("path_outcome_d1_spike_pct", 0.03),
        "pre_5pct_window_days":     cfg.get("path_outcome_pre_5pct_window_days", 3),
        "target_5pct":              cfg.get("path_outcome_target_5pct", 0.05),
        "target_7pct":              cfg.get("path_outcome_target_7pct", 0.07),
        "target_10pct":             cfg.get("path_outcome_target_10pct", 0.10),
        "success_5to7_final_min":   cfg.get("path_outcome_success_5to7_final_min", 0.03),
        "success_7to10_final_min":  cfg.get("path_outcome_success_7to10_final_min", 0.05),
        "success_strong_final_min": cfg.get("path_outcome_success_strong_final_min", 0.07),
    }


def load_config() -> dict:
    """Read breakout_config.payload, merge over defaults so missing keys remain
    safe. Cached only for the duration of one cron invocation by the caller."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT payload FROM breakout_config WHERE id = 1")
        row = cur.fetchone()
        loaded = (row.get("payload") if row else None) or {}
        # shallow merge over defaults
        cfg = dict(_DEFAULT_CONFIG)
        for k, v in loaded.items():
            cfg[k] = v
        return cfg
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# 1. Pivot detection
# ──────────────────────────────────────────────────────────────────────────────

def _stage2_universe(conn, as_of_date: date, cfg: dict) -> list:
    """Return security_ids passing the Stage 2 trend template as of `as_of_date`.

    Uses stock_daily_summary for cheap precomputed ma50/ma200/52w high/low.
    Falls back gracefully when summary is missing — those stocks are simply
    skipped (will be picked up the next day after the daily summary refresh).
    """
    cur = conn.cursor()
    cur.execute("SET LOCAL statement_timeout = '60000'")
    dist_from_high = float(cfg.get("stage2_dist_from_52w_high", 0.25))
    above_low = float(cfg.get("stage2_above_52w_low", 0.30))
    min_liq = float(cfg.get("min_liq_for_eligibility", 0.3))
    cur.execute(
        """
        SELECT s.security_id
          FROM stock_daily_summary s
          JOIN stock_universe u ON u.security_id = s.security_id
         WHERE s.prev_close > 0
           AND s.ma50 > 0 AND s.ma200 > 0
           AND s.high_52w > 0 AND s.low_52w > 0
           AND s.prev_close > s.ma50
           AND s.ma50 > s.ma200
           AND s.prev_close >= (1.0 - %s) * s.high_52w
           AND s.prev_close >= (1.0 + %s) * s.low_52w
           AND COALESCE(s.is_etf, false) = false
           AND COALESCE(u.is_etf, false) = false
           AND COALESCE(u.is_active, true) = true
           AND COALESCE(s.liq_cr, 0) >= %s
        """,
        (dist_from_high, above_low, min_liq),
    )
    return [r["security_id"] for r in cur.fetchall()]


def _load_recent_candles(conn, sids: list, as_of_date: date, days: int = 60) -> dict:
    """Pull last `days` candles per security_id, returned as
    {security_id: [{date, open, high, low, close, volume}, ...]} sorted ASC."""
    if not sids:
        return {}
    cur = conn.cursor()
    cur.execute("SET LOCAL statement_timeout = '120000'")
    start = as_of_date - timedelta(days=days)
    cur.execute(
        """
        SELECT security_id, date, open, high, low, close, volume
          FROM candles_daily
         WHERE security_id = ANY(%s::text[])
           AND date BETWEEN %s AND %s
           AND volume > 0
         ORDER BY security_id, date
        """,
        (sids, start, as_of_date),
    )
    out: dict = {}
    for r in cur.fetchall():
        out.setdefault(r["security_id"], []).append({
            "date": r["date"],
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": int(r["volume"] or 0),
        })
    return out


def _grade_tightness(width_pct: float, n: int, cfg: dict) -> Optional[str]:
    """Map range width to tight / normal / loose / None (reject)."""
    th = cfg["tightness_thresholds"]
    short_max = float(cfg.get("short_base_max_width", 0.07))
    if n < 7 and width_pct > short_max:
        return None
    if width_pct <= th["tight"]:
        return "tight"
    if width_pct <= th["normal"]:
        return "normal"
    if width_pct <= th["loose"]:
        return "loose"
    return None


def _ma50(candles: list) -> float:
    """50-day simple moving average of closes from the candles list.
    Returns 0.0 if fewer than 50 candles are available."""
    if len(candles) < 50:
        return 0.0
    return statistics.mean(c["close"] for c in candles[-50:])


def _quality_score(width_pct: float, n: int, last_close: float,
                   high_52w: float, ma50: float, cfg: dict) -> int:
    """0–100 base quality.

    Components:
      45 pts tightness — narrower range scores higher; rejected past `loose` threshold
      20 pts length    — triangular peak at N=11 (sweet-spot 7–15 days)
      15 pts trend     — distance above MA50, monotonic up to +20%, then clipped
      20 pts prox      — close vs 52w high, rescaled to the Stage-2 range [0.75, 1.0]
    """
    loose = float(cfg.get("tightness_thresholds", {}).get("loose", 0.12))
    tight = max(0.0, 1.0 - (width_pct / max(loose, 1e-9)))
    length = max(0.0, 1.0 - abs(n - 11) / 11.0)
    trend = (
        min(1.0, max(0.0, (last_close - ma50) / max(ma50 * 0.20, 1e-9)))
        if ma50 > 0 else 0.0
    )
    prox = (
        min(1.0, max(0.0, (last_close / high_52w - 0.75) / 0.25))
        if high_52w > 0 else 0.0
    )
    score = 45 * tight + 20 * length + 15 * trend + 20 * prox
    return int(round(max(0.0, min(100.0, score))))


def _find_best_base(candles: list, as_of_date: date, cfg: dict, high_52w: float) -> Optional[dict]:
    """Scan window lengths N=min..max ending on `as_of_date`. Return the
    HIGHEST-quality base that satisfies the tightness rules. Returns None
    if the stock has no qualifying consolidation (runaway uptrend = no pivot)."""
    n_min = int(cfg.get("min_base_length", 3))
    n_max = int(cfg.get("max_base_length", 25))

    if not candles or candles[-1]["date"] != as_of_date:
        return None
    if len(candles) < n_min:
        return None

    ma50 = _ma50(candles)
    last_close = candles[-1]["close"]

    best: Optional[dict] = None
    best_score = -1
    for n in range(n_min, min(n_max, len(candles)) + 1):
        window = candles[-n:]
        range_high = max(c["high"] for c in window)
        range_low = min(c["low"] for c in window)
        if range_low <= 0:
            continue
        width = (range_high - range_low) / range_low
        grade = _grade_tightness(width, n, cfg)
        if grade is None:
            continue
        # Pivot price = max(range_high, max_close * 1.001) — protects against single-tick wicks
        max_close = max(c["close"] for c in window)
        pivot = max(range_high, max_close * 1.001)
        score = _quality_score(width, n, last_close, high_52w, ma50, cfg)
        if score > best_score:
            best_score = score
            best = {
                "base_start_date": window[0]["date"],
                "base_end_date": window[-1]["date"],
                "range_high": pivot,
                "range_low": range_low,
                "range_width_pct": round(width, 4),
                "base_length_days": n,
                "avg_volume_base": int(statistics.mean(c["volume"] for c in window)),
                "tightness_grade": grade,
                "base_quality": score,
            }
    return best


def _refresh_pivot_lifecycle(conn, as_of_date: date, cfg: dict) -> dict:
    """Mark pivots invalidated (close < range_low - buffer) or expired (too old).
    Runs as set-based SQL for efficiency. Returns counts."""
    cur = conn.cursor()
    cur.execute("SET LOCAL statement_timeout = '60000'")
    invalidation = float(cfg.get("invalidation_pct", 0.02))
    max_age = int(cfg.get("pivot_max_age_days", 30))

    # Invalidate: stock closed too far below the base low (broke down)
    cur.execute(
        """
        UPDATE breakout_pivots p
           SET status = 'invalidated',
               invalidated_at = %s,
               invalidated_reason = 'close_below_low',
               updated_at = NOW()
          FROM (
              SELECT DISTINCT ON (security_id) security_id, close
                FROM candles_daily
               WHERE date = %s
               ORDER BY security_id, date DESC
          ) c
         WHERE p.security_id = c.security_id
           AND p.status IN ('forming', 'active')
           AND c.close < p.range_low * (1.0 - %s)
        """,
        (as_of_date, as_of_date, invalidation),
    )
    invalidated = cur.rowcount or 0

    # Expire: pivot older than max_age days without trigger
    cur.execute(
        """
        UPDATE breakout_pivots
           SET status = 'expired',
               expired_at = %s,
               updated_at = NOW()
         WHERE status IN ('forming', 'active')
           AND base_end_date < %s - (%s * INTERVAL '1 day')
        """,
        (as_of_date, as_of_date, max_age),
    )
    expired = cur.rowcount or 0

    conn.commit()
    return {"invalidated": invalidated, "expired": expired}


def detect_pivots_for_universe(as_of_date: date) -> dict:
    """Stage 2 filter → window scan → UPSERT into breakout_pivots.
    Lifecycle pass marks invalidated/expired pivots. Returns counts dict."""
    cfg = load_config()
    conn = get_db()
    try:
        # 1. Stage 2 filter
        sids = _stage2_universe(conn, as_of_date, cfg)
        if not sids:
            return {"stage2": 0, "pivots_inserted": 0, "invalidated": 0, "expired": 0}

        # 2. Pull recent candles for stage 2 stocks only
        candles_by_sid = _load_recent_candles(conn, sids, as_of_date, days=60)

        # 3. Per-stock 52w high lookup (one query)
        cur = conn.cursor()
        cur.execute(
            "SELECT security_id, high_52w FROM stock_daily_summary WHERE security_id = ANY(%s::text[])",
            (sids,),
        )
        h52 = {r["security_id"]: float(r["high_52w"] or 0) for r in cur.fetchall()}

        # 4. Find best base per stock
        rows_to_upsert = []
        for sid, candles in candles_by_sid.items():
            if not candles:
                continue
            pivot = _find_best_base(candles, as_of_date, cfg, h52.get(sid, 0.0))
            if pivot:
                rows_to_upsert.append((sid, pivot))

        # 5. Bulk UPSERT — one round-trip per row is fine at ~700 rows max
        inserted = 0
        for sid, p in rows_to_upsert:
            cur.execute(
                """
                INSERT INTO breakout_pivots (
                    security_id, base_start_date, base_end_date,
                    range_high, range_low, range_width_pct, base_length_days,
                    avg_volume_base, tightness_grade, base_quality,
                    status, detected_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          'active', NOW(), NOW())
                ON CONFLICT (security_id, base_end_date) DO UPDATE SET
                    range_high       = EXCLUDED.range_high,
                    range_low        = EXCLUDED.range_low,
                    range_width_pct  = EXCLUDED.range_width_pct,
                    base_length_days = EXCLUDED.base_length_days,
                    avg_volume_base  = EXCLUDED.avg_volume_base,
                    tightness_grade  = EXCLUDED.tightness_grade,
                    base_quality     = EXCLUDED.base_quality,
                    status           = CASE
                                          WHEN breakout_pivots.status = 'triggered' THEN 'triggered'
                                          ELSE 'active'
                                       END,
                    updated_at       = NOW()
                """,
                (
                    sid, p["base_start_date"], p["base_end_date"],
                    p["range_high"], p["range_low"], p["range_width_pct"],
                    p["base_length_days"], p["avg_volume_base"],
                    p["tightness_grade"], p["base_quality"],
                ),
            )
            inserted += 1
        conn.commit()

        # 6. Invalidation + expiry pass
        life = _refresh_pivot_lifecycle(conn, as_of_date, cfg)

        return {
            "stage2": len(sids),
            "pivots_inserted": inserted,
            "invalidated": life["invalidated"],
            "expired": life["expired"],
        }
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# 2. Breakout event detection
# ──────────────────────────────────────────────────────────────────────────────

def _classify_squat(high: float, close: float, pivot: float, cfg: dict):
    """Classify breakout-day intraday squat pattern.

    Three buckets, evaluated in order:
      no_squat:     giveback ≤ no_squat_max — closed near the high
      weak_squat:   weak_min ≤ giveback ≤ weak_max AND close ≥ X% above pivot —
                    gave back a controlled amount but close is still strong
      strong_squat: catch-all — any other meaningful giveback

    `giveback = (high - close) / high` (% drop from intraday high).
    Returns (grade_text, giveback_pct_rounded), or (None, None) on bad input.
    """
    if pivot <= 0 or high <= 0 or close <= 0:
        return (None, None)

    giveback = (high - close) / high
    close_above_pivot = (close - pivot) / pivot

    no_max         = float(cfg.get("squat_no_squat_giveback_max",      0.02))
    weak_min       = float(cfg.get("squat_weak_giveback_min",          0.03))
    weak_max       = float(cfg.get("squat_weak_giveback_max",          0.05))
    weak_close_min = float(cfg.get("squat_weak_close_min_above_pivot", 0.03))

    if giveback <= no_max:
        grade = "no_squat"
    elif weak_min <= giveback <= weak_max and close_above_pivot >= weak_close_min:
        grade = "weak_squat"
    else:
        grade = "strong_squat"

    return (grade, round(giveback, 4))


def evaluate_pivots_for_breakouts(as_of_date: date) -> dict:
    """Find active pivots whose stock closed > pivot * (1 + buffer) on `as_of_date`.
    Snapshot liquidity, market cap, sector, breadth, then INSERT breakout_events
    and mark pivot.status = 'triggered'."""
    cfg = load_config()
    buffer = float(cfg.get("pivot_buffer_pct", 0.005))
    gap_extended_pct = float(cfg.get("gap_extended_pct", 0.03))

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '120000'")

        # Pull breadth snapshot once
        cur.execute(
            """
            SELECT total_stocks, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                   new_highs, new_lows, advance_count, decline_count,
                   thrust, momentum_20pc
              FROM breadth_daily_history WHERE date = %s
            """,
            (as_of_date,),
        )
        breadth_row = cur.fetchone()
        breadth_snap = dict(breadth_row) if breadth_row else {}

        # Find candidate breakouts: active pivots × today's candle where close > pivot+buffer
        cur.execute(
            """
            WITH active AS (
                SELECT id AS pivot_id, security_id, range_high, range_low,
                       base_end_date, range_width_pct
                  FROM breakout_pivots
                 WHERE status = 'active'
            ),
            today AS (
                SELECT DISTINCT ON (security_id) security_id, date,
                       open, high, low, close, volume
                  FROM candles_daily
                 WHERE date = %s
                 ORDER BY security_id, date DESC
            )
            SELECT a.pivot_id, a.security_id, a.range_high, a.range_low,
                   t.open, t.high, t.low, t.close, t.volume
              FROM active a
              JOIN today  t ON t.security_id = a.security_id
             WHERE t.close > a.range_high * (1.0 + %s)
               AND t.high  > a.range_high
            """,
            (as_of_date, buffer),
        )
        candidates = cur.fetchall()
        if not candidates:
            return {"candidates": 0, "events_inserted": 0}

        sids = [c["security_id"] for c in candidates]

        # Liquidity, market cap, sector, industry snapshots
        cur.execute(
            """
            SELECT su.security_id,
                   COALESCE(su.valvo_sector, su.sector) AS sector,
                   su.industry,
                   su.shares_outstanding,
                   sds.liq_cr
              FROM stock_universe su
         LEFT JOIN stock_daily_summary sds ON sds.security_id = su.security_id
             WHERE su.security_id = ANY(%s::text[])
            """,
            (sids,),
        )
        meta = {r["security_id"]: r for r in cur.fetchall()}

        events_inserted = 0
        for c in candidates:
            sid = c["security_id"]
            close_t = float(c["close"])
            open_t = float(c["open"])
            high_t = float(c["high"])
            volume_t = int(c["volume"] or 0)
            pivot = float(c["range_high"])

            # Gap-up handling
            gap_up_pct = (open_t - pivot) / pivot if open_t > pivot else 0.0
            gap_extended = gap_up_pct > gap_extended_pct

            # Breakout-day intraday squat classification
            squat_grade, squat_giveback = _classify_squat(high_t, close_t, pivot, cfg)

            # Realistic entry: max(open T+1, pivot * 1.005). T+1 isn't available
            # yet for a same-day insert — use pivot * 1.005 conservatively;
            # update_outcomes() may re-anchor entry once T+1 lands.
            entry = pivot * 1.005

            # Liquidity / market cap snapshot
            m = meta.get(sid, {})
            shares = m.get("shares_outstanding")
            mcap = (float(shares) * close_t / 1e7) if shares else None
            liq = float(m.get("liq_cr") or 0) if m.get("liq_cr") is not None else None

            cur.execute(
                """
                INSERT INTO breakout_events (
                    pivot_id, security_id, breakout_date, breakout_close, pivot_price,
                    entry_price, volume,
                    gap_up_pct, gap_extended,
                    liq_cr_at_breakout, mcap_cr_at_breakout, sector, industry,
                    squat_grade, squat_giveback_pct,
                    breadth_snapshot, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (pivot_id, breakout_date) DO NOTHING
                """,
                (
                    c["pivot_id"], sid, as_of_date, close_t, pivot, entry, volume_t,
                    round(gap_up_pct, 4), gap_extended,
                    liq, mcap, m.get("sector"), m.get("industry"),
                    squat_grade, squat_giveback,
                    json.dumps(breadth_snap, default=str),
                ),
            )
            if cur.rowcount:
                events_inserted += 1
                # Mark pivot triggered
                cur.execute(
                    """
                    UPDATE breakout_pivots
                       SET status = 'triggered', triggered_at = %s, updated_at = NOW()
                     WHERE id = %s AND status != 'triggered'
                    """,
                    (as_of_date, c["pivot_id"]),
                )
        conn.commit()
        return {"candidates": len(candidates), "events_inserted": events_inserted}
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# 3. Outcome resolution — path-aware buckets (5d / 10d) + day-1/day-2 gains
# ──────────────────────────────────────────────────────────────────────────────

def update_outcomes(as_of_date: date) -> dict:
    """For unresolved events, walk forward bars and write path-aware outcome
    columns + day-1/day-2 % gains. Idempotent — only fills NULLs.

    Resolves an event if it's missing path_outcome_5d / path_outcome_10d /
    path_outcome_20d / gain_d1_pct / gain_d2_pct AND the breakout_date is
    recent enough that we may have new forward bars to evaluate.
    """
    cfg = load_config()
    path_cfg = _path_outcome_cfg(cfg)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '300000'")

        # Look back 60 days — covers the full 20d window plus margin for
        # weekends/holidays.
        cur.execute(
            """
            SELECT id, security_id, breakout_date, entry_price, pivot_price,
                   path_outcome_5d, path_outcome_10d, path_outcome_20d,
                   gain_d1_pct, gain_d2_pct
              FROM breakout_events
             WHERE (path_outcome_5d  IS NULL
                    OR path_outcome_10d IS NULL
                    OR path_outcome_20d IS NULL
                    OR gain_d1_pct IS NULL
                    OR gain_d2_pct IS NULL)
               AND breakout_date BETWEEN %s AND %s
            """,
            (as_of_date - timedelta(days=60), as_of_date - timedelta(days=1)),
        )
        events = cur.fetchall()
        if not events:
            return {"updated": 0}

        sids = list({e["security_id"] for e in events})
        # Pull forward candles for all of these stocks in one shot
        cur.execute(
            """
            SELECT security_id, date, open, high, low, close
              FROM candles_daily
             WHERE security_id = ANY(%s::text[])
               AND date BETWEEN %s AND %s
               AND volume > 0
             ORDER BY security_id, date
            """,
            (sids, min(e["breakout_date"] for e in events) - timedelta(days=1), as_of_date),
        )
        cand_by_sid: dict = {}
        for r in cur.fetchall():
            cand_by_sid.setdefault(r["security_id"], []).append(r)

        updated = 0
        for e in events:
            cands = cand_by_sid.get(e["security_id"], [])
            # Forward candles strictly AFTER breakout_date
            fwd = [c for c in cands if c["date"] > e["breakout_date"]]
            if not fwd:
                continue

            # Realistic entry: max(open[T+1], pivot * 1.005)
            pivot_price = float(e["pivot_price"])
            entry = max(float(fwd[0]["open"]), pivot_price * 1.005)

            updates = {}

            # Day+1 / Day+2 % gains relative to pivot (so they're directly
            # comparable to the path-outcome thresholds).
            if e.get("gain_d1_pct") is None and len(fwd) >= 1:
                d1_close = float(fwd[0]["close"])
                updates["gain_d1_pct"] = round((d1_close - pivot_price) / pivot_price, 4)
            if e.get("gain_d2_pct") is None and len(fwd) >= 2:
                d2_close = float(fwd[1]["close"])
                updates["gain_d2_pct"] = round((d2_close - pivot_price) / pivot_price, 4)

            # Path-aware 5d / 10d / 20d buckets.
            for window_name, n in (("5d", 5), ("10d", 10), ("20d", 20)):
                if e.get(f"path_outcome_{window_name}") is not None:
                    continue
                if len(fwd) < n:
                    continue
                path = classify_path_outcome(pivot_price, fwd, n, cfg=path_cfg)
                if path is None:
                    continue
                updates[f"path_outcome_{window_name}"]   = path["bucket"]
                updates[f"peak_pct_{window_name}"]       = path["peak_pct"]
                updates[f"final_vs_pivot_{window_name}"] = path["final_pct"]
                updates[f"hit_5pct_day_{window_name}"]   = path["hit_5pct_day"]
                updates[f"below_pivot_day_{window_name}"] = path["hit_below_pivot_day"]

            if not updates:
                continue
            # Re-anchor entry_price to max(open T+1, pivot*1.005) once T+1 is
            # known. GREATEST() ensures we never lower it.
            cols = ", ".join(f"{k} = %s" for k in updates.keys())
            cur.execute(
                f"UPDATE breakout_events SET {cols}, "
                "entry_price = GREATEST(entry_price, %s) WHERE id = %s",
                list(updates.values()) + [entry, e["id"]],
            )
            updated += 1

        conn.commit()
        return {"updated": updated}
    finally:
        conn.close()

