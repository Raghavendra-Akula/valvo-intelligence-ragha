"""
Path-aware breakout outcome classifier.

Answers: "Of the breakouts we triggered, how many actually worked vs failed?"

Bucket priority (first match wins). All thresholds measured against `pivot_price`,
not entry — user spec describes everything in pivot terms. Thresholds are
cfg-driven (passed via the `cfg` dict) with sensible defaults.

  failed_d1_reversal           Day T+1 high >= pivot*(1+d1_spike) AND close < pivot.
                               The classic intraday-spike-and-close-back-below fakeout.

  failed_pre_5pct_drop_close   Within Days T+1..T+pre_window, the bar both wicked
                               AND closed below pivot before max-high reached
                               pivot*(1+target_5pct). Real selling — bears took
                               control on a closing basis.

  failed_pre_5pct_drop_wick    Same window, low went below pivot but close stayed
                               above pivot. Intraday dip / liquidity sweep that
                               didn't follow through. Less informative than
                               _close, useful as a separate signal.

  failed_no_5pct_in_window     Window expired without ever touching
                               pivot*(1+target_5pct). Slow drift up that never
                               got moving.

  moderate_reversed            Hit pivot*(1+target_5pct) but failed to sustain
                               into the lowest success tier (5_to_7).

  success_5_to_7               Peak in [+5%, +7%) AND final close >=
                               pivot*(1+success_5to7_final_min).

  success_7_to_10              Peak in [+7%, +10%) AND final close >=
                               pivot*(1+success_7to10_final_min).

  success_strong_gt_10         Peak >= +10% AND final close >=
                               pivot*(1+success_strong_final_min).

The success cascade is "best tier whose floor you cleared on BOTH peak AND final".
A peak ≥ +10% that gives back to a +4% close lands in success_5_to_7 (still up,
but didn't hold the strong tier).

Companion to `breakout_outcomes_v2.label_triple_barrier`. This is the
plain-English bucketing for the dashboard headline; v2 is the R-multiple
analytical layer.
"""
from typing import Optional


DEFAULTS = {
    "d1_spike_pct":             0.03,   # Day T+1 high must reach pivot*(1+x) for d1_reversal
    "pre_5pct_window_days":     3,      # T+1..T+window for the "below pivot before 5%" rule
    "target_5pct":              0.05,
    "target_7pct":              0.07,
    "target_10pct":             0.10,
    "success_5to7_final_min":   0.03,   # final close must be >= pivot*(1+x) for 5_to_7
    "success_7to10_final_min":  0.05,
    "success_strong_final_min": 0.07,
}


PATH_BUCKETS = (
    "failed_d1_reversal",
    "failed_pre_5pct_drop_close",
    "failed_pre_5pct_drop_wick",
    "failed_no_5pct_in_window",
    "moderate_reversed",
    "success_5_to_7",
    "success_7_to_10",
    "success_strong_gt_10",
)

FAILED_BUCKETS = {
    "failed_d1_reversal",
    "failed_pre_5pct_drop_close",
    "failed_pre_5pct_drop_wick",
    "failed_no_5pct_in_window",
}
MODERATE_BUCKETS = {"moderate_reversed"}
SUCCESS_BUCKETS = {"success_5_to_7", "success_7_to_10", "success_strong_gt_10"}


def _bucket_outcome(peak_pct: float, final_pct: float, cfg: dict) -> str:
    """Cascade peak+final through the three success tiers; fall to moderate
    if no tier's BOTH floors are met."""
    target_5  = float(cfg["target_5pct"])
    target_7  = float(cfg["target_7pct"])
    target_10 = float(cfg["target_10pct"])
    f_5to7    = float(cfg["success_5to7_final_min"])
    f_7to10   = float(cfg["success_7to10_final_min"])
    f_strong  = float(cfg["success_strong_final_min"])

    if peak_pct >= target_10 and final_pct >= f_strong:
        return "success_strong_gt_10"
    if peak_pct >= target_7 and final_pct >= f_7to10:
        return "success_7_to_10"
    if peak_pct >= target_5 and final_pct >= f_5to7:
        return "success_5_to_7"
    return "moderate_reversed"


