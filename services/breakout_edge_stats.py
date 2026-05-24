"""
Breakout Edge Stats — dashboard math over the path-aware outcome columns.
=================================================================================
Reads breakout_events.path_outcome_{5d,10d}, peak_pct_{5d,10d},
final_vs_pivot_{5d,10d}, below_pivot_day_{5d,10d}; never UPDATEs.

Returns the edge-stats payload for /api/breakout/edge-stats:

  - path_outcomes  : per-window bucket counts + rollup (the headline).
                     "Of the breakouts we triggered, how many actually held?"
  - summary        : Wilson-95 CI on success rate, R-multiples (O'Neil 7%%
                     structural stop), expectancy, profit factor, path-honesty.
  - r_distribution : histogram of final_vs_pivot expressed as R-multiples.
  - regime_stratification : same buckets, sliced by the breadth regime in
                     force on each breakout's bar.

The legacy outcome_Xd / max_gain_Xd / max_dd_Xd / final_Xd / ftd_grade /
vol_confirmed columns no longer exist on the table; ftd_lift / vol_lift are
returned as empty arrays (the frontend cards already handle that).

Conventions:
  - All SQL uses %% to escape literal % per psycopg2 (incl. inside comments).
  - Every query starts with SET LOCAL statement_timeout = ... .
  - All public functions are pure read; no UPDATE / INSERT.
  - Returns plain dicts safe for json.dumps(default=str).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional

from database.database import get_db


# ──────────────────────────────────────────────────────────────────────────────
# Wilson 95% confidence interval (binomial proportion)
# ──────────────────────────────────────────────────────────────────────────────

def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score interval for a binomial proportion. Brown/Cai/DasGupta
    (2001) recommend Wilson over Wald for n<40 AND for p near 0/1.

    Minimum-N at p≈0.5 for ±10pp width @ 95%%: N≥96. Below that, the CI
    bar in the UI is wider than the point estimate — which is the truth."""
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


# ──────────────────────────────────────────────────────────────────────────────
# Filter clause helpers (mirrors the existing aggregate_custom convention)
# ──────────────────────────────────────────────────────────────────────────────

def _where_clauses(
    liq_min: Optional[float], liq_max: Optional[float],
    mcap_min: Optional[float], mcap_max: Optional[float],
    sector: Optional[str],
) -> tuple:
    """Returns (extra_sql, params_list). The extra_sql starts with ' AND '."""
    clauses = []
    params: list = []
    if liq_min is not None:
        clauses.append("liq_cr_at_breakout >= %s"); params.append(liq_min)
    if liq_max is not None:
        clauses.append("liq_cr_at_breakout <  %s"); params.append(liq_max)
    if mcap_min is not None:
        clauses.append("mcap_cr_at_breakout >= %s"); params.append(mcap_min)
    if mcap_max is not None:
        clauses.append("mcap_cr_at_breakout <  %s"); params.append(mcap_max)
    if sector:
        clauses.append("sector = %s"); params.append(sector)
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""
    return extra, params


def _last_n_trading_dates(conn, as_of_date: date, n: int) -> list:
    """Distinct trading days ≤ as_of_date, most-recent N. Works the same way
    the existing _last_n_trading_dates helper does — duplicated here to keep
    this module self-contained and importable without circular deps."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT date FROM candles_daily
         WHERE date <= %s AND date >= %s
         ORDER BY date DESC LIMIT %s
        """,
        (as_of_date, as_of_date - timedelta(days=n * 3 + 30), n),
    )
    return [r["date"] for r in cur.fetchall()]


# ──────────────────────────────────────────────────────────────────────────────
# Regime stratification — the highest-leverage chart on the page
# ──────────────────────────────────────────────────────────────────────────────

def _classify_regime(thrust: bool, momentum_20pc: int, new_highs: int,
                     new_lows: int, pct_above_ema20: float) -> str:
    """Six-bucket breadth-tape taxonomy. The thrust flag fires more often than
    you'd expect on breakout days (selection bias — breakouts cluster on strong-
    advance days), so BREADTH_THRUST also requires meaningful momentum to stay
    rare. CONFIRMED_RALLY fills the "healthy uptrend" middle. Thresholds tuned
    so all six buckets are populated in a typical 90-day lookback."""
    if thrust and momentum_20pc >= 25 and new_highs > new_lows:
        return "BREADTH_THRUST"
    if pct_above_ema20 >= 55 and new_highs > new_lows:
        return "CONFIRMED_RALLY"
    if pct_above_ema20 >= 45 and new_highs >= new_lows:
        return "RALLY_ATTEMPT"
    if pct_above_ema20 < 25 or (new_lows > new_highs * 2 and pct_above_ema20 < 35):
        return "CORRECTION"
    if pct_above_ema20 < 40 or new_lows > new_highs:
        return "UNDER_PRESSURE"
    return "NEUTRAL"


