"""
Historical VALVO scoring — recompute the 12-parameter score at any past date.

Why this exists
---------------
VALVO's flagship score (services.scoring_service) takes a `data` dict and
returns a 0-10 final score. Today the inputs are gathered live (current
liquidity, current sector strength, current market trend). For Deep Research
on a *retrospective* move we need the score as it would have looked on day -1
of the move so we can answer: "did the system catch this?"

Every input is mechanically derived from data we already store
(candles_daily, candles_indices, v_stock_classification_v2, stock_universe).
Subjective fields (sector_strength, relative_strength, institutional_
participation, symmetry, linearity, market_trend) are reduced to objective
rules with documented thresholds — every score has a `reasoning` line so an
analyst can audit why the parameter scored what it did.

Public surface:
    compute_historical_score(security_id, symbol, as_of, move_window=...)
        → dict with `inputs`, `reasoning`, `scores`, `raw_composite`,
          `gatekeepers`, `combined_gatekeeper`, `final_score`, `rating`

Edge cases:
    • If `as_of` falls before the stock's listing date, returns `None`.
    • Missing data is filled with conservative defaults that DEPRESS the
      score (we never inflate); each is flagged in `reasoning` as 'data gap'.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

from database.database import get_db
from services.scoring_service import (
    PARAMETER_WEIGHTS,
    calculate_score_from_data,
)


# ═══════════════════════════════════════════════════════════════════════════
#  Thresholds — every magic number documented here, never inline
# ═══════════════════════════════════════════════════════════════════════════

LIQUIDITY_LOOKBACK_DAYS = 60        # prior trading sessions for avg turnover
ADR_LOOKBACK_DAYS = 20              # prior trading sessions for ADR
LINEARITY_LOOKBACK_DAYS = 60        # prior sessions for R² regression
RS_LOOKBACK_DAYS = 90               # prior sessions for relative-strength check
IP_LOOKBACK_DAYS = 30               # prior sessions for inst-participation volume check
IP_BASELINE_DAYS = 90               # baseline against which IP volume is compared
SECTOR_LOOKBACK_DAYS = 60           # prior sessions for sub-sector ROC ranking
SYMMETRY_LOOKBACK_DAYS = 30         # prior sessions for base-tightness check
MARKET_TREND_SMA_DAYS = 50          # SMA window for Smallcap-100 trend

# R² → linearity
LINEARITY_VERY_GOOD_R2 = 0.80
LINEARITY_GOOD_R2 = 0.50

# Sector strength: top-N quartile of sub-sectors over lookback
SECTOR_TOP_QUARTILE_FRACTION = 0.25

# Relative strength: stock ROC must exceed benchmark ROC by at least this many pp
RS_MARGIN_PP = 2.0

# IP: prior 30d avg volume > N × prior 90d avg volume
IP_VOLUME_MULTIPLIER = 1.5

# Symmetry: stdev/mean over base window <= this threshold passes
SYMMETRY_TIGHTNESS_THRESHOLD = 0.06

# Market trend bands (Smallcap-100 close vs its 50DMA)
MARKET_UPTREND_PCT = 2.0    # >+2% above SMA
MARKET_GRIND_DOWN_PCT = -2.0  # -2 to -8% below
MARKET_SHARP_DOWN_PCT = -8.0  # below -8%

# Move-EMA threshold: % of move-window days closing above each EMA
MOVE_EMA_PASS_PCT = 80.0


# ═══════════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════════

def compute_historical_score(
    *,
    security_id: str,
    symbol: str,
    as_of: str | date,
    move_window: tuple[str | date, str | date] | None = None,
    conn=None,
) -> dict[str, Any] | None:
    """Compute a full VALVO score AS OF the given date.

    `move_window` (from, to) — if provided, magnitude / ferocity / move_ema
    are computed against that window (the actual move). Otherwise these fall
    to zero (you're scoring a flat-base stock with no live move).
    """
    as_of_d = _to_date(as_of)
    move_from = _to_date(move_window[0]) if move_window else None
    move_to = _to_date(move_window[1]) if move_window else None

    should_close = False
    if conn is None:
        conn = get_db()
        should_close = True
    if conn is None:
        return None

    try:
        cur = conn.cursor()

        # Anchor close at as_of (last available trading day on/before)
        cur.execute(
            """
            SELECT date, open, high, low, close, volume
              FROM candles_daily
             WHERE security_id = %s AND date <= %s
          ORDER BY date DESC
             LIMIT 1
            """,
            (security_id, as_of_d),
        )
        anchor = cur.fetchone()
        if not anchor:
            return None
        anchor_close = float(anchor["close"])
        anchor_date = anchor["date"]

        # ─── Liquidity (avg turnover Cr, prior LIQUIDITY_LOOKBACK_DAYS) ───
        liquidity_cr, liquidity_reason = _liquidity(cur, security_id, anchor_date)

        # ─── ADR (avg daily range %, prior ADR_LOOKBACK_DAYS) ───
        adr_pct, adr_reason = _adr(cur, security_id, anchor_date)

        # ─── Market cap (Cr) at as_of ───
        mcap_cr, mcap_reason = _market_cap(cur, security_id, anchor_close, anchor_date)

        # ─── Linearity (R² of log(close) over prior LINEARITY_LOOKBACK_DAYS) ───
        linearity_label, linearity_reason = _linearity(cur, security_id, anchor_date)

        # ─── Sector strength (sub-sector ROC vs all sub-sectors, prior SECTOR_LOOKBACK_DAYS) ───
        sector_strength_bool, sector_reason, sub_sector_name = _sector_strength(
            cur, security_id, anchor_date,
        )

        # ─── Relative strength (stock 90d ROC vs Smallcap-100 90d ROC) ───
        rs_bool, rs_reason = _relative_strength(cur, security_id, anchor_date)

        # ─── Institutional participation (volume signature) ───
        ip_bool, ip_reason = _institutional_participation(cur, security_id, anchor_date)

        # ─── Symmetry (base tightness) ───
        sym_bool, sym_reason = _symmetry(cur, security_id, anchor_date)

        # ─── Market trend (Smallcap-100 vs 50DMA) ───
        market_trend, market_trend_reason = _market_trend(cur, anchor_date)

        # ─── Move-window-derived (magnitude / ferocity / move_ema) ───
        if move_from and move_to:
            move_pct, move_days, move_ema, move_reason = _move_window_metrics(
                cur, security_id, move_from, move_to,
            )
            previous_move_enabled = True
        else:
            move_pct = None
            move_days = None
            move_ema = None
            move_reason = "No move window provided — magnitude / ferocity / move_ema set to 0."
            previous_move_enabled = False

        # ─── Build the scoring `data` dict in the shape scoring_service expects ───
        data = {
            "market_cap": mcap_cr,
            "liquidity": liquidity_cr,
            "adr": adr_pct,
            "linearity": linearity_label,
            "sector_strength": sector_strength_bool,
            "relative_strength": rs_bool,
            "institutional_participation": ip_bool,
            "symmetry": sym_bool,
            "market_trend": market_trend,
            "previous_move_enabled": previous_move_enabled,
            "move_percentage": move_pct,
            "move_days": move_days,
            "move_ema": move_ema,
        }

        result = calculate_score_from_data(data)

        return {
            "as_of": str(anchor_date),
            "anchor_close": anchor_close,
            "sub_sector": sub_sector_name,
            "inputs": data,
            "reasoning": {
                "market_cap": mcap_reason,
                "liquidity": liquidity_reason,
                "adr": adr_reason,
                "linearity": linearity_reason,
                "sector_strength": sector_reason,
                "relative_strength": rs_reason,
                "institutional_participation": ip_reason,
                "symmetry": sym_reason,
                "market_trend": market_trend_reason,
                "move": move_reason,
            },
            "scores": result["individual_scores"],
            "weights": dict(PARAMETER_WEIGHTS),
            "raw_composite": result["raw_composite"],
            "gatekeepers": result["gatekeepers"],
            "combined_gatekeeper": result["combined_gatekeeper"],
            "final_score": result["final_score"],
            "rating": result["rating"],
        }
    finally:
        if should_close:
            try:
                conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
#  Per-parameter derivations
# ═══════════════════════════════════════════════════════════════════════════

def _liquidity(cur, security_id: str, anchor_date: date) -> tuple[float | None, str]:
    cur.execute(
        """
        SELECT AVG(volume::float8 * close / 1e7) AS avg_turnover_cr,
               COUNT(*) AS n
          FROM (
              SELECT volume, close
                FROM candles_daily
               WHERE security_id = %s AND date < %s
            ORDER BY date DESC
               LIMIT %s
          ) sub
        """,
        (security_id, anchor_date, LIQUIDITY_LOOKBACK_DAYS),
    )
    r = cur.fetchone()
    if not r or r["avg_turnover_cr"] is None:
        return None, f"Data gap: no candles in {LIQUIDITY_LOOKBACK_DAYS}d before {anchor_date}."
    val = float(r["avg_turnover_cr"])
    return round(val, 2), (
        f"Avg turnover (vol × close) over prior {int(r['n'])} sessions before "
        f"{anchor_date}: ₹{val:,.2f} Cr."
    )


def _adr(cur, security_id: str, anchor_date: date) -> tuple[float | None, str]:
    cur.execute(
        """
        SELECT AVG(((high - low) / NULLIF(close, 0)) * 100) AS avg_adr,
               COUNT(*) AS n
          FROM (
              SELECT high, low, close
                FROM candles_daily
               WHERE security_id = %s AND date < %s
            ORDER BY date DESC
               LIMIT %s
          ) sub
        """,
        (security_id, anchor_date, ADR_LOOKBACK_DAYS),
    )
    r = cur.fetchone()
    if not r or r["avg_adr"] is None:
        return None, f"Data gap: no ADR data in {ADR_LOOKBACK_DAYS}d before {anchor_date}."
    val = float(r["avg_adr"])
    return round(val, 2), (
        f"Avg daily range (high-low)/close over prior {int(r['n'])} sessions: {val:.2f}%."
    )


def _market_cap(cur, security_id: str, close_at: float, anchor_date: date) -> tuple[float | None, str]:
    cur.execute(
        "SELECT shares_outstanding FROM stock_universe WHERE security_id = %s LIMIT 1",
        (security_id,),
    )
    r = cur.fetchone()
    if not r or not r.get("shares_outstanding"):
        return None, "Data gap: shares_outstanding not on file."
    shares = float(r["shares_outstanding"])
    mcap = round(shares * close_at / 1e7, 2)
    return mcap, (
        f"Close ₹{close_at:,.2f} × {shares / 1e7:.2f} Cr shares = ₹{mcap:,.0f} Cr at {anchor_date}."
    )


def _linearity(cur, security_id: str, anchor_date: date) -> tuple[str, str]:
    """R² of log(close) on day index. >0.80 Very Good, >0.50 Good, else Bad."""
    cur.execute(
        """
        SELECT close
          FROM (
              SELECT close, date
                FROM candles_daily
               WHERE security_id = %s AND date < %s
            ORDER BY date DESC
               LIMIT %s
          ) sub
      ORDER BY date ASC
        """,
        (security_id, anchor_date, LINEARITY_LOOKBACK_DAYS),
    )
    closes = [float(row["close"]) for row in cur.fetchall() if row["close"] and row["close"] > 0]
    if len(closes) < 20:
        return "Bad", f"Data gap: only {len(closes)} sessions of price data — insufficient for R² fit."

    ys = [math.log(c) for c in closes]
    n = len(ys)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    sxx = sum((xs[i] - mean_x) ** 2 for i in range(n))
    syy = sum((ys[i] - mean_y) ** 2 for i in range(n))
    if sxx == 0 or syy == 0:
        return "Bad", "Data gap: zero variance in price series."
    r2 = (sxy ** 2) / (sxx * syy)

    if r2 >= LINEARITY_VERY_GOOD_R2:
        label = "Very Good"
    elif r2 >= LINEARITY_GOOD_R2:
        label = "Good"
    else:
        label = "Bad"
    return label, (
        f"R²={r2:.2f} over prior {n} sessions of log-close → {label} "
        f"(thresholds: ≥{LINEARITY_VERY_GOOD_R2:.2f} Very Good, "
        f"≥{LINEARITY_GOOD_R2:.2f} Good, else Bad)."
    )


def _sector_strength(cur, security_id: str, anchor_date: date) -> tuple[bool, str, str | None]:
    """True if the stock's sub-sector is in top quartile of sub-sectors by SECTOR_LOOKBACK_DAYS ROC."""
    # Resolve stock's sub-sector
    try:
        cur.execute(
            "SELECT sub_sector_name FROM v_stock_classification_v2 WHERE security_id = %s LIMIT 1",
            (security_id,),
        )
        r = cur.fetchone()
    except Exception:
        return False, "Data gap: classification view unavailable.", None

    if not r or not r.get("sub_sector_name"):
        return False, "Data gap: sub-sector not classified.", None

    sub = r["sub_sector_name"]

    # Compute ROC for every sub-sector over prior SECTOR_LOOKBACK_DAYS (anchor inclusive end)
    start = anchor_date - timedelta(days=int(SECTOR_LOOKBACK_DAYS * 1.6))  # calendar buffer
    try:
        cur.execute(
            """
            WITH bound AS (
                SELECT sub_sector_name,
                       MIN(cd.date) AS fd,
                       MAX(cd.date) AS ld
                  FROM v_stock_classification_v2 v
                  JOIN candles_daily cd ON cd.security_id = v.security_id
                 WHERE cd.date >= %s AND cd.date < %s
                   AND v.sub_sector_name IS NOT NULL
              GROUP BY sub_sector_name
                HAVING COUNT(*) >= 5
            ),
            roc AS (
                SELECT b.sub_sector_name,
                       AVG(((lc.close - fc.close) / NULLIF(fc.close, 0)) * 100) AS avg_roc,
                       COUNT(*) AS n_stocks
                  FROM bound b
                  JOIN v_stock_classification_v2 v ON v.sub_sector_name = b.sub_sector_name
                  JOIN candles_daily fc ON fc.security_id = v.security_id AND fc.date = b.fd
                  JOIN candles_daily lc ON lc.security_id = v.security_id AND lc.date = b.ld
              GROUP BY b.sub_sector_name
            )
            SELECT sub_sector_name, avg_roc, n_stocks,
                   PERCENT_RANK() OVER (ORDER BY avg_roc) AS pct_rank
              FROM roc
            """,
            (start, anchor_date),
        )
        rows = cur.fetchall()
    except Exception as e:
        return False, f"Data gap: sub-sector ranking failed ({e}).", sub

    target = next((row for row in rows if row["sub_sector_name"] == sub), None)
    if not target:
        return False, f"Data gap: sub-sector '{sub}' had no ROC data over prior {SECTOR_LOOKBACK_DAYS}d.", sub

    pct_rank = float(target["pct_rank"] or 0)
    avg_roc = float(target["avg_roc"] or 0)
    threshold = 1.0 - SECTOR_TOP_QUARTILE_FRACTION
    passed = pct_rank >= threshold
    n_total = len(rows)
    rank_pos = sum(1 for row in rows if (row["avg_roc"] or 0) >= avg_roc)
    return passed, (
        f"{'✓' if passed else '✗'} '{sub}' avg ROC over prior {SECTOR_LOOKBACK_DAYS}d = "
        f"{avg_roc:+.2f}% (rank {rank_pos}/{n_total}, percentile "
        f"{pct_rank * 100:.0f}). Top-quartile threshold = {threshold * 100:.0f}th percentile."
    ), sub


def _relative_strength(cur, security_id: str, anchor_date: date) -> tuple[bool, str]:
    """Stock's prior 90d ROC must exceed Smallcap-100 prior 90d ROC by RS_MARGIN_PP."""
    start = anchor_date - timedelta(days=int(RS_LOOKBACK_DAYS * 1.6))

    cur.execute(
        """
        WITH win AS (
            SELECT MIN(date) fd, MAX(date) ld
              FROM candles_daily
             WHERE security_id = %s AND date >= %s AND date < %s
        )
        SELECT fc.close fc_close, lc.close lc_close, w.fd, w.ld
          FROM win w
          LEFT JOIN candles_daily fc ON fc.security_id = %s AND fc.date = w.fd
          LEFT JOIN candles_daily lc ON lc.security_id = %s AND lc.date = w.ld
        """,
        (security_id, start, anchor_date, security_id, security_id),
    )
    r = cur.fetchone()
    if not r or not r["fc_close"] or not r["lc_close"]:
        return False, "Data gap: insufficient stock candles for RS check."
    stock_roc = (float(r["lc_close"]) - float(r["fc_close"])) / float(r["fc_close"]) * 100

    cur.execute(
        """
        WITH win AS (
            SELECT MIN(date) fd, MAX(date) ld
              FROM candles_indices
             WHERE symbol = 'NIFTY SMALLCAP 100' AND date >= %s AND date < %s
        )
        SELECT fc.close fc_close, lc.close lc_close
          FROM win w
          LEFT JOIN candles_indices fc ON fc.symbol = 'NIFTY SMALLCAP 100' AND fc.date = w.fd
          LEFT JOIN candles_indices lc ON lc.symbol = 'NIFTY SMALLCAP 100' AND lc.date = w.ld
        """,
        (start, anchor_date),
    )
    rb = cur.fetchone()
    if not rb or not rb["fc_close"] or not rb["lc_close"]:
        return False, "Data gap: insufficient Smallcap-100 candles for RS check."
    bench_roc = (float(rb["lc_close"]) - float(rb["fc_close"])) / float(rb["fc_close"]) * 100

    delta = stock_roc - bench_roc
    passed = delta >= RS_MARGIN_PP
    return passed, (
        f"{'✓' if passed else '✗'} Stock {RS_LOOKBACK_DAYS}d ROC {stock_roc:+.2f}% vs "
        f"Smallcap-100 {bench_roc:+.2f}% (delta {delta:+.2f}pp; pass threshold ≥{RS_MARGIN_PP:.1f}pp)."
    )


def _institutional_participation(cur, security_id: str, anchor_date: date) -> tuple[bool, str]:
    """Avg volume in last IP_LOOKBACK_DAYS sessions > IP_VOLUME_MULTIPLIER × baseline (IP_BASELINE_DAYS)."""
    cur.execute(
        """
        SELECT
            (SELECT AVG(volume::float8) FROM (
                SELECT volume FROM candles_daily WHERE security_id = %s AND date < %s
                ORDER BY date DESC LIMIT %s
            ) recent) AS recent_avg,
            (SELECT AVG(volume::float8) FROM (
                SELECT volume FROM candles_daily WHERE security_id = %s AND date < %s
                ORDER BY date DESC LIMIT %s
            ) base) AS base_avg
        """,
        (security_id, anchor_date, IP_LOOKBACK_DAYS, security_id, anchor_date, IP_BASELINE_DAYS),
    )
    r = cur.fetchone()
    if not r or not r["recent_avg"] or not r["base_avg"]:
        return False, "Data gap: insufficient volume history for IP signature."
    recent = float(r["recent_avg"])
    base = float(r["base_avg"])
    ratio = recent / base if base else 0
    passed = ratio >= IP_VOLUME_MULTIPLIER
    return passed, (
        f"{'✓' if passed else '✗'} Last {IP_LOOKBACK_DAYS}d avg volume "
        f"{recent / 1e5:,.1f} L vs {IP_BASELINE_DAYS}d baseline "
        f"{base / 1e5:,.1f} L = {ratio:.2f}× (pass threshold ≥{IP_VOLUME_MULTIPLIER:.1f}×)."
    )


def _symmetry(cur, security_id: str, anchor_date: date) -> tuple[bool, str]:
    """Stdev/mean of close over prior SYMMETRY_LOOKBACK_DAYS ≤ SYMMETRY_TIGHTNESS_THRESHOLD = tight base."""
    cur.execute(
        """
        SELECT close
          FROM (
              SELECT close FROM candles_daily WHERE security_id = %s AND date < %s
              ORDER BY date DESC LIMIT %s
          ) sub
        """,
        (security_id, anchor_date, SYMMETRY_LOOKBACK_DAYS),
    )
    closes = [float(r["close"]) for r in cur.fetchall() if r["close"]]
    if len(closes) < 10:
        return False, f"Data gap: only {len(closes)} sessions for symmetry/base check."
    mean = sum(closes) / len(closes)
    if mean == 0:
        return False, "Data gap: mean close is zero."
    var = sum((c - mean) ** 2 for c in closes) / len(closes)
    stdev = math.sqrt(var)
    cv = stdev / mean
    passed = cv <= SYMMETRY_TIGHTNESS_THRESHOLD
    return passed, (
        f"{'✓' if passed else '✗'} Coefficient of variation (stdev/mean) over prior "
        f"{len(closes)} sessions = {cv * 100:.2f}% "
        f"(pass threshold ≤{SYMMETRY_TIGHTNESS_THRESHOLD * 100:.0f}%)."
    )


def _market_trend(cur, anchor_date: date) -> tuple[str, str]:
    """Smallcap-100 close on anchor_date vs its 50-session SMA."""
    cur.execute(
        """
        SELECT close
          FROM (
              SELECT close, date
                FROM candles_indices
               WHERE symbol = 'NIFTY SMALLCAP 100' AND date <= %s
            ORDER BY date DESC
               LIMIT %s
          ) sub
      ORDER BY date ASC
        """,
        (anchor_date, MARKET_TREND_SMA_DAYS + 1),
    )
    closes = [float(r["close"]) for r in cur.fetchall() if r["close"]]
    if len(closes) < 30:
        return "Sideways", f"Data gap: only {len(closes)} Smallcap-100 sessions; defaulting to Sideways."
    today_close = closes[-1]
    sma = sum(closes[:-1]) / max(1, len(closes) - 1)
    if sma == 0:
        return "Sideways", "Data gap: zero SMA."
    delta_pct = (today_close - sma) / sma * 100
    if delta_pct >= MARKET_UPTREND_PCT:
        label = "Uptrend"
    elif delta_pct <= MARKET_SHARP_DOWN_PCT:
        label = "Sharp Downtrend"
    elif delta_pct <= MARKET_GRIND_DOWN_PCT:
        label = "Grind Downtrend"
    else:
        label = "Sideways"
    return label, (
        f"Smallcap-100 close {today_close:,.0f} vs {MARKET_TREND_SMA_DAYS}DMA "
        f"{sma:,.0f} → {delta_pct:+.2f}% ⇒ {label} "
        f"(bands: ≥{MARKET_UPTREND_PCT:+.0f}% Uptrend, "
        f"≤{MARKET_GRIND_DOWN_PCT:+.0f}% Grind Down, "
        f"≤{MARKET_SHARP_DOWN_PCT:+.0f}% Sharp Down, else Sideways)."
    )


def _move_window_metrics(
    cur, security_id: str, move_from: date, move_to: date,
) -> tuple[float | None, int | None, str, str]:
    """Compute the actual move %, days, and which EMA was respected."""
    cur.execute(
        """
        SELECT date, close
          FROM candles_daily
         WHERE security_id = %s AND date >= %s AND date <= %s
      ORDER BY date ASC
        """,
        (security_id, move_from, move_to),
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return None, None, "5 EMA", f"Data gap: only {len(rows)} sessions in move window."
    closes = [float(r["close"]) for r in rows]
    move_days = len(closes)
    move_pct = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0

    # We need to compute EMAs WITHIN the move window — start the EMA seed from a point
    # 30+ sessions before move_from so the EMA has stabilised by the time move starts.
    cur.execute(
        """
        SELECT close
          FROM (
              SELECT close, date
                FROM candles_daily
               WHERE security_id = %s AND date < %s
            ORDER BY date DESC
               LIMIT 60
          ) sub
      ORDER BY date ASC
        """,
        (security_id, move_from),
    )
    pre = [float(r["close"]) for r in cur.fetchall() if r["close"]]
    full = pre + closes
    ema5 = _ema_series(full, 5)
    ema10 = _ema_series(full, 10)
    ema20 = _ema_series(full, 20)

    in_move_offset = len(pre)
    above5 = sum(1 for i in range(in_move_offset, len(full)) if full[i] >= ema5[i])
    above10 = sum(1 for i in range(in_move_offset, len(full)) if full[i] >= ema10[i])
    above20 = sum(1 for i in range(in_move_offset, len(full)) if full[i] >= ema20[i])
    total = move_days

    pct5 = (above5 / total) * 100 if total else 0
    pct10 = (above10 / total) * 100 if total else 0
    pct20 = (above20 / total) * 100 if total else 0

    # Pick tightest EMA the move respected (5 → 10 → 20).
    if pct5 >= MOVE_EMA_PASS_PCT:
        chosen = "5 EMA"
    elif pct10 >= MOVE_EMA_PASS_PCT:
        chosen = "10 EMA"
    else:
        chosen = "20 EMA"

    return round(move_pct, 2), int(move_days), chosen, (
        f"Move {move_pct:+.2f}% over {move_days} sessions ({move_from} → {move_to}). "
        f"Days above 5/10/20 EMA: {pct5:.0f}% / {pct10:.0f}% / {pct20:.0f}% "
        f"(tightest EMA crossing {MOVE_EMA_PASS_PCT:.0f}% bar = {chosen})."
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _to_date(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(d, "%Y-%m-%d").date()


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out