def classify_path_outcome(
    pivot: float,
    fwd_bars: list,
    window_n: int,
    cfg: Optional[dict] = None,
) -> Optional[dict]:
    """Walk the first `window_n` forward bars (T+1..T+window_n) and return a
    path-aware outcome bucket.

    fwd_bars: list of dicts with keys 'open', 'high', 'low', 'close',
              ordered ascending (T+1, T+2, ...). Bars BEFORE T+1 must
              already be filtered out by the caller.

    cfg: optional dict overriding any of the keys in DEFAULTS.

    Returns None if there aren't enough bars yet to evaluate (caller should
    leave the row unresolved). The 'failed_pre_5pct_drop_*' rules need at
    least min(window_n, pre_5pct_window_days) bars to be definitive; for
    short forward arrays we still classify if a determinative rule has
    fired, otherwise we return None.
    """
    if pivot is None or pivot <= 0 or not fwd_bars or window_n < 1:
        return None

    seg = fwd_bars[:window_n]
    if not seg:
        return None

    cfg = {**DEFAULTS, **(cfg or {})}
    d1_spike   = float(cfg["d1_spike_pct"])
    pre_window = int(cfg["pre_5pct_window_days"])
    target_5   = float(cfg["target_5pct"])

    p_d1 = pivot * (1 + d1_spike)
    p_5  = pivot * (1 + target_5)

    # Day T+1 reversal: most specific bucket, check first.
    d1_high  = float(seg[0]["high"])
    d1_close = float(seg[0]["close"])
    if d1_high >= p_d1 and d1_close < pivot:
        peak_pct  = max(float(b["high"]) for b in seg) / pivot - 1.0
        final_pct = float(seg[-1]["close"]) / pivot - 1.0
        return {
            "bucket": "failed_d1_reversal",
            "peak_pct": round(peak_pct, 4),
            "final_pct": round(final_pct, 4),
            "hit_5pct_day": None,
            "hit_below_pivot_day": 1,
            "n_bars_evaluated": len(seg),
        }

    # Walk T+1..T+min(window_n, pre_window) for failed_pre_5pct_drop_{close|wick}.
    running_max_high = 0.0
    hit_5pct_day = None
    hit_below_pivot_day = None

    pre_5pct_window = min(len(seg), pre_window)
    for i in range(pre_5pct_window):
        bar = seg[i]
        bar_high  = float(bar["high"])
        bar_low   = float(bar["low"])
        bar_close = float(bar["close"])
        running_max_high = max(running_max_high, bar_high)

        if hit_5pct_day is None and running_max_high >= p_5:
            hit_5pct_day = i + 1

        if bar_low < pivot and hit_below_pivot_day is None:
            hit_below_pivot_day = i + 1
            if hit_5pct_day is None:
                # Determinative failure — split by close-vs-pivot:
                # close < pivot → real selling; close >= pivot → intraday wick only.
                bucket = ("failed_pre_5pct_drop_close" if bar_close < pivot
                          else "failed_pre_5pct_drop_wick")
                peak_pct  = running_max_high / pivot - 1.0 if running_max_high else 0.0
                final_pct = float(seg[-1]["close"]) / pivot - 1.0
                return {
                    "bucket": bucket,
                    "peak_pct": round(peak_pct, 4),
                    "final_pct": round(final_pct, 4),
                    "hit_5pct_day": None,
                    "hit_below_pivot_day": i + 1,
                    "n_bars_evaluated": len(seg),
                }

    # If we're still walking and need more days for a definitive call, only
    # bail if we don't have a full window AND haven't yet hit 5%.
    if len(seg) < window_n and hit_5pct_day is None:
        return None

    # Continue walking past the pre_window to check for 5% touch and final outcome.
    for i in range(pre_5pct_window, len(seg)):
        bar = seg[i]
        running_max_high = max(running_max_high, float(bar["high"]))
        if hit_5pct_day is None and running_max_high >= p_5:
            hit_5pct_day = i + 1

    final_close = float(seg[-1]["close"])
    final_pct   = final_close / pivot - 1.0
    peak_pct    = running_max_high / pivot - 1.0

    if hit_5pct_day is None:
        return {
            "bucket": "failed_no_5pct_in_window",
            "peak_pct": round(peak_pct, 4),
            "final_pct": round(final_pct, 4),
            "hit_5pct_day": None,
            "hit_below_pivot_day": hit_below_pivot_day,
            "n_bars_evaluated": len(seg),
        }

    bucket = _bucket_outcome(peak_pct, final_pct, cfg)

    return {
        "bucket": bucket,
        "peak_pct": round(peak_pct, 4),
        "final_pct": round(final_pct, 4),
        "hit_5pct_day": hit_5pct_day,
        "hit_below_pivot_day": hit_below_pivot_day,
        "n_bars_evaluated": len(seg),
    }


