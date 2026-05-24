"""
daily_coach_service.py — Proactive end-of-day trade coaching.

Counterpart to rationale_service (which fires REACTIVELY after a bad close
with a "why?" question). This service runs once per trading day after
market close, joins the user's recent trade activity with that day's
market context (regime + breadth + index moves), and surfaces five
specific "leaks" with concrete next-day fixes. The whole report is
persisted into daily_coach_reports so the user can scroll history and
chart their leak score over time.

The five leaks (mirroring the diagnoses Valvo AI surfaced in chat):

  1. SIZING_INVERSION
     Bigger sizes on losers than on winners. Inverted because the
     statistically right thing is the opposite (or at least uniform).
     Computed over a rolling window (default last 20 closed trades).

  2. SL_BREACH
     Closed losers where realised loss % exceeded the planned SL %
     by more than `SL_BREACH_TOLERANCE_PCT`. "I broke my own rule."

  3. CONCURRENCY_OVERLOAD
     Days where the user had >= CONCURRENCY_THRESHOLD positions open
     simultaneously. Cluster-trading: when the market turns, every
     position gets hit at once.

  4. EARLY_EXIT_ON_WINNERS
     Winners closed where the stock kept rising materially after exit
     (>= EARLY_EXIT_RISE_PCT in the EARLY_EXIT_LOOKAHEAD_DAYS that
     followed). Proxy for "I sold before the trend broke."

  5. WINNER_CONCENTRATION
     Single-trade dependency — % of FY P&L from the top 1 / top 3
     trades. High concentration means edge is fragile.

Each leak returns: severity (low|medium|high), one-line headline, a
detailed paragraph, the evidence rows (so the UI can render a small
table), and a concrete one-sentence fix.

Market context is pulled from market_regime_history + breadth_daily_history
+ index_daily_summary (NIFTY 50 / NIFTY 500). The interpretation links
trade behaviour to tape: "you opened 3 new positions on a -1.2% NIFTY
day with breadth at 18% advancing — wrong tape for chasing momentum."

Failures are swallowed at the leak level — one broken detector must
not kill the whole report. We log and emit a short error in findings.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Optional

from database.database import get_db
from services.user_analytics_service import get_user_base_capital


# ──────────────────────────────────────────────────────────────────────
#  Tuning knobs — single source of truth for thresholds. The fixes
#  reference these so the user knows what number to aim for.
# ──────────────────────────────────────────────────────────────────────
RECENT_TRADES_WINDOW = 20          # rolling window of closed trades to analyse
SIZING_TARGET_PCT = 3.0            # the user's stated 3% uniform sizing rule
SIZING_HIGH_PCT = 6.0              # any single position >= this = "way oversized"
SL_BREACH_TOLERANCE_PCT = 0.5      # loss must exceed planned SL by >= 0.5pp to count
CONCURRENCY_THRESHOLD = 4          # >=4 simultaneous open positions = cluster
CONCURRENCY_LOOKBACK_DAYS = 14     # window for cluster detection
EARLY_EXIT_RISE_PCT = 5.0          # winner kept rising >=5% after exit
EARLY_EXIT_LOOKAHEAD_DAYS = 7      # within 7 trading days of exit
WINNER_CONC_TOP1_HIGH = 50.0       # top 1 trade >= 50% of FY P&L = high
WINNER_CONC_TOP3_HIGH = 80.0       # top 3 trades >= 80% of FY P&L = high


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────

def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _resolve_fy_for_date(d: date) -> str:
    """Indian financial year for a given date — Apr 1 to Mar 31.
    Returns 'YYYY-YY' e.g. 2026-05-04 -> '2026-27'."""
    if d.month >= 4:
        start = d.year
    else:
        start = d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _severity_from_score(score: int) -> str:
    if score >= 60:
        return "high"
    if score >= 30:
        return "medium"
    return "low"


# ──────────────────────────────────────────────────────────────────────
#  Data loaders
# ──────────────────────────────────────────────────────────────────────

def _load_recent_closed_trades(cur, user_id: str, fy: str, limit: int) -> list[dict]:
    """Pulls the most recent N closed positions, joined with planned SL %
    so we can compute breach. Uses positions table directly (the source
    of truth) rather than journal_trades_computed view to keep entry/SL
    fields canonical."""
    cur.execute(
        """
        SELECT id, stock_name, entry_price, stop_loss, exit_price,
               quantity, total_pnl, total_pnl_pct, entry_date, exit_date,
               current_r_multiple, total_cost_outlay,
               initial_entry_price, initial_qty
        FROM positions
        WHERE user_id = %s
          AND status = 'closed'
        ORDER BY exit_date DESC NULLS LAST
        LIMIT %s
        """,
        (user_id, limit),
    )
    rows = cur.fetchall() or []
    out = []
    for r in rows:
        entry = _safe_float(r.get("entry_price"))
        sl = _safe_float(r.get("stop_loss"))
        exit_p = _safe_float(r.get("exit_price"))
        qty = int(r.get("quantity") or 0)
        planned_sl_pct = ((entry - sl) / entry * 100.0) if (entry > 0 and sl > 0) else 0.0
        realised_pct = _safe_float(r.get("total_pnl_pct"))
        # Prefer the canonical cash-committed amount (total_cost_outlay tracks
        # pyramid additions). Fall back to initial entry × initial qty (pre-pyramid),
        # then to entry × current qty as last resort.
        outlay = _safe_float(r.get("total_cost_outlay"))
        if outlay > 0:
            buy_value = outlay
        else:
            i_entry = _safe_float(r.get("initial_entry_price")) or entry
            i_qty = int(r.get("initial_qty") or qty)
            buy_value = i_entry * i_qty if i_qty > 0 else entry * qty
        out.append({
            "id": int(r["id"]),
            "stock_name": r.get("stock_name"),
            "entry_price": entry,
            "stop_loss": sl,
            "exit_price": exit_p,
            "quantity": qty,
            "buy_value": round(buy_value, 2),
            "total_pnl": _safe_float(r.get("total_pnl")),
            "realised_pct": round(realised_pct, 2),
            "planned_sl_pct": round(planned_sl_pct, 2),
            "r_multiple": _safe_float(r.get("current_r_multiple")),
            "entry_date": r["entry_date"].isoformat() if r.get("entry_date") else None,
            "exit_date": r["exit_date"].isoformat() if r.get("exit_date") else None,
        })
    return out


def _load_open_positions(cur, user_id: str) -> list[dict]:
    cur.execute(
        """
        SELECT id, stock_name, entry_price, stop_loss, quantity,
               entry_date, current_price, current_r_multiple
        FROM positions
        WHERE user_id = %s AND status = 'active'
        ORDER BY entry_date DESC
        """,
        (user_id,),
    )
    return cur.fetchall() or []


def _load_recent_concurrency(cur, user_id: str, end_date: date, lookback: int) -> list[dict]:
    """For each calendar day in the window, how many positions were open?
    A position is 'open' on day D if entry_date <= D <= COALESCE(exit_date, today)."""
    cur.execute(
        """
        WITH day_series AS (
            SELECT generate_series(
                %s::date - make_interval(days => %s),
                %s::date,
                '1 day'::interval
            )::date AS d
        ),
        opens AS (
            SELECT id, entry_date, COALESCE(exit_date::date, %s::date) AS exit_d
            FROM positions
            WHERE user_id = %s
              AND entry_date IS NOT NULL
              AND entry_date <= %s::date
              AND COALESCE(exit_date::date, %s::date) >= (%s::date - make_interval(days => %s))::date
        )
        SELECT ds.d AS day,
               COUNT(o.id) AS open_count
        FROM day_series ds
        LEFT JOIN opens o
          ON ds.d BETWEEN o.entry_date AND o.exit_d
        GROUP BY ds.d
        ORDER BY ds.d
        """,
        (end_date, int(lookback), end_date, end_date, user_id, end_date, end_date, end_date, int(lookback)),
    )
    return cur.fetchall() or []


def _load_market_context(cur, on_date: date) -> dict:
    """Pull the market regime + NIFTY/NIFTY 500 moves + breadth for the day.
    Falls back gracefully if any piece is missing."""
    out: dict[str, Any] = {
        "regime": None, "regime_note": None,
        "indices": {}, "breadth": {},
        "interpretation": None,
    }

    try:
        cur.execute(
            "SELECT regime, note FROM market_regime_history "
            "WHERE updated_at::date <= %s ORDER BY updated_at DESC LIMIT 1",
            (on_date,),
        )
        r = cur.fetchone()
        if r:
            out["regime"] = r.get("regime")
            out["regime_note"] = r.get("note")
    except Exception as exc:
        print(f"[coach] market_regime fetch failed: {exc}")

    try:
        cur.execute(
            """
            SELECT symbol, prev_close, return_5d, return_20d, close_5d
            FROM index_daily_summary
            WHERE symbol IN ('NIFTY 50','NIFTY 500','NIFTY MIDCAP 100','NIFTY SMALLCAP 100')
              AND computed_date <= %s
            ORDER BY computed_date DESC
            LIMIT 8
            """,
            (on_date,),
        )
        seen: set[str] = set()
        for row in cur.fetchall() or []:
            sym = row.get("symbol")
            if sym in seen:
                continue
            seen.add(sym)
            out["indices"][sym] = {
                "prev_close": _safe_float(row.get("prev_close")),
                "return_5d_pct": round(_safe_float(row.get("return_5d")) * 100.0, 2)
                    if row.get("return_5d") is not None and abs(_safe_float(row.get("return_5d"))) < 5
                    else round(_safe_float(row.get("return_5d")), 2),
                "return_20d_pct": round(_safe_float(row.get("return_20d")) * 100.0, 2)
                    if row.get("return_20d") is not None and abs(_safe_float(row.get("return_20d"))) < 5
                    else round(_safe_float(row.get("return_20d")), 2),
            }
    except Exception as exc:
        print(f"[coach] index_daily_summary fetch failed: {exc}")

    try:
        cur.execute(
            """
            SELECT date, total_stocks, pct_above_ema20, pct_above_ema50, pct_above_ema200,
                   advance_count, decline_count, new_highs, new_lows, thrust, momentum_20pc
            FROM breadth_daily_history
            WHERE date <= %s
            ORDER BY date DESC
            LIMIT 1
            """,
            (on_date,),
        )
        r = cur.fetchone()
        if r:
            adv = int(r.get("advance_count") or 0)
            dec = int(r.get("decline_count") or 0)
            ad_ratio = (adv / dec) if dec > 0 else None
            out["breadth"] = {
                "as_of": r["date"].isoformat() if r.get("date") else None,
                "total_stocks": int(r.get("total_stocks") or 0),
                "advance_count": adv, "decline_count": dec,
                "ad_ratio": round(ad_ratio, 2) if ad_ratio is not None else None,
                "pct_above_ema20": round(_safe_float(r.get("pct_above_ema20")), 1),
                "pct_above_ema50": round(_safe_float(r.get("pct_above_ema50")), 1),
                "pct_above_ema200": round(_safe_float(r.get("pct_above_ema200")), 1),
                "new_highs": int(r.get("new_highs") or 0),
                "new_lows": int(r.get("new_lows") or 0),
                "thrust": _safe_float(r.get("thrust")),
                "momentum_20pc": int(r.get("momentum_20pc") or 0),
            }
    except Exception as exc:
        print(f"[coach] breadth fetch failed: {exc}")

    out["interpretation"] = _interpret_market(out)
    return out


def _interpret_market(market: dict) -> str:
    regime = (market.get("regime") or "").strip()
    breadth = market.get("breadth") or {}
    pct_above_20 = breadth.get("pct_above_ema20") or 0
    ad_ratio = breadth.get("ad_ratio")

    pieces: list[str] = []
    if regime:
        pieces.append(f"Regime: {regime}.")
    if pct_above_20:
        if pct_above_20 >= 60:
            pieces.append(f"Breadth strong — {pct_above_20:.0f}% of stocks above 20EMA.")
        elif pct_above_20 >= 40:
            pieces.append(f"Breadth mixed — {pct_above_20:.0f}% above 20EMA.")
        else:
            pieces.append(f"Breadth weak — only {pct_above_20:.0f}% above 20EMA. Caution chasing momentum.")
    if ad_ratio is not None:
        if ad_ratio >= 1.5:
            pieces.append(f"A/D ratio {ad_ratio} — broad participation.")
        elif ad_ratio < 0.7:
            pieces.append(f"A/D ratio {ad_ratio} — declines dominating.")
    return " ".join(pieces) or "Market context unavailable."


def _load_top_rationale_tags(cur, user_id: str, days: int = 30) -> list[str]:
    """Top 3 tags from rationale_service prompts in last N days. Lets the coach
    cite recurring behavioural patterns the user has self-reported."""
    try:
        cur.execute(
            """
            SELECT extracted_tags
            FROM trade_rationale_prompts
            WHERE user_id = %s AND status = 'answered'
              AND answered_at > NOW() - make_interval(days => %s)
            """,
            (user_id, int(days)),
        )
        rows = cur.fetchall() or []
        counts: dict[str, int] = {}
        for r in rows:
            tags = r.get("extracted_tags") or []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            for t in tags:
                if isinstance(t, str):
                    counts[t] = counts.get(t, 0) + 1
        return [t for t, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:3]]
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────
#  Leak detectors — each returns one finding dict (or None to skip).
# ──────────────────────────────────────────────────────────────────────

def _leak_sizing_inversion(trades: list[dict], base_capital: float) -> Optional[dict]:
    """Compute size% (buy_value / base_capital * 100) per trade.
    Compare avg size of winners vs losers. If losers' avg size >
    winners' avg size, that's inversion."""
    if base_capital <= 0 or len(trades) < 4:
        return None
    sized = []
    for t in trades:
        size_pct = (t["buy_value"] / base_capital) * 100.0 if t["buy_value"] else 0.0
        sized.append({
            **t,
            "size_pct": round(size_pct, 2),
            "is_winner": t["total_pnl"] > 0,
        })
    winners = [t for t in sized if t["is_winner"]]
    losers = [t for t in sized if not t["is_winner"]]
    if not winners or not losers:
        return None

    avg_winner_size = sum(t["size_pct"] for t in winners) / len(winners)
    avg_loser_size = sum(t["size_pct"] for t in losers) / len(losers)
    inversion_ratio = (avg_loser_size / avg_winner_size) if avg_winner_size > 0 else 1.0

    over_target = [t for t in sized if t["size_pct"] >= SIZING_HIGH_PCT]
    over_target.sort(key=lambda t: t["size_pct"], reverse=True)

    if inversion_ratio >= 1.4 or len(over_target) >= 2:
        severity = "high" if inversion_ratio >= 1.6 or len(over_target) >= 3 else "medium"
    elif inversion_ratio >= 1.15:
        severity = "low"
    else:
        return {
            "key": "sizing_inversion",
            "severity": "low",
            "headline": (
                f"Sizing balanced — winners avg {avg_winner_size:.1f}% vs losers "
                f"{avg_loser_size:.1f}% (ratio {inversion_ratio:.2f}). Keep it tight."
            ),
            "detail": "No inversion detected in the last "
                     f"{len(sized)} closed trades.",
            "evidence": [],
            "fix": f"Stay disciplined at {SIZING_TARGET_PCT:.0f}% per trade.",
            "metrics": {
                "avg_winner_size_pct": round(avg_winner_size, 2),
                "avg_loser_size_pct": round(avg_loser_size, 2),
                "inversion_ratio": round(inversion_ratio, 2),
                "trades_analyzed": len(sized),
            },
        }

    headline = (
        f"You're sizing {inversion_ratio:.1f}× bigger on losers than on winners "
        f"({avg_loser_size:.1f}% vs {avg_winner_size:.1f}% of capital)."
    )
    detail_parts = [
        f"Across the last {len(sized)} closed trades: winners averaged "
        f"{avg_winner_size:.1f}% of capital, losers averaged {avg_loser_size:.1f}%."
    ]
    if over_target:
        names = ", ".join(f"{t['stock_name']} ({t['size_pct']:.1f}%)" for t in over_target[:3])
        detail_parts.append(
            f"{len(over_target)} trade(s) breached the {SIZING_HIGH_PCT:.0f}% ceiling — top: {names}."
        )
    detail = " ".join(detail_parts)

    fix = (
        f"Size every new entry at exactly {SIZING_TARGET_PCT:.0f}% of capital. "
        f"No conviction override above {SIZING_HIGH_PCT:.0f}%."
    )

    return {
        "key": "sizing_inversion",
        "severity": severity,
        "headline": headline,
        "detail": detail,
        "evidence": [
            {
                "stock_name": t["stock_name"],
                "size_pct": t["size_pct"],
                "realised_pct": t["realised_pct"],
                "total_pnl": t["total_pnl"],
                "exit_date": t["exit_date"],
                "is_winner": t["is_winner"],
            }
            for t in sorted(sized, key=lambda x: x["size_pct"], reverse=True)[:8]
        ],
        "fix": fix,
        "metrics": {
            "avg_winner_size_pct": round(avg_winner_size, 2),
            "avg_loser_size_pct": round(avg_loser_size, 2),
            "inversion_ratio": round(inversion_ratio, 2),
            "trades_over_high_pct": len(over_target),
            "trades_analyzed": len(sized),
        },
    }


def _leak_sl_breach(trades: list[dict]) -> Optional[dict]:
    """A loss is a breach if the realised loss % exceeded the planned SL %
    by more than SL_BREACH_TOLERANCE_PCT."""
    losers = [t for t in trades if t["total_pnl"] < 0 and t["planned_sl_pct"] > 0]
    if not losers:
        return None
    breaches: list[dict] = []
    for t in losers:
        # realised_pct is negative for losers; planned_sl_pct is the magnitude.
        realised_loss_pct = abs(t["realised_pct"])
        breach_by = realised_loss_pct - t["planned_sl_pct"]
        if breach_by >= SL_BREACH_TOLERANCE_PCT:
            breaches.append({**t, "breach_by_pct": round(breach_by, 2)})

    breach_rate = len(breaches) / len(losers) * 100.0 if losers else 0
    if not breaches:
        return {
            "key": "sl_breach",
            "severity": "low",
            "headline": (
                f"Stop-loss discipline is clean — 0/{len(losers)} losers breached the "
                "planned SL in the recent window."
            ),
            "detail": "No stop-loss breaches detected.",
            "evidence": [],
            "fix": "Keep cutting at the planned SL — no exceptions.",
            "metrics": {"breach_count": 0, "loser_count": len(losers), "breach_rate_pct": 0},
        }

    severity = "high" if breach_rate >= 50 or len(breaches) >= 3 else "medium" if breach_rate >= 25 else "low"
    breaches.sort(key=lambda x: x["breach_by_pct"], reverse=True)
    worst = breaches[0]
    headline = (
        f"{len(breaches)}/{len(losers)} recent losers blew past the planned SL — "
        f"worst was {worst['stock_name']} at {abs(worst['realised_pct']):.2f}% (planned {worst['planned_sl_pct']:.2f}%)."
    )
    detail = (
        f"Average breach magnitude: {sum(b['breach_by_pct'] for b in breaches)/len(breaches):.2f} percentage points "
        f"beyond the stated SL. That's the difference between a 1R loss and a 1.5–2R loss."
    )
    fix = (
        "Set a hard EOD review: any open position trading below SL at close must be "
        "exited the next session open. No 'one more day to recover'."
    )
    return {
        "key": "sl_breach",
        "severity": severity,
        "headline": headline,
        "detail": detail,
        "evidence": [
            {
                "stock_name": b["stock_name"],
                "planned_sl_pct": b["planned_sl_pct"],
                "realised_pct": b["realised_pct"],
                "breach_by_pct": b["breach_by_pct"],
                "total_pnl": b["total_pnl"],
                "exit_date": b["exit_date"],
            }
            for b in breaches[:5]
        ],
        "fix": fix,
        "metrics": {
            "breach_count": len(breaches),
            "loser_count": len(losers),
            "breach_rate_pct": round(breach_rate, 1),
        },
    }


def _leak_concurrency(daily_counts: list[dict]) -> Optional[dict]:
    if not daily_counts:
        return None
    cluster_days = [
        {"day": (r["day"].isoformat() if hasattr(r["day"], "isoformat") else str(r["day"])),
         "open_count": int(r["open_count"])}
        for r in daily_counts if int(r["open_count"]) >= CONCURRENCY_THRESHOLD
    ]
    max_count = max((int(r["open_count"]) for r in daily_counts), default=0)

    if not cluster_days:
        return {
            "key": "concurrency_overload",
            "severity": "low",
            "headline": (
                f"Concurrent positions stayed under {CONCURRENCY_THRESHOLD} all "
                f"{len(daily_counts)} days — no cluster trading."
            ),
            "detail": f"Peak was {max_count} concurrent open positions in the lookback window.",
            "evidence": [],
            "fix": f"Hold the line at max 3 concurrent positions until you're on a winning streak.",
            "metrics": {"cluster_day_count": 0, "max_concurrent": max_count, "lookback_days": len(daily_counts)},
        }

    severity = "high" if len(cluster_days) >= 4 or max_count >= 6 else "medium"
    headline = (
        f"{len(cluster_days)} days in the last {len(daily_counts)} had ≥{CONCURRENCY_THRESHOLD} "
        f"concurrent open positions (peak {max_count}). When the tape turns, every name gets hit at once."
    )
    detail = (
        "Cluster trading is the silent killer of momentum portfolios — drawdowns compound when "
        "positions are correlated by sector or simply by 'risk-on'. Enforce a cap."
    )
    fix = (
        f"Cap concurrent positions at {CONCURRENCY_THRESHOLD - 1}. Want to add #{CONCURRENCY_THRESHOLD}? "
        "Close one of the weakest first."
    )
    return {
        "key": "concurrency_overload",
        "severity": severity,
        "headline": headline,
        "detail": detail,
        "evidence": cluster_days[-10:],  # last 10 cluster days, chronological
        "fix": fix,
        "metrics": {
            "cluster_day_count": len(cluster_days),
            "max_concurrent": max_count,
            "lookback_days": len(daily_counts),
        },
    }


def _leak_early_exit(cur, trades: list[dict]) -> Optional[dict]:
    """For each winner closed in the recent window, look up the stock's
    candles_daily for the EARLY_EXIT_LOOKAHEAD_DAYS days after exit. If
    the stock kept rising >= EARLY_EXIT_RISE_PCT, count as early exit.

    Falls back silently if candles_daily lookup fails (it might not have
    coverage for every symbol)."""
    winners = [t for t in trades if t["total_pnl"] > 0 and t.get("exit_date") and t.get("exit_price", 0) > 0]
    if not winners:
        return None

    early_exits: list[dict] = []
    for t in winners:
        try:
            # Resolve symbol — positions table doesn't carry symbol, so we
            # need to look it up via journal_trades (linked by stock_name + entry_date)
            # OR query candles_daily directly by stock_name → security_id.
            # Use a simple lookup via positions.security_id if present.
            cur.execute(
                """
                SELECT security_id FROM positions WHERE id = %s
                """,
                (t["id"],),
            )
            row = cur.fetchone()
            sec_id = row.get("security_id") if row else None
            if not sec_id:
                continue
            cur.execute(
                """
                SELECT MAX(high) AS peak_high
                FROM candles_daily
                WHERE security_id = %s
                  AND date > %s::date
                  AND date <= %s::date + make_interval(days => %s)
                """,
                (str(sec_id), t["exit_date"], t["exit_date"], int(EARLY_EXIT_LOOKAHEAD_DAYS)),
            )
            r = cur.fetchone()
            peak = _safe_float((r or {}).get("peak_high"))
            if peak <= 0 or t["exit_price"] <= 0:
                continue
            rise_pct = (peak - t["exit_price"]) / t["exit_price"] * 100.0
            if rise_pct >= EARLY_EXIT_RISE_PCT:
                early_exits.append({
                    "stock_name": t["stock_name"],
                    "exit_price": t["exit_price"],
                    "peak_after": round(peak, 2),
                    "rise_after_pct": round(rise_pct, 2),
                    "exit_date": t["exit_date"],
                    "you_made_pct": t["realised_pct"],
                })
        except Exception as exc:
            # Skip silently — candles coverage gaps are expected
            print(f"[coach] early-exit lookup failed for {t.get('stock_name')}: {exc}")
            continue

    if not early_exits:
        return None  # not enough data, hide leak entirely

    early_exits.sort(key=lambda x: x["rise_after_pct"], reverse=True)
    avg_left = sum(e["rise_after_pct"] for e in early_exits) / len(early_exits)
    severity = "high" if len(early_exits) >= 3 or avg_left >= 15 else "medium" if len(early_exits) >= 2 else "low"
    top = early_exits[0]
    headline = (
        f"{len(early_exits)}/{len(winners)} recent winners kept running after your exit — "
        f"top miss: {top['stock_name']} +{top['rise_after_pct']:.1f}% in the {EARLY_EXIT_LOOKAHEAD_DAYS} days "
        "post-exit."
    )
    detail = (
        f"Average rise-after-exit on these winners: {avg_left:.1f}%. Your trailing rule "
        "(5MA close) didn't break — you exited on noise, not signal."
    )
    fix = (
        "Stop using mental stops on winners. Set a hard rule: exit only when the daily close "
        "is below the 5MA. No 'looks tired' exits."
    )
    return {
        "key": "early_exit_on_winners",
        "severity": severity,
        "headline": headline,
        "detail": detail,
        "evidence": early_exits[:5],
        "fix": fix,
        "metrics": {
            "early_exit_count": len(early_exits),
            "winner_count": len(winners),
            "avg_rise_after_pct": round(avg_left, 2),
            "lookahead_days": EARLY_EXIT_LOOKAHEAD_DAYS,
        },
    }


def _leak_winner_concentration(cur, user_id: str, fy: str) -> Optional[dict]:
    """% of FY net P&L coming from top 1 / top 3 trades."""
    try:
        cur.execute(
            """
            SELECT id, stock_name, total_pnl
            FROM positions
            WHERE user_id = %s AND status = 'closed' AND total_pnl IS NOT NULL
            ORDER BY total_pnl DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall() or []
    except Exception:
        return None

    if len(rows) < 5:
        return None

    pnls = [_safe_float(r["total_pnl"]) for r in rows]
    gross_winners = sum(p for p in pnls if p > 0)
    if gross_winners <= 0:
        return None

    top1 = pnls[0]
    top3 = sum(pnls[:3])
    top1_pct = top1 / gross_winners * 100.0
    top3_pct = top3 / gross_winners * 100.0

    if top1_pct >= WINNER_CONC_TOP1_HIGH or top3_pct >= WINNER_CONC_TOP3_HIGH:
        severity = "high"
    elif top1_pct >= 35 or top3_pct >= 65:
        severity = "medium"
    else:
        severity = "low"

    top_names = ", ".join(f"{r['stock_name']} (₹{_safe_float(r['total_pnl'])/100000:.2f}L)" for r in rows[:3])
    headline = (
        f"Top trade = {top1_pct:.1f}% of gross profit; top 3 = {top3_pct:.1f}%. "
        f"Edge depends on a handful of names ({top_names})."
    )
    if severity == "low":
        detail = "Edge is well-distributed across multiple winners — not over-reliant on any single trade."
        fix = "Keep doing what's working — repeatable setups, not one home run."
    else:
        detail = (
            "Heavy reliance on outliers means one missed setup or one premature exit "
            "wipes out the edge. The way to fix this is to compound — don't sell winners early "
            "and let pyramid scaling enlarge the second/third winners too."
        )
        fix = "Hold winners longer and pyramid into strength. Don't compress the right tail."

    return {
        "key": "winner_concentration",
        "severity": severity,
        "headline": headline,
        "detail": detail,
        "evidence": [
            {"stock_name": r["stock_name"], "total_pnl": _safe_float(r["total_pnl"])}
            for r in rows[:5]
        ],
        "fix": fix,
        "metrics": {
            "top1_pct_of_gross_winners": round(top1_pct, 1),
            "top3_pct_of_gross_winners": round(top3_pct, 1),
            "total_closed_trades": len(rows),
            "gross_winners": round(gross_winners, 2),
        },
    }


