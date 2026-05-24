"""
Breakout Success Intelligence — outcome v2 helpers (path-dependent labeling)
============================================================================

Pure-function helpers that implement the canonical methodology for measuring
breakout success without the path-dependence bug in the legacy `_bucket()` /
`update_outcomes()`. None of these wire themselves into the pipeline yet —
they are intended to be invoked from a parallel labeler that writes the
`tb_*`, `mae_*`, `mfe_*`, `bucket_v2_*` columns added by
`breakout_outcome_v2_migration.sql`.

Companions:
  - Backend/database/breakout_outcome_v2_migration.sql (additive ALTER)
  - memory/project_breakout_success_testing_canon.md (research synthesis)

References:
  - López de Prado, *Advances in Financial Machine Learning* ch. 3 (triple-barrier)
  - Van Tharp, *Trade Your Way to Financial Freedom* (R-multiples)
  - Carver, *Systematic Trading* (vol-anchored stops)
  - vectorbt OHLC stop-handling docs (pessimistic SL-first same-bar rule)
  - Wilson (1927) score interval for binomial proportions
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


# ──────────────────────────────────────────────────────────────────────────────
# Triple-Barrier Labeling (López de Prado)
# ──────────────────────────────────────────────────────────────────────────────

def label_triple_barrier(
    forward_candles: Sequence[dict],
    entry: float,
    stop_price: float,
    target_price: float,
    vertical_n: int,
    risk_per_share: Optional[float] = None,
) -> dict:
    """Walk forward bar-by-bar, return the first-touched barrier outcome.

    Same-bar SL+TP collision: pessimistic SL-first (vectorbt convention).
    Gap handling: if open is past a barrier, fill at the open (worse-than-stop
    on gap-down, better-than-target on gap-up).

    Returns dict with:
      label         'pt_hit' | 'sl_hit' | 'time_exit'
      exit_idx      bar index 0..vertical_n-1 of exit bar
      exit_price    realized fill
      mae           min(low - entry) / entry  (signed, ≤0)
      mfe           max(high - entry) / entry (signed, ≥0)
      R_outcome     (exit_price - entry) / risk_per_share  (None if risk_per_share is None)
      days_in_trade exit_idx + 1
    """
    if not forward_candles:
        return {
            "label": "no_data", "exit_idx": None, "exit_price": entry,
            "mae": 0.0, "mfe": 0.0, "R_outcome": 0.0, "days_in_trade": 0,
        }

    if risk_per_share is None:
        risk_per_share = max(entry - stop_price, 1e-9)

    mae_low = entry
    mfe_high = entry
    n = min(vertical_n, len(forward_candles))

    for i in range(n):
        bar = forward_candles[i]
        hi = float(bar["high"]); lo = float(bar["low"])
        op = float(bar["open"]); cl = float(bar["close"])
        mae_low = min(mae_low, lo)
        mfe_high = max(mfe_high, hi)

        sl_touched = lo <= stop_price
        pt_touched = hi >= target_price

        # Same-bar collision: SL first (pessimistic / vectorbt convention)
        if sl_touched:
            exit_px = min(stop_price, op) if op < stop_price else stop_price
            return {
                "label": "sl_hit", "exit_idx": i, "exit_price": exit_px,
                "mae": (mae_low - entry) / entry,
                "mfe": (mfe_high - entry) / entry,
                "R_outcome": (exit_px - entry) / risk_per_share,
                "days_in_trade": i + 1,
            }
        if pt_touched:
            exit_px = max(target_price, op) if op > target_price else target_price
            return {
                "label": "pt_hit", "exit_idx": i, "exit_price": exit_px,
                "mae": (mae_low - entry) / entry,
                "mfe": (mfe_high - entry) / entry,
                "R_outcome": (exit_px - entry) / risk_per_share,
                "days_in_trade": i + 1,
            }

    last_close = float(forward_candles[n - 1]["close"])
    return {
        "label": "time_exit", "exit_idx": n - 1, "exit_price": last_close,
        "mae": (mae_low - entry) / entry,
        "mfe": (mfe_high - entry) / entry,
        "R_outcome": (last_close - entry) / risk_per_share,
        "days_in_trade": n,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Path-honest bucket (replaces legacy _bucket)
# ──────────────────────────────────────────────────────────────────────────────

def bucket_v2(
    label: str, R_outcome: float, mae_atr: float, mfe_atr: float,
    days_in_trade: int,
) -> str:
    """Path-dependent bucket category. The key change vs legacy: a trade that
    hit the stop FIRST is `failed_quick`/`failed_late`, never `strong_gt_10`."""
    if label == "sl_hit":
        return "failed_quick" if days_in_trade <= 3 else "failed_late"

    if label == "pt_hit":
        if R_outcome >= 4.0: return "win_4R_plus"
        if R_outcome >= 2.0: return "win_2to4R"
        return "win_1to2R"

    if label == "time_exit":
        if R_outcome >= 1.0:  return "time_win"
        if R_outcome >= 0.0:  return "time_breakeven"
        if R_outcome > -1.0:
            if mae_atr <= -1.5: return "time_whipsaw_recovered"
            return "time_drift"
        return "time_loss"

    return "unknown"


WIN_BUCKETS_V2 = frozenset({
    "win_1to2R", "win_2to4R", "win_4R_plus", "time_win",
})


# ──────────────────────────────────────────────────────────────────────────────
# Synthetic stops (compute at event-creation time, persist on the row)
# ──────────────────────────────────────────────────────────────────────────────

def synthetic_stops(
    entry: float, pivot: float, range_low: float, atr14: float,
    stop_atr_mult: float = 1.5, stop_struct_pct: float = 0.07,
) -> dict:
    """Two industry-standard synthetic stops, computable from data we already have.

    stop_atr     = entry - 1.5 * ATR14   (Clenow/Carver/Chandelier — vol-anchored,
                                          comparable across stocks)
    stop_struct  = max(range_low, pivot * (1 - 0.07))
                                         (O'Neil 7-8% rule, structural invalidation)
    """
    stop_atr_p = entry - stop_atr_mult * atr14
    stop_struct_p = max(range_low, pivot * (1 - stop_struct_pct))
    return {
        "stop_atr":    stop_atr_p,
        "stop_struct": stop_struct_p,
        "risk_atr":    max(entry - stop_atr_p, 1e-9),
        "risk_struct": max(entry - stop_struct_p, 1e-9),
    }


# ──────────────────────────────────────────────────────────────────────────────
# NSE-tiered slippage (apply to entry price for honest backtest)
# ──────────────────────────────────────────────────────────────────────────────

def slippage_bps_for_liq(liq_cr: Optional[float]) -> int:
    """Return realistic slippage in basis points for an NSE entry, by liquidity tier.

    Calibrated against NSE impact-cost methodology — Nifty-50 names
    avg ~5 bps; smallcap/Emerge segment avg 16%+; we use a conservative
    ramp that flips many micro-cap edges from positive to neutral."""
    if liq_cr is None:
        return 40
    if liq_cr >= 5.0:  return 5     # very_liquid
    if liq_cr >= 2.0:  return 15    # liquid
    if liq_cr >= 0.5:  return 40    # moderate
    return 100                      # illiquid


def apply_slippage(price: float, bps: int) -> float:
    return price * (1.0 + bps / 10000.0)


# ──────────────────────────────────────────────────────────────────────────────
# Wilson 95% CI for win rates (statistical honesty in the dashboard)
# ──────────────────────────────────────────────────────────────────────────────

def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. Robust for small n.

    Brown, Cai & DasGupta (2001) recommend Wilson over Wald for n<40; Wilson
    is also the default in scipy.stats.binomtest(..).proportion_ci('wilson').

    Minimum-N rule of thumb at p≈0.5:
        ±5pp width @ 95%  → N ≥ 384
        ±10pp             → N ≥ 96
        ±15pp             → N ≥ 43
    """
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


# ──────────────────────────────────────────────────────────────────────────────
# FTD scoring v2 — Goldilocks extension curve, ATR-scaled pivot tolerance,
# index-regime multiplier
# ──────────────────────────────────────────────────────────────────────────────

def extension_curve(close: float, pivot: float,
                    peak_pct: float = 0.02, decay_start_pct: float = 0.05) -> float:
    """Replaces the monotonic-increasing c5. Peaks at +2% above pivot, decays
    sharply past +5% (Minervini explicitly warns against chasing >5% past pivot)."""
    if pivot <= 0:
        return 0.0
    ext = (close - pivot) / pivot
    if ext < 0:
        return 0.0
    if ext < peak_pct:
        return ext / peak_pct * 0.5
    if ext < 0.03:
        return 1.0
    if ext < decay_start_pct:
        return 1.0 - (ext - 0.03) / (decay_start_pct - 0.03) * 0.3
    return max(0.0, 0.7 - (ext - decay_start_pct) * 10.0)


def low_holds_pivot_atr_scaled(low: float, pivot: float, atr: float) -> float:
    """Replaces c4's hardcoded 2% tolerance with an ATR-scaled tolerance.
    NSE small-caps routinely wick 3-5% intraday before reclaiming."""
    if pivot <= 0:
        return 0.0
    if low >= pivot:
        return 1.0
    tolerance = max(0.5 * atr, pivot * 0.005)  # at least 0.5% floor
    return max(0.0, 1.0 - (pivot - low) / tolerance)


def index_regime_multiplier(regime: Optional[str]) -> float:
    """Multiply the stock-level FTD score by the index-level regime tilt.
    Single highest-leverage change in the FTD scorer (Quantifiable Edges:
    vanilla FTD = 55% precision; breadth-confirmed = 75-100%)."""
    table = {
        "BREADTH_THRUST":  1.20,
        "CONFIRMED_RALLY": 1.00,
        "RALLY_ATTEMPT":   0.90,
        "AGGRESSIVE":      1.00,
        "NORMAL":          0.85,
        "SELECTIVE":       0.70,
        "UNDER_PRESSURE":  0.65,
        "CAUTIOUS":        0.50,
        "CORRECTION":      0.40,
    }
    return table.get(str(regime or "").upper(), 0.75)


# ──────────────────────────────────────────────────────────────────────────────
# ATR-normalized win threshold (separates skill from noise across vol regimes)
# ──────────────────────────────────────────────────────────────────────────────

def is_real_win_atr(max_gain: float, atr_pct: float,
                    threshold_atr: float = 3.0) -> bool:
    """A 5% gain on a low-vol stock (1.5% ATR) is 3.3 ATR (real); the same 5%
    on a high-vol stock (4.5% ATR) is 1.1 ATR (noise). Current bucket lumps
    them together. ATR-normalization separates skill from noise."""
    if atr_pct <= 0:
        return False
    return (max_gain / atr_pct) >= threshold_atr