def is_failed(bucket: Optional[str]) -> bool:
    return bucket in FAILED_BUCKETS


def is_moderate(bucket: Optional[str]) -> bool:
    return bucket in MODERATE_BUCKETS


def is_success(bucket: Optional[str]) -> bool:
    return bucket in SUCCESS_BUCKETS


if __name__ == "__main__":
    # Smoke tests — run with `python breakout_path_outcomes.py`
    pivot = 100.0

    # Case 1: D1 reversal — high spikes to 103.5, closes at 99 (below pivot)
    bars = [
        {"open": 100.5, "high": 103.5, "low": 98.5, "close": 99.0},
        {"open": 99.0,  "high": 100.0, "low": 95.0, "close": 96.0},
        {"open": 96.0,  "high": 97.0,  "low": 94.0, "close": 95.0},
        {"open": 95.0,  "high": 96.0,  "low": 93.0, "close": 94.0},
        {"open": 94.0,  "high": 95.0,  "low": 92.0, "close": 93.0},
    ]
    r = classify_path_outcome(pivot, bars, 5)
    assert r["bucket"] == "failed_d1_reversal", r
    print("Case 1 (D1 reversal):", r["bucket"])

    # Case 2a: Pre-5% drop — CLOSE — Day 2 closes below pivot
    bars = [
        {"open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5},
        {"open": 101.5, "high": 102.5, "low": 99.0,  "close": 99.5},  # close < pivot
        {"open": 99.5,  "high": 101.0, "low": 98.0,  "close": 100.5},
        {"open": 100.5, "high": 101.0, "low": 99.0,  "close": 100.0},
        {"open": 100.0, "high": 101.0, "low": 99.0,  "close": 100.0},
    ]
    r = classify_path_outcome(pivot, bars, 5)
    assert r["bucket"] == "failed_pre_5pct_drop_close", r
    print("Case 2a (pre-5pct close):", r["bucket"])

    # Case 2b: Pre-5% drop — WICK — Day 2 low < pivot but close > pivot
    bars = [
        {"open": 100.5, "high": 102.0, "low": 100.0, "close": 101.5},
        {"open": 101.5, "high": 102.5, "low": 99.0,  "close": 101.0},  # wick only
        {"open": 101.0, "high": 102.0, "low": 100.0, "close": 101.5},
        {"open": 101.5, "high": 102.5, "low": 100.5, "close": 102.0},
        {"open": 102.0, "high": 103.0, "low": 101.0, "close": 102.5},
    ]
    r = classify_path_outcome(pivot, bars, 5)
    assert r["bucket"] == "failed_pre_5pct_drop_wick", r
    print("Case 2b (pre-5pct wick):", r["bucket"])

    # Case 3: No 5% in window — slow drift up but never touches 105
    bars = [{"open": 100.5, "high": 101.5, "low": 100.0, "close": 101.0}] * 5
    r = classify_path_outcome(pivot, bars, 5)
    assert r["bucket"] == "failed_no_5pct_in_window", r
    print("Case 3 (no 5pct):", r["bucket"])

    # Case 4: Moderate reversed — hit 105.5, closed at 102 (below 5to7 final 3% bar)
    # Wait: final = 102 → final_pct = 0.02, below 0.03. peak = 5.5%.
    # Tier check: peak >= 5%, final >= 3%? 0.02 < 0.03 → moderate_reversed.
    bars = [
        {"open": 100.5, "high": 103.0, "low": 100.0, "close": 102.5},
        {"open": 102.5, "high": 105.5, "low": 102.0, "close": 104.5},
        {"open": 104.5, "high": 105.0, "low": 102.0, "close": 103.0},
        {"open": 103.0, "high": 104.0, "low": 101.5, "close": 102.5},
        {"open": 102.5, "high": 103.0, "low": 101.0, "close": 102.0},
    ]
    r = classify_path_outcome(pivot, bars, 5)
    assert r["bucket"] == "moderate_reversed", r
    print("Case 4 (moderate):", r["bucket"])

    # Case 5: success_5_to_7 — peak 5.5%, final 4%
    bars = [
        {"open": 100.5, "high": 103.0, "low": 100.0, "close": 102.5},
        {"open": 102.5, "high": 105.5, "low": 102.0, "close": 104.5},
        {"open": 104.5, "high": 105.0, "low": 103.0, "close": 104.0},
        {"open": 104.0, "high": 104.5, "low": 103.5, "close": 104.0},
        {"open": 104.0, "high": 104.5, "low": 103.5, "close": 104.0},
    ]
    r = classify_path_outcome(pivot, bars, 5)
    assert r["bucket"] == "success_5_to_7", r
    print("Case 5 (success 5-7):", r["bucket"])

    # Case 6: success_7_to_10 — peak 8%, final 6%
    bars = [
        {"open": 100.5, "high": 103.0, "low": 100.0, "close": 102.5},
        {"open": 102.5, "high": 105.5, "low": 102.0, "close": 105.0},
        {"open": 105.0, "high": 108.0, "low": 104.5, "close": 107.0},
        {"open": 107.0, "high": 108.0, "low": 106.0, "close": 106.5},
        {"open": 106.5, "high": 107.0, "low": 105.5, "close": 106.0},
    ]
    r = classify_path_outcome(pivot, bars, 5)
    assert r["bucket"] == "success_7_to_10", r
    print("Case 6 (success 7-10):", r["bucket"])

    # Case 7: Strong success — peak 15%, final 12%
    bars = [
        {"open": 100.5, "high": 105.0, "low": 100.0, "close": 104.5},
        {"open": 104.5, "high": 110.0, "low": 104.0, "close": 109.5},
        {"open": 109.5, "high": 115.0, "low": 109.0, "close": 113.0},
        {"open": 113.0, "high": 114.0, "low": 111.0, "close": 112.5},
        {"open": 112.5, "high": 113.0, "low": 111.5, "close": 112.0},
    ]
    r = classify_path_outcome(pivot, bars, 5)
    assert r["bucket"] == "success_strong_gt_10", r
    print("Case 7 (success strong):", r["bucket"])

    # Case 8: Cascade — peak 12%, final 5.5% → falls from strong to 7_to_10
    bars = [
        {"open": 100.5, "high": 104.0, "low": 100.0, "close": 103.5},
        {"open": 103.5, "high": 108.0, "low": 103.0, "close": 107.5},
        {"open": 107.5, "high": 112.0, "low": 107.0, "close": 110.0},
        {"open": 110.0, "high": 110.0, "low": 105.0, "close": 105.5},
        {"open": 105.5, "high": 106.0, "low": 105.0, "close": 105.5},
    ]
    r = classify_path_outcome(pivot, bars, 5)
    assert r["bucket"] == "success_7_to_10", r
    print("Case 8 (peak strong, final 7-10):", r["bucket"])

    # Case 9: cfg override — same bars as case 1, but raise d1_spike to 5% so
    # the +3.5% spike no longer qualifies as a d1_reversal trigger.
    bars = [
        {"open": 100.5, "high": 103.5, "low": 98.5, "close": 99.0},
        {"open": 99.0,  "high": 100.0, "low": 95.0, "close": 96.0},
        {"open": 96.0,  "high": 97.0,  "low": 94.0, "close": 95.0},
        {"open": 95.0,  "high": 96.0,  "low": 93.0, "close": 94.0},
        {"open": 94.0,  "high": 95.0,  "low": 92.0, "close": 93.0},
    ]
    r = classify_path_outcome(pivot, bars, 5, cfg={"d1_spike_pct": 0.05})
    # No d1_reversal — falls to pre_5pct path. Day 1 close (99) < pivot (100) →
    # _close variant fires.
    assert r["bucket"] == "failed_pre_5pct_drop_close", r
    print("Case 9 (cfg override, d1_spike=5%):", r["bucket"])

    print("\nAll path-outcome smoke tests passed.")