# ──────────────────────────────────────────────────────────────────────
#  Adherence streaks — counts of consecutive days without each leak.
#  Cheap (one query over recent reports) and powerful for motivation.
# ──────────────────────────────────────────────────────────────────────

def _compute_adherence_streak(cur, user_id: str, end_date: date, current_findings: dict) -> dict:
    """Look back through previous reports and count consecutive days where
    each leak was severity != 'high'. Today's report extends or breaks
    the streak."""
    try:
        cur.execute(
            """
            SELECT report_date, findings
            FROM daily_coach_reports
            WHERE user_id = %s AND report_date < %s
            ORDER BY report_date DESC
            LIMIT 30
            """,
            (user_id, end_date),
        )
        prior = cur.fetchall() or []
    except Exception:
        prior = []

    leak_keys = ["sizing_inversion", "sl_breach", "concurrency_overload",
                 "early_exit_on_winners", "winner_concentration"]
    sev_today = {l["key"]: l.get("severity", "low") for l in current_findings.get("leaks", [])}

    streaks: dict[str, int] = {}
    for k in leak_keys:
        if sev_today.get(k) == "high":
            streaks[k] = 0
            continue
        days = 1
        for row in prior:
            f = row.get("findings") or {}
            if isinstance(f, str):
                try:
                    f = json.loads(f)
                except Exception:
                    f = {}
            sev = next((l.get("severity") for l in f.get("leaks", []) if l.get("key") == k), "low")
            if sev == "high":
                break
            days += 1
        streaks[k] = days
    return streaks