REGIME_ORDER = [
    "BREADTH_THRUST",
    "CONFIRMED_RALLY",
    "RALLY_ATTEMPT",
    "NEUTRAL",
    "UNDER_PRESSURE",
    "CORRECTION",
]


# ──────────────────────────────────────────────────────────────────────────────
# Path-aware outcome breakdown — the headline metric.
# "Of the breakouts we triggered, how many actually held vs failed?"
# ──────────────────────────────────────────────────────────────────────────────

PATH_BUCKET_ORDER = [
    "failed_d1_reversal",
    "failed_pre_5pct_drop",
    "failed_no_5pct_in_window",
    "moderate_reversed",
    "success_7_to_10",
    "success_strong_gt_10",
]
FAILED_PATH_BUCKETS = {
    "failed_d1_reversal", "failed_pre_5pct_drop", "failed_no_5pct_in_window"
}
MODERATE_PATH_BUCKETS = {"moderate_reversed"}
SUCCESS_PATH_BUCKETS = {"success_7_to_10", "success_strong_gt_10"}


def _path_outcome_breakdown(cur, from_date, to_date, window_n: int,
                            extra: str, fparams: list) -> dict:
    """Aggregate path-aware outcome buckets for a single window (5d or 10d).

    Returns a dict shaped for the frontend headline section:
      {
        n_resolved, by_bucket: {bucket: {n, pct, avg_peak_pct, avg_final_pct}},
        rollup: {failed_n, failed_pct, moderate_n, moderate_pct,
                 success_n, success_pct, avg_move_pct_success}
      }
    """
    if window_n not in (5, 10):
        raise ValueError(f"path-outcome window must be 5 or 10, got {window_n}")
    suffix = f"{window_n}d"
    path_col = f"path_outcome_{suffix}"
    peak_col = f"peak_pct_{suffix}"
    final_col = f"final_vs_pivot_{suffix}"

    sql = f"""
        SELECT {path_col} AS bucket,
               COUNT(*)               AS n,
               AVG({peak_col})        AS avg_peak,
               AVG({final_col})       AS avg_final
          FROM breakout_events
         WHERE breakout_date BETWEEN %s AND %s
           AND {path_col} IS NOT NULL
           {extra}
         GROUP BY {path_col}
    """
    cur.execute(sql, [from_date, to_date, *fparams])
    rows = cur.fetchall()
    by_bucket_raw = {r["bucket"]: r for r in rows if r.get("bucket")}
    n_total = sum(int(r.get("n") or 0) for r in rows)

    by_bucket = {}
    for bucket in PATH_BUCKET_ORDER:
        r = by_bucket_raw.get(bucket)
        if not r:
            by_bucket[bucket] = {"n": 0, "pct": None, "avg_peak_pct": None, "avg_final_pct": None}
            continue
        n = int(r.get("n") or 0)
        by_bucket[bucket] = {
            "n": n,
            "pct": round(100.0 * n / n_total, 2) if n_total else None,
            "avg_peak_pct": round(float(r.get("avg_peak") or 0) * 100, 2),
            "avg_final_pct": round(float(r.get("avg_final") or 0) * 100, 2),
        }

    failed_n = sum(by_bucket[b]["n"] for b in FAILED_PATH_BUCKETS)
    moderate_n = sum(by_bucket[b]["n"] for b in MODERATE_PATH_BUCKETS)
    success_n = sum(by_bucket[b]["n"] for b in SUCCESS_PATH_BUCKETS)

    # Average peak in successful breakouts ("what's the average move in winners")
    sql_success_avg = f"""
        SELECT AVG({peak_col})  AS avg_peak,
               AVG({final_col}) AS avg_final
          FROM breakout_events
         WHERE breakout_date BETWEEN %s AND %s
           AND {path_col} IN ('success_7_to_10', 'success_strong_gt_10')
           {extra}
    """
    cur.execute(sql_success_avg, [from_date, to_date, *fparams])
    srow = cur.fetchone() or {}
    avg_move_success = float(srow.get("avg_peak") or 0) * 100
    avg_final_success = float(srow.get("avg_final") or 0) * 100

    return {
        "n_resolved": n_total,
        "by_bucket": by_bucket,
        "rollup": {
            "failed_n": failed_n,
            "failed_pct": round(100.0 * failed_n / n_total, 2) if n_total else None,
            "moderate_n": moderate_n,
            "moderate_pct": round(100.0 * moderate_n / n_total, 2) if n_total else None,
            "success_n": success_n,
            "success_pct": round(100.0 * success_n / n_total, 2) if n_total else None,
            "avg_peak_pct_success": round(avg_move_success, 2) if success_n else None,
            "avg_final_pct_success": round(avg_final_success, 2) if success_n else None,
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

def edge_stats(
    window_days: int,
    as_of_date: Optional[date] = None,
    lookback_days: int = 90,
    liq_min: Optional[float] = None,
    liq_max: Optional[float] = None,
    mcap_min: Optional[float] = None,
    mcap_max: Optional[float] = None,
    sector: Optional[str] = None,
) -> dict:
    """Returns a dict with the v2 edge-quality payload for the dashboard.

    `lookback_days` is the calendar-day window for "how big a sample am I
    looking at?" — defaulting to 90 days gives a sample size of 250-1000
    breakouts under typical conditions, which is enough for stable Wilson
    CIs and meaningful regime stratification (the existing dashboard's
    rolling-5-day window is too small for either).
    """
    # The path-outcome schema only resolves 5d and 10d windows; force valid choice.
    if window_days not in (5, 10):
        window_days = 10
    if as_of_date is None:
        as_of_date = date.today()

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '60000'")

        from_date = as_of_date - timedelta(days=lookback_days)
        # Forward-cap so events have enough bars for the path-outcome window
        to_date = as_of_date - timedelta(days=window_days + 2)

        path_col = f"path_outcome_{window_days}d"
        peak_col = f"peak_pct_{window_days}d"
        final_col = f"final_vs_pivot_{window_days}d"
        below_col = f"below_pivot_day_{window_days}d"

        extra, fparams = _where_clauses(liq_min, liq_max, mcap_min, mcap_max, sector)
        ONEIL_STOP = 0.07  # R-multiple anchor (O'Neil structural stop)

        # ── 1. Top-line summary, derived entirely from path_outcome ───────
        sql_summary = f"""
            WITH ev AS (
              SELECT *
                FROM breakout_events
               WHERE breakout_date BETWEEN %s AND %s
                 AND {path_col} IS NOT NULL
                 {extra}
            )
            SELECT COUNT(*)                                                        AS resolved,
                   COUNT(*) FILTER (WHERE {path_col} IN ('success_strong_gt_10','success_7_to_10')) AS wins,
                   COUNT(*) FILTER (WHERE {path_col} = 'success_strong_gt_10')     AS wins_strong,
                   COUNT(*) FILTER (WHERE {path_col} = 'success_7_to_10')          AS wins_good,
                   COUNT(*) FILTER (WHERE {path_col} = 'moderate_reversed')        AS modest,
                   COUNT(*) FILTER (WHERE {path_col} = 'failed_no_5pct_in_window') AS failed,
                   COUNT(*) FILTER (WHERE {path_col} = 'failed_d1_reversal')       AS failed_d1,
                   COUNT(*) FILTER (WHERE {path_col} = 'failed_pre_5pct_drop')     AS failed_pre,
                   AVG({final_col})                                                AS avg_final,
                   AVG({peak_col})                                                 AS avg_peak,
                   STDDEV_SAMP({final_col})                                        AS std_final,
                   AVG({final_col}) FILTER (WHERE {final_col} > 0)                 AS avg_winner_final,
                   AVG({final_col}) FILTER (WHERE {final_col} < 0)                 AS avg_loser_final,
                   COUNT(*) FILTER (WHERE {final_col} > 0)                         AS n_winners,
                   COUNT(*) FILTER (WHERE {final_col} < 0)                         AS n_losers,
                   COUNT(*) FILTER (
                     WHERE {path_col} IN ('success_strong_gt_10','success_7_to_10','moderate_reversed')
                       AND {below_col} IS NOT NULL
                   )                                                               AS n_path_dishonest_wins
              FROM ev
        """
        cur.execute(sql_summary, [from_date, to_date, *fparams])
        s = cur.fetchone() or {}

        resolved = int(s.get("resolved") or 0)
        wins = int(s.get("wins") or 0)
        n_winners = int(s.get("n_winners") or 0)
        n_losers = int(s.get("n_losers") or 0)
        avg_winner_final = float(s.get("avg_winner_final") or 0)
        avg_loser_final = float(s.get("avg_loser_final") or 0)

        success_rate = (wins / resolved) if resolved > 0 else 0.0
        ci_lo, ci_hi = wilson_ci(wins, resolved)

        avg_R = float(s.get("avg_final") or 0) / ONEIL_STOP
        sigma_R = float(s.get("std_final") or 0) / ONEIL_STOP

        gross_winner = avg_winner_final * n_winners if n_winners else 0.0
        gross_loser = abs(avg_loser_final) * n_losers if n_losers else 0.0
        profit_factor = (gross_winner / gross_loser) if gross_loser > 0 else None

        winner_R = avg_winner_final / ONEIL_STOP
        loser_R = avg_loser_final / ONEIL_STOP
        win_rate_pos_neg = (n_winners / (n_winners + n_losers)) if (n_winners + n_losers) > 0 else 0.0
        expectancy_R = win_rate_pos_neg * winner_R + (1 - win_rate_pos_neg) * loser_R

        # Path-honesty proxy: share of "wins/moderate" that never dipped below pivot.
        # Replaces the legacy "drew down past -7%%" definition (no max_dd column exists).
        path_dishonest = int(s.get("n_path_dishonest_wins") or 0)
        n_total_wins = wins + int(s.get("modest") or 0)
        path_honesty_pct = ((n_total_wins - path_dishonest) / n_total_wins) if n_total_wins > 0 else None

        summary = {
            "n_resolved": resolved,
            "n_winners": wins,
            "win_rate": round(success_rate, 4),
            "win_rate_ci_lo": round(ci_lo, 4),
            "win_rate_ci_hi": round(ci_hi, 4),
            "avg_max_gain": round(float(s.get("avg_peak") or 0), 4),
            "avg_max_dd":   None,
            "avg_final":    round(float(s.get("avg_final") or 0), 4),
            "avg_R":        round(avg_R, 3),
            "sigma_R":      round(sigma_R, 3),
            "expectancy_R": round(expectancy_R, 3),
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "path_honesty_pct": round(path_honesty_pct, 4) if path_honesty_pct is not None else None,
            "n_path_dishonest_wins": path_dishonest,
            "n_total_wins": n_total_wins,
            "avg_ftd_quality": None,
            # Quantifiable Edges baseline: vanilla FTD ≈ 55%% precision
            "vs_baseline_pp": round((success_rate - 0.55) * 100, 1),
        }

        # ── 2. R-distribution histogram (over final_vs_pivot) ─────────────
        sql_hist = f"""
            SELECT {final_col} AS final_ret
              FROM breakout_events
             WHERE breakout_date BETWEEN %s AND %s
               AND {path_col} IS NOT NULL
               AND {final_col} IS NOT NULL
               {extra}
        """
        cur.execute(sql_hist, [from_date, to_date, *fparams])
        finals = [float(row["final_ret"]) for row in cur.fetchall() if row.get("final_ret") is not None]
        edges_R = [-3.0, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 10.0]
        bins = [0] * (len(edges_R) + 1)
        for r in finals:
            R = r / ONEIL_STOP
            placed = False
            for i, edge in enumerate(edges_R):
                if R < edge:
                    bins[i] += 1
                    placed = True
                    break
            if not placed:
                bins[-1] += 1
        labels = [f"<{edges_R[0]:.0f}R"]
        for i in range(1, len(edges_R)):
            labels.append(f"{edges_R[i-1]:+.1f}R")
        labels.append(f"≥{edges_R[-1]:.0f}R")
        r_distribution = [{"label": lab, "count": c} for lab, c in zip(labels, bins)]

        # ── 3. Regime stratification (JOIN events × breadth on breakout_date) ──
        sql_regime = f"""
            WITH ev AS (
              SELECT e.breakout_date, e.{path_col} AS path_bucket, e.{final_col} AS final_ret,
                     e.{peak_col} AS gain
                FROM breakout_events e
               WHERE e.breakout_date BETWEEN %s AND %s
                 AND e.{path_col} IS NOT NULL
                 {extra}
            )
            SELECT b.date, b.thrust, b.momentum_20pc, b.new_highs, b.new_lows,
                   b.pct_above_ema20,
                   ev.path_bucket, ev.final_ret, ev.gain
              FROM ev
              JOIN breadth_daily_history b ON b.date = ev.breakout_date
        """
        cur.execute(sql_regime, [from_date, to_date, *fparams])
        regime_rows = cur.fetchall()
        by_regime: dict = {}
        for row in regime_rows:
            reg = _classify_regime(
                bool(row.get("thrust") or 0),
                int(row.get("momentum_20pc") or 0),
                int(row.get("new_highs") or 0),
                int(row.get("new_lows") or 0),
                float(row.get("pct_above_ema20") or 0),
            )
            by_regime.setdefault(reg, []).append(row)

        regime_stratification = []
        for reg in REGIME_ORDER:
            rows = by_regime.get(reg, [])
            n = len(rows)
            if n == 0:
                regime_stratification.append({
                    "regime": reg, "n": 0, "win_rate": None, "ci_lo": None, "ci_hi": None,
                    "avg_R": None, "expectancy_R": None, "profit_factor": None,
                })
                continue
            wins_ = sum(1 for r in rows if r["path_bucket"] in SUCCESS_PATH_BUCKETS)
            wr = wins_ / n
            lo, hi = wilson_ci(wins_, n)
            finals_r = [float(r["final_ret"] or 0) for r in rows if r["final_ret"] is not None]
            pos = [f for f in finals_r if f > 0]
            neg = [f for f in finals_r if f < 0]
            avg_pos = (sum(pos) / len(pos)) if pos else 0.0
            avg_neg = (sum(neg) / len(neg)) if neg else 0.0
            n_pos, n_neg = len(pos), len(neg)
            wr_pn = (n_pos / (n_pos + n_neg)) if (n_pos + n_neg) else 0.0
            exp_R = wr_pn * (avg_pos / ONEIL_STOP) + (1 - wr_pn) * (avg_neg / ONEIL_STOP)
            gross_win = avg_pos * n_pos
            gross_loss = abs(avg_neg) * n_neg
            pf = (gross_win / gross_loss) if gross_loss > 0 else None
            avg_R_reg = (sum(finals_r) / len(finals_r)) / ONEIL_STOP if finals_r else 0.0
            regime_stratification.append({
                "regime": reg, "n": n,
                "win_rate": round(wr, 4),
                "ci_lo": round(lo, 4), "ci_hi": round(hi, 4),
                "avg_R": round(avg_R_reg, 3),
                "expectancy_R": round(exp_R, 3),
                "profit_factor": round(pf, 2) if pf is not None else None,
            })

        # ── 4. FTD-grade & vol-confirmed lift — columns don't exist on the
        # current schema. Frontend cards already handle empty arrays.
        ftd_lift = [
            {"grade": g, "n": 0, "win_rate": None, "ci_lo": None, "ci_hi": None, "avg_R": None}
            for g in ("A", "B", "C", "D")
        ]
        vol_lift = [
            {"vol_confirmed": c, "n": 0, "win_rate": None, "ci_lo": None, "ci_hi": None, "avg_R": None}
            for c in (True, False)
        ]

        # ── 5. Path-aware outcome breakdown (the headline metric) ───────────
        # Computed for BOTH 5d and 10d regardless of the selected window_days,
        # because the dashboard headline shows them side-by-side. The forward
        # cap for path-outcome is per-window, not the dashboard's window_days.
        path_outcomes = {}
        for path_window in (5, 10):
            path_to_date = as_of_date - timedelta(days=path_window + 2)
            po = _path_outcome_breakdown(cur, from_date, path_to_date,
                                         path_window, extra, fparams)
            path_outcomes[f"{path_window}d"] = po

        return {
            "as_of_date": as_of_date.isoformat(),
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "lookback_days": lookback_days,
            "window_days": window_days,
            "filters": {
                "liq_min": liq_min, "liq_max": liq_max,
                "mcap_min": mcap_min, "mcap_max": mcap_max,
                "sector": sector,
            },
            "summary": summary,
            "path_outcomes": path_outcomes,
            "r_distribution": r_distribution,
            "regime_stratification": regime_stratification,
            "ftd_lift": ftd_lift,
            "vol_lift": vol_lift,
            "constants": {
                "stop_basis": "O'Neil 7%",
                "stop_pct": ONEIL_STOP,
                "ftd_baseline_pct": 0.55,
            },
        }
    finally:
        conn.close()