# ──────────────────────────────────────────────────────────────────────
#  Trades window summary
# ──────────────────────────────────────────────────────────────────────

def _trades_window_summary(cur, user_id: str, end_date: date, days: int = 7) -> dict:
    start = end_date - timedelta(days=days)
    try:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE exit_date::date BETWEEN %s AND %s) AS closed_count,
              COUNT(*) FILTER (WHERE entry_date BETWEEN %s AND %s) AS opened_count,
              COUNT(*) FILTER (WHERE exit_date::date BETWEEN %s AND %s AND total_pnl > 0) AS wins,
              COUNT(*) FILTER (WHERE exit_date::date BETWEEN %s AND %s AND total_pnl < 0) AS losses,
              COALESCE(SUM(total_pnl) FILTER (WHERE exit_date::date BETWEEN %s AND %s), 0) AS net_pnl
            FROM positions
            WHERE user_id = %s
            """,
            (start, end_date, start, end_date, start, end_date,
             start, end_date, start, end_date, user_id),
        )
        r = cur.fetchone() or {}
        closed = int(r.get("closed_count") or 0)
        wins = int(r.get("wins") or 0)
        losses = int(r.get("losses") or 0)
        decided = wins + losses
        win_rate = round(wins / decided * 100.0, 1) if decided > 0 else None
        return {
            "days": days,
            "trades_closed": closed,
            "trades_opened": int(r.get("opened_count") or 0),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
            "net_pnl": round(_safe_float(r.get("net_pnl")), 2),
        }
    except Exception as exc:
        print(f"[coach] trades_window_summary failed: {exc}")
        return {"days": days, "trades_closed": 0, "trades_opened": 0,
                "wins": 0, "losses": 0, "win_rate_pct": None, "net_pnl": 0}


# ──────────────────────────────────────────────────────────────────────
#  Leak score — single 0..100 number summarising how leaky today is.
# ──────────────────────────────────────────────────────────────────────

SEVERITY_WEIGHT = {"high": 25, "medium": 12, "low": 0}

def _compute_leak_score(leaks: list[dict]) -> int:
    if not leaks:
        return 0
    total = sum(SEVERITY_WEIGHT.get(l.get("severity", "low"), 0) for l in leaks)
    return min(100, max(0, total))


# ──────────────────────────────────────────────────────────────────────
#  Main entry point
# ──────────────────────────────────────────────────────────────────────

def build_report(cur, user_id: str, on_date: Optional[date] = None) -> dict:
    """Compute the daily coach report for `user_id` for the given date.
    Returns the findings dict (does NOT persist)."""
    on_date = on_date or date.today()
    fy = _resolve_fy_for_date(on_date)

    base_capital = get_user_base_capital(cur, user_id, fy) or 0.0

    trades = _load_recent_closed_trades(cur, user_id, fy, RECENT_TRADES_WINDOW)
    open_positions = _load_open_positions(cur, user_id)
    daily_counts = _load_recent_concurrency(cur, user_id, on_date, CONCURRENCY_LOOKBACK_DAYS)
    market = _load_market_context(cur, on_date)
    tag_carryover = _load_top_rationale_tags(cur, user_id, days=30)
    window = _trades_window_summary(cur, user_id, on_date, days=7)

    leaks: list[dict] = []
    for fn, args in [
        (_leak_sizing_inversion, (trades, base_capital)),
        (_leak_sl_breach, (trades,)),
        (_leak_concurrency, (daily_counts,)),
        (_leak_early_exit, (cur, trades)),
        (_leak_winner_concentration, (cur, user_id, fy)),
    ]:
        try:
            res = fn(*args)
            if res is not None:
                leaks.append(res)
        except Exception as exc:
            print(f"[coach] leak {fn.__name__} failed: {exc}")
            leaks.append({
                "key": fn.__name__.replace("_leak_", ""),
                "severity": "low",
                "headline": "Detector errored — see logs.",
                "detail": str(exc),
                "evidence": [], "fix": "", "metrics": {},
            })

    findings = {
        "leaks": leaks,
        "market": market,
        "trades_window": window,
        "open_positions_count": len(open_positions),
        "tag_carryover": tag_carryover,
        "base_capital": base_capital,
        "computed_at": datetime.utcnow().isoformat() + "Z",
    }

    leak_score = _compute_leak_score(leaks)
    adherence = _compute_adherence_streak(cur, user_id, on_date, findings)

    return {
        "report_date": on_date.isoformat(),
        "fy": fy,
        "leak_score": leak_score,
        "adherence_streak": adherence,
        "findings": findings,
    }


def upsert_report(cur, user_id: str, report: dict) -> int:
    """Persist the report. Returns the row id."""
    cur.execute(
        """
        INSERT INTO daily_coach_reports
            (user_id, report_date, fy, schema_version, findings, leak_score, adherence_streak)
        VALUES (%s, %s, %s, 1, %s, %s, %s)
        ON CONFLICT (user_id, report_date, fy)
        DO UPDATE SET findings = EXCLUDED.findings,
                      leak_score = EXCLUDED.leak_score,
                      adherence_streak = EXCLUDED.adherence_streak,
                      updated_at = NOW()
        RETURNING id
        """,
        (
            user_id,
            report["report_date"],
            report["fy"],
            json.dumps(report["findings"]),
            int(report["leak_score"]),
            json.dumps(report["adherence_streak"]),
        ),
    )
    return int(cur.fetchone()["id"])


def run_and_persist(user_id: str, on_date: Optional[date] = None) -> dict:
    """Convenience wrapper — opens a connection, builds, persists, closes.
    Used by the cron route. Returns the report dict + persisted id."""
    conn = get_db()
    if not conn:
        return {"error": "database unavailable"}
    try:
        cur = conn.cursor()
        report = build_report(cur, user_id, on_date)
        rid = upsert_report(cur, user_id, report)
        conn.commit()
        report["id"] = rid
        return report
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def fetch_report(cur, user_id: str, on_date: Optional[date] = None) -> Optional[dict]:
    """Most-recent report for the user on/before `on_date`."""
    on_date = on_date or date.today()
    cur.execute(
        """
        SELECT id, report_date, fy, schema_version, findings, leak_score,
               adherence_streak, acknowledged_at, notes, created_at, updated_at
        FROM daily_coach_reports
        WHERE user_id = %s AND report_date <= %s
        ORDER BY report_date DESC
        LIMIT 1
        """,
        (user_id, on_date),
    )
    r = cur.fetchone()
    if not r:
        return None
    findings = r["findings"]
    if isinstance(findings, str):
        try:
            findings = json.loads(findings)
        except Exception:
            findings = {}
    streak = r["adherence_streak"]
    if isinstance(streak, str):
        try:
            streak = json.loads(streak)
        except Exception:
            streak = {}
    return {
        "id": int(r["id"]),
        "report_date": r["report_date"].isoformat() if r.get("report_date") else None,
        "fy": r["fy"],
        "schema_version": int(r.get("schema_version") or 1),
        "leak_score": int(r["leak_score"]),
        "findings": findings,
        "adherence_streak": streak,
        "acknowledged_at": r["acknowledged_at"].isoformat() if r.get("acknowledged_at") else None,
        "notes": r.get("notes"),
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
    }


def fetch_history(cur, user_id: str, days: int = 30) -> list[dict]:
    """Trend data: latest N daily reports (date + leak_score + key counts)."""
    cur.execute(
        """
        SELECT report_date, leak_score, findings
        FROM daily_coach_reports
        WHERE user_id = %s
          AND report_date > (CURRENT_DATE - make_interval(days => %s))::date
        ORDER BY report_date ASC
        """,
        (user_id, int(days)),
    )
    out = []
    for r in cur.fetchall() or []:
        f = r["findings"]
        if isinstance(f, str):
            try:
                f = json.loads(f)
            except Exception:
                f = {}
        leaks = f.get("leaks", []) if isinstance(f, dict) else []
        high_count = sum(1 for l in leaks if l.get("severity") == "high")
        out.append({
            "date": r["report_date"].isoformat(),
            "leak_score": int(r["leak_score"]),
            "high_severity_count": high_count,
        })
    return out
