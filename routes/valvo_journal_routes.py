"""
Valvo Journal Analytics Routes
- /api/equity-curve/full — FY 2025-26 equity curve from legacy_monthly_summary
- /api/equity-curve/long-term — Multi-FY equity curve
- /api/equity-curve/smallcap-benchmark — NIFTY SMALLCAP 250 comparison
- /api/trade-history — Per-FY trade history
- /api/monthly-pl/full — Monthly P&L breakdown
- /api/analytics/trade-stats — Win rate / avg winner / etc by period
- /api/analytics/full — Full analytics bundle
- /api/analytics/drawdown-deep — Drawdown periods with trade attribution
"""
from flask import Blueprint, request, jsonify, g
import json
from extensions import limiter

valvo_journal_bp = Blueprint("valvo_journal", __name__)


def _get_db():
    from database.database import get_db
    return get_db()

def _close_db(conn):
    from database.database import close_db
    close_db(conn)


@valvo_journal_bp.route("/api/equity-curve/full", methods=["GET"])
@limiter.limit("60 per minute")
def full_equity_curve():
    """Equity curve from legacy_monthly_summary — all 12 months FY 2025-26.
    Starting capital from user_fy_config."""
    from services.user_analytics_service import get_user_base_capital
    conn = _get_db()
    try:
        cur = conn.cursor()
        base = get_user_base_capital(cur, g.user_id, "2025-26")
        if base is None:
            return jsonify({"error": "setup_required", "message": "Set your FY 2025-26 base capital first"}), 400
        base_capital = int(base)

        month_dates = {
            1:"2025-04-30", 2:"2025-05-31", 3:"2025-06-30", 4:"2025-07-31",
            5:"2025-08-31", 6:"2025-09-30", 7:"2025-10-31", 8:"2025-11-30",
            9:"2025-12-31", 10:"2026-01-31", 11:"2026-02-28", 12:"2026-03-31",
        }

        cur.execute("""
            SELECT month_label, month_order, after_charges
            FROM legacy_monthly_summary ORDER BY month_order
        """)
        legacy_months = cur.fetchall()

        points = [{"date": "2025-04-01", "label": "FY Start", "pnl": 0, "cumm_pnl": 0, "source": "start", "details": []}]

        # Compounding: multiply (1 + monthly%) to get cumulative
        portfolio = float(base_capital)
        for m in legacy_months:
            pct = m["after_charges"] or 0
            month_pl = round((pct / 100) * portfolio)
            portfolio += month_pl
            cumm_pct = round((portfolio / base_capital - 1) * 100, 2)
            points.append({
                "date": month_dates.get(m["month_order"], "2026-03-31"),
                "label": m["month_label"], "pnl": month_pl, "cumm_pnl": round(portfolio - base_capital),
                "cumm_pct": cumm_pct, "source": "legacy",
                "details": [{"name": m["month_label"], "pl": month_pl}],
            })

        final = points[-1]["cumm_pnl"] if points else 0
        peak = max(p["cumm_pnl"] for p in points) if points else 0
        running_peak = 0
        max_dd = 0
        for p in points:
            if p["cumm_pnl"] > running_peak:
                running_peak = p["cumm_pnl"]
            dd = p["cumm_pnl"] - running_peak
            if dd < max_dd:
                max_dd = dd

        # Per-user trade count and win count. legacy_trades has a user_id
        # column; unfiltered counts expose every other user's total activity.
        cur.execute("SELECT COUNT(*) as cnt FROM legacy_trades WHERE user_id = %s", (g.user_id,))
        total_trades = cur.fetchone()["cnt"]
        cur.execute(
            "SELECT COUNT(*) as cnt FROM legacy_trades WHERE is_winner AND user_id = %s",
            (g.user_id,),
        )
        total_wins = cur.fetchone()["cnt"]
        win_rate = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0

        return jsonify({
            "base_capital": base_capital,
            "points": points,
            "summary": {
                "final_pnl": final,
                "final_capital": base_capital + final,
                "final_return_pct": round(final / base_capital * 100, 2),
                "peak_pnl": peak,
                "max_dd": round(max_dd),
                "max_dd_pct": round(max_dd / base_capital * 100, 2) if base_capital else 0,
                "total_trades": total_trades,
                "total_wins": total_wins,
                "win_rate": win_rate,
            }
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[valvo-journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# ─── Helpers for portfolio_capital_log-sourced FYs (2026-27+) ───
# These adapt per-event log rows into the same {month_label, month_order,
# after_charges} shape that legacy_monthly_* tables produce, so the long-term
# equity curve renderer below can treat them identically. `after_charges`
# here is GROSS monthly % (no STCG/LTCG tax applied yet) — mirrors the field
# name even though legacy semantics deduct charges. Tax handling for the
# current FY is a follow-up.

_FY_MONTH_LABELS = ["Apr", "May", "Jun", "Jul", "Aug", "Sep",
                    "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]


def _months_from_log(cur, user_id, fy: str, base_capital: float) -> list[dict]:
    """Aggregate portfolio_capital_log realized PnL by FY month and return
    rows shaped like legacy_monthly_*. Stops at the current month — no point
    plotting flat zero-PnL points for future months."""
    try:
        start_year = int(fy.split("-")[0])
    except (ValueError, IndexError):
        return []

    cur.execute(
        """
        SELECT date_trunc('month', event_date)::date AS m,
               COALESCE(SUM(realized_pnl), 0)::numeric AS pnl
        FROM portfolio_capital_log
        WHERE user_id = %s AND fy = %s
        GROUP BY m
        ORDER BY m
        """,
        (user_id, fy),
    )
    pnl_by_month = {row["m"]: float(row["pnl"] or 0) for row in cur.fetchall()}

    from datetime import date
    today = date.today()
    months: list[dict] = []
    running = float(base_capital or 0)

    fy_months = [(4, start_year), (5, start_year), (6, start_year),
                 (7, start_year), (8, start_year), (9, start_year),
                 (10, start_year), (11, start_year), (12, start_year),
                 (1, start_year + 1), (2, start_year + 1), (3, start_year + 1)]

    for i, (mo, yr) in enumerate(fy_months, start=1):
        # Skip future months — they pollute the chart with flat zeros and
        # make it look like the strategy stalled.
        if (yr, mo) > (today.year, today.month):
            break
        key = date(yr, mo, 1)
        pnl = pnl_by_month.get(key, 0.0)
        pct = (pnl / running * 100.0) if running > 0 else 0.0
        running += pnl
        months.append({
            "month_label": _FY_MONTH_LABELS[i - 1],
            "month_order": i,
            "after_charges": round(pct, 4),
            "net_pf_impact": round(pct, 4),
            "charges": 0,
        })
    return months


def _daily_log_events(cur, user_id, fy: str) -> list[dict]:
    """One row per day with realized PnL. Used to give the current-FY equity
    curve daily resolution instead of just monthly aggregates (the legacy
    monthly_table approach is fine for closed FYs, but the live FY only has
    a few weeks of data and renders as a single dot)."""
    cur.execute(
        """
        SELECT event_date::date AS d,
               COALESCE(SUM(realized_pnl), 0)::numeric AS pnl
        FROM portfolio_capital_log
        WHERE user_id = %s AND fy = %s
        GROUP BY d
        ORDER BY d
        """,
        (user_id, fy),
    )
    return [{"date": r["d"].strftime("%Y-%m-%d"), "pnl": float(r["pnl"] or 0)} for r in cur.fetchall()]


def _trade_stats_from_positions(cur, user_id, fy: str) -> tuple[int, int]:
    """Count closed trades + winners in this FY by exit_date window. Used by
    the long-term curve's per-FY summary block."""
    try:
        start_year = int(fy.split("-")[0])
    except (ValueError, IndexError):
        return 0, 0
    fy_start = f"{start_year}-04-01 00:00:00"
    fy_end = f"{start_year + 1}-03-31 23:59:59"
    cur.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE total_pnl IS NOT NULL)        AS cnt,
            COUNT(*) FILTER (WHERE COALESCE(total_pnl, 0) > 0)   AS wins
        FROM positions
        WHERE user_id = %s
          AND status = 'closed'
          AND exit_date >= %s::timestamp
          AND exit_date <= %s::timestamp
        """,
        (user_id, fy_start, fy_end),
    )
    row = cur.fetchone()
    return int(row["cnt"] or 0), int(row["wins"] or 0)


def _realized_pl_from_log(cur, user_id, fy: str) -> float:
    cur.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0)::numeric AS pl "
        "FROM portfolio_capital_log WHERE user_id = %s AND fy = %s",
        (user_id, fy),
    )
    row = cur.fetchone()
    return float(row["pl"] or 0) if row else 0.0


@valvo_journal_bp.route("/api/equity-curve/long-term", methods=["GET"])
@limiter.limit("60 per minute")
def long_term_equity_curve():
    """Multi-FY equity curve with two views:
    1. Amount (₹): actual portfolio value, dotted lines where capital was added
    2. % Growth: simple-add of monthly PF impacts across FYs (each month uses its FY's base)

    Extensible: add more FYs by adding entries to fy_config list.
    """
    from services.user_analytics_service import get_user_base_capital
    conn = _get_db()
    try:
        cur = conn.cursor()

        # ═══ FY Configuration — add new FYs here ═══
        fy_config = [
            {
                "fy": "2020-21", "label": "FY 2020-21",
                "start_capital": 6000000,   # ₹60L
                "end_capital": 9075419,     # carries to FY21-22 start
                "monthly_table": "legacy_monthly_fy2021",
                "trades_table": "legacy_trades_fy2021",
                "start_date": "2020-04-01",
            },
            {
                "fy": "2021-22", "label": "FY 2021-22",
                "start_capital": 9075419,   # ₹90.75L (back-calculated with 20% tax)
                "end_capital": 16187147,    # carries to FY22-23 start
                "monthly_table": "legacy_monthly_fy2122",
                "trades_table": "legacy_trades_fy2122",
                "start_date": "2021-04-01",
            },
            {
                "fy": "2022-23", "label": "FY 2022-23",
                "start_capital": 16187147,  # ₹1.62 Cr (back-calculated with 20% tax)
                "end_capital": 18028240,    # carries to FY23-24 start
                "monthly_table": "legacy_monthly_fy2223",
                "trades_table": "legacy_trades_fy2223",
                "start_date": "2022-04-01",
            },
            {
                "fy": "2023-24", "label": "FY 2023-24",
                "start_capital": 18028240,  # ₹1.80 Cr (FY24-25 ₹2.8Cr - post-tax profits ₹99.7L)
                "end_capital": 28000000,    # ₹2.8 Cr (capital added for next FY)
                "monthly_table": "legacy_monthly_fy2324",
                "trades_table": "legacy_trades_fy2324",
                "start_date": "2023-04-01",
            },
            {
                "fy": "2024-25", "label": "FY 2024-25",
                "start_capital": 28000000,  # ₹2.8 Cr
                "end_capital": 50000000,    # ₹5 Cr (capital added)
                "monthly_table": "legacy_monthly_fy2425",
                "trades_table": "legacy_trades_fy2425",
                "start_date": "2024-04-01",
            },
            {
                "fy": "2025-26", "label": "FY 2025-26",
                "start_capital": 50000000,  # ₹5 Cr
                "end_capital": None,        # current FY, no end yet
                "monthly_table": "legacy_monthly_summary",
                "trades_table": "legacy_trades",
                "start_date": "2025-04-01",
            },
        ]

        # ═══ User scoping: only show FYs this user has configured ═══
        filtered_fy_config = []
        for fc in fy_config:
            user_base = get_user_base_capital(cur, g.user_id, fc["fy"])
            if user_base is not None:
                fc_copy = dict(fc)
                fc_copy["start_capital"] = int(user_base)
                fc_copy["source"] = "legacy"
                filtered_fy_config.append(fc_copy)

        # Pull in any user-configured FY beyond the hardcoded list. FY 2026-27+
        # has no legacy_monthly_* / legacy_trades_* tables — exits live in
        # portfolio_capital_log (per-event log written from close/sell/pyramid
        # mutation paths). Synthesize a fy_config entry that the per-FY loop
        # below detects via source == 'log' and pulls from the log instead.
        known_fys = {fc["fy"] for fc in filtered_fy_config}
        cur.execute(
            "SELECT fy, base_capital FROM user_fy_config WHERE user_id = %s ORDER BY fy",
            (g.user_id,),
        )
        for r in cur.fetchall():
            if r["fy"] in known_fys:
                continue
            try:
                start_year = int(str(r["fy"]).split("-")[0])
            except (ValueError, IndexError):
                continue
            filtered_fy_config.append({
                "fy": r["fy"],
                "label": f"FY {r['fy']}",
                "start_capital": int(float(r["base_capital"])),
                "end_capital": None,
                "monthly_table": None,
                "trades_table": None,
                "start_date": f"{start_year}-04-01",
                "source": "log",
            })

        filtered_fy_config.sort(key=lambda x: x["fy"])
        if not filtered_fy_config:
            return jsonify({"error": "setup_required", "message": "Set your FY base capital first"}), 400
        fy_config = filtered_fy_config

        month_dates_template = [
            "04-30", "05-31", "06-30", "07-31", "08-31", "09-30",
            "10-31", "11-30", "12-31", "01-31", "02-28", "03-31",
        ]

        amount_points = []  # ₹ value over time
        pct_points = []     # cumulative % over time
        fy_boundaries = []  # where capital was added (for dotted lines)
        cumm_pct = 0        # running cumulative %
        prior_fy_cumm = 0   # sum of prior FYs' total returns

        for fi, fc in enumerate(fy_config):
            base = fc["start_capital"]
            start_year = int(fc["fy"].split("-")[0])

            # Fetch monthly data — legacy FYs read from monthly aggregates,
            # 2026-27+ aggregates portfolio_capital_log realized events.
            if fc.get("source") == "log":
                months = _months_from_log(cur, g.user_id, fc["fy"], base)
                trade_count, win_count = _trade_stats_from_positions(cur, g.user_id, fc["fy"])
            else:
                cur.execute(f"""
                    SELECT month_label, month_order, after_charges, net_pf_impact, charges
                    FROM {fc['monthly_table']} ORDER BY month_order
                """)
                months = cur.fetchall()

                cur.execute(f"SELECT COUNT(*) as cnt FROM {fc['trades_table']}")
                trade_count = cur.fetchone()["cnt"]
                cur.execute(f"SELECT COUNT(*) as cnt FROM {fc['trades_table']} WHERE is_winner")
                win_count = cur.fetchone()["cnt"]

            # FY start point
            if fi == 0:
                amount_points.append({
                    "date": fc["start_date"], "label": f"{fc['label']} Start",
                    "amount": base, "pnl": 0, "source": fc["fy"], "type": "start",
                })
                pct_points.append({
                    "date": fc["start_date"], "label": f"{fc['label']} Start",
                    "cumm_pct": 0, "month_pct": 0, "source": fc["fy"], "type": "start",
                })
            elif fi > 0 and amount_points:
                # Check if capital was added by comparing new start vs actual last portfolio value
                last_actual_amount = amount_points[-1]["amount"]
                capital_diff = base - last_actual_amount
                if abs(capital_diff) > 100000:  # > ₹1L difference = capital was added/removed
                    if capital_diff >= 0:
                        gap_label = f"+₹{capital_diff/100000:.1f}L capital added"
                    else:
                        gap_label = f"-₹{abs(capital_diff)/100000:.1f}L (tax & adj)"
                    fy_boundaries.append({
                        "date": fc["start_date"],
                        "from_fy": fy_config[fi-1]["fy"],
                        "to_fy": fc["fy"],
                        "from_amount": last_actual_amount,
                        "to_amount": base,
                        "capital_added": capital_diff,
                        "label": gap_label,
                    })
                    # Add the jump point for amount view
                    amount_points.append({
                        "date": fc["start_date"], "label": f"{fc['label']} Start",
                        "amount": base, "pnl": 0, "source": fc["fy"], "type": "capital_added",
                    })
                # Always add a pct_points start anchor so single-FY views have
                # at least one anchor point at FY start (0% after frontend
                # re-baseline). Without this, current FY w/ <2 monthly points
                # renders "Not enough data".
                pct_points.append({
                    "date": fc["start_date"], "label": f"{fc['label']} Start",
                    "cumm_pct": round(prior_fy_cumm, 2), "month_pct": 0,
                    "source": fc["fy"], "type": "start",
                })

            # Points within FY. Log-sourced FYs get daily resolution (one
            # point per trading day with a realized event), so the live FY
            # renders as a real curve rather than a single monthly dot.
            running_amount = float(base)
            fy_multiplier = 1.0
            if fc.get("source") == "log":
                day_events = _daily_log_events(cur, g.user_id, fc["fy"])
                for ev in day_events:
                    pnl = ev["pnl"]
                    pct = (pnl / running_amount * 100.0) if running_amount > 0 else 0.0
                    running_amount += pnl
                    fy_multiplier *= (1 + pct / 100)
                    cumm_pct = prior_fy_cumm + round((fy_multiplier - 1) * 100, 4)
                    amount_points.append({
                        "date": ev["date"],
                        "label": ev["date"],
                        "amount": round(running_amount),
                        "pnl": round(pnl),
                        "month_pct": round(pct, 4),
                        "source": fc["fy"],
                        "type": "day",
                    })
                    pct_points.append({
                        "date": ev["date"],
                        "label": ev["date"],
                        "cumm_pct": round(cumm_pct, 4),
                        "month_pct": round(pct, 4),
                        "source": fc["fy"],
                        "type": "day",
                        "base_capital": base,
                    })
            else:
                for m in months:
                    pct = m["after_charges"] or 0
                    month_pl = round((pct / 100) * running_amount)
                    running_amount += month_pl
                    fy_multiplier *= (1 + pct / 100)
                    fy_cumm_pct = round((fy_multiplier - 1) * 100, 2)
                    cumm_pct = prior_fy_cumm + fy_cumm_pct

                    # Compute date
                    mo = m["month_order"]
                    if mo <= 9:  # Apr-Dec = same year
                        dt = f"{start_year}-{month_dates_template[mo-1]}"
                    else:  # Jan-Mar = next year
                        dt = f"{start_year+1}-{month_dates_template[mo-1]}"

                    amount_points.append({
                        "date": dt,
                        "label": m["month_label"],
                        "amount": round(running_amount),
                        "pnl": month_pl,
                        "month_pct": round(pct, 2),
                        "source": fc["fy"],
                        "type": "month",
                    })
                    pct_points.append({
                        "date": dt,
                        "label": m["month_label"],
                        "cumm_pct": round(cumm_pct, 2),
                        "month_pct": round(pct, 2),
                        "source": fc["fy"],
                        "type": "month",
                        "base_capital": base,
                    })

            # FY summary — compounded return within this FY
            fy_compound_return = round((fy_multiplier - 1) * 100, 2)
            prior_fy_cumm += fy_compound_return

        # Summary
        final_amount = amount_points[-1]["amount"] if amount_points else 0
        peak_amount = max(p["amount"] for p in amount_points) if amount_points else 0

        # Per-FY summaries
        fy_summaries = []
        for fc in fy_config:
            base = fc["start_capital"]
            if fc.get("source") == "log":
                tc, wc = _trade_stats_from_positions(cur, g.user_id, fc["fy"])
                pl = _realized_pl_from_log(cur, g.user_id, fc["fy"])
                fy_months = _months_from_log(cur, g.user_id, fc["fy"], base)
            else:
                cur.execute(f"SELECT COUNT(*) as cnt FROM {fc['trades_table']}")
                tc = cur.fetchone()["cnt"]
                cur.execute(f"SELECT COUNT(*) as cnt FROM {fc['trades_table']} WHERE is_winner")
                wc = cur.fetchone()["cnt"]
                cur.execute(f"SELECT ROUND(SUM(realized_pl)::numeric) as pl FROM {fc['trades_table']}")
                pl = float(cur.fetchone()["pl"] or 0)
                cur.execute(f"SELECT after_charges FROM {fc['monthly_table']} ORDER BY month_order")
                fy_months = cur.fetchall()

            fy_mult = 1.0
            fy_portfolio = float(base)
            for fm in fy_months:
                pct = fm["after_charges"] or 0
                month_pl = (pct / 100) * fy_portfolio
                fy_portfolio += month_pl
                fy_mult *= (1 + pct / 100)
            net_pct = round((fy_mult - 1) * 100, 2)

            fy_summaries.append({
                "fy": fc["fy"], "label": fc["label"],
                "start_capital": base,
                "end_capital": round(base + (net_pct / 100 * base)),
                "gross_pl": round(pl),
                "net_return_pct": round(net_pct, 2),
                "trades": tc, "wins": wc,
                "win_rate": round(wc / tc * 100, 1) if tc > 0 else 0,
            })

        return jsonify({
            "amount_points": amount_points,
            "pct_points": pct_points,
            "fy_boundaries": fy_boundaries,
            "fy_summaries": fy_summaries,
            "method": "simple_add",  # for future: "compounded"
            "final_cumm_pct": round(cumm_pct, 2),
            "final_amount": final_amount,
            "peak_amount": peak_amount,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[valvo-journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


# Whitelist of allowed benchmark symbols (display label → DB symbol)
BENCHMARK_SYMBOLS = {
    "smallcap_100":  ("NIFTY SMALLCAP 100", "Smallcap 100"),
    "smallcap_250":  ("NIFTY SMALLCAP 250", "Smallcap 250"),
    "nifty_50":      ("NIFTY",              "Nifty 50"),
    "nifty_500":     ("NIFTY 500",          "Nifty 500"),
    "midcap_100":    ("NIFTY MID100 FREE",  "Midcap 100"),
    "next_50":       ("NIFTY NEXT 50",      "Next 50"),
    "microcap_250":  ("NIFTY MICROCAP250",  "Microcap 250"),
}


@valvo_journal_bp.route("/api/equity-curve/smallcap-benchmark", methods=["GET"])
@limiter.limit("60 per minute")
def smallcap_benchmark():
    """Index returns for benchmark comparison with portfolio equity curve.

    Query params:
      - benchmark:   key from BENCHMARK_SYMBOLS (default: smallcap_250)
      - symbol:      raw symbol (overrides benchmark; whitelisted)
      - resolution:  daily | weekly | monthly (default: daily — smooth real-time line)
      - from:        ISO date filter (default: 2020-04-01)
    """
    requested_key = request.args.get("benchmark", "smallcap_250").lower()
    raw_symbol = request.args.get("symbol")
    resolution = (request.args.get("resolution") or "daily").lower()
    from_date = request.args.get("from") or "2020-04-01"

    if resolution not in ("daily", "weekly", "monthly"):
        return jsonify({"error": "invalid resolution"}), 400

    # Resolve symbol with whitelist guard
    if raw_symbol:
        allowed = {db_sym for db_sym, _ in BENCHMARK_SYMBOLS.values()}
        if raw_symbol not in allowed:
            return jsonify({"error": "invalid symbol"}), 400
        db_symbol = raw_symbol
        display = next((lbl for db_sym, lbl in BENCHMARK_SYMBOLS.values() if db_sym == raw_symbol), raw_symbol)
    else:
        if requested_key not in BENCHMARK_SYMBOLS:
            return jsonify({"error": "invalid benchmark"}), 400
        db_symbol, display = BENCHMARK_SYMBOLS[requested_key]

    conn = _get_db()
    try:
        cur = conn.cursor()
        if resolution == "daily":
            cur.execute("""
                SELECT date, close
                FROM candles_indices
                WHERE symbol = %s AND date >= %s
                ORDER BY date
            """, (db_symbol, from_date))
        elif resolution == "weekly":
            cur.execute("""
                WITH weekly AS (
                    SELECT DATE_TRUNC('week', date) AS week_start,
                        (ARRAY_AGG(close ORDER BY date DESC))[1] AS close_val,
                        MAX(date) AS last_date
                    FROM candles_indices
                    WHERE symbol = %s AND date >= %s
                    GROUP BY DATE_TRUNC('week', date)
                    ORDER BY week_start
                )
                SELECT last_date::date AS date, close_val AS close FROM weekly ORDER BY date
            """, (db_symbol, from_date))
        else:  # monthly
            cur.execute("""
                WITH monthly AS (
                    SELECT DATE_TRUNC('month', date) AS month,
                        (ARRAY_AGG(close ORDER BY date DESC))[1] AS close_val,
                        MAX(date) AS last_date
                    FROM candles_indices
                    WHERE symbol = %s AND date >= %s
                    GROUP BY DATE_TRUNC('month', date)
                    ORDER BY month
                )
                SELECT last_date::date AS date, close_val AS close FROM monthly ORDER BY date
            """, (db_symbol, from_date))

        rows = cur.fetchall()
        if not rows:
            return jsonify({"points": [], "symbol": db_symbol, "display": display, "resolution": resolution})

        base = float(rows[0]["close"])
        points = []
        for r in rows:
            close = float(r["close"])
            cum_pct = round((close / base - 1) * 100, 3)
            dt = str(r["date"])
            year = int(dt[:4])
            month = int(dt[5:7])
            fy_start = year if month >= 4 else year - 1
            fy_label = f"{fy_start}-{str(fy_start + 1)[-2:]}"
            points.append({
                "date": dt,
                "close": round(close, 2),
                "cum_pct": cum_pct,
                "fy": fy_label,
            })

        return jsonify({
            "points": points,
            "base_date": str(rows[0]["date"]),
            "base_close": base,
            "symbol": db_symbol,
            "display": display,
            "resolution": resolution,
            "count": len(points),
            "last_date": str(rows[-1]["date"]),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[valvo-journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@valvo_journal_bp.route("/api/trade-history", methods=["GET"])
@limiter.limit("60 per minute")
def trade_history():
    """Trade history — FY-aware, user-scoped, returns trades grouped by month.
    ?fy=2023-24|2024-25|2025-26  (default: 2026-27)
    ?limit=10  (for preview; omit for all trades)
    """
    fy = request.args.get("fy", "2026-27")
    limit = request.args.get("limit", type=int)
    conn = _get_db()
    try:
        cur = conn.cursor()

        from services.user_analytics_service import resolve_fy
        resolved = resolve_fy(cur, g.user_id, fy)
        if not resolved.get("allowed"):
            if resolved.get("needs_setup"):
                return jsonify({"error": "setup_required", "message": "Set your base capital first"}), 400
            return jsonify({"error": "FY not available"}), 403

        table = resolved["table"]
        base = resolved["base"]

        # If user filtering needed and not already handled in a UNION subquery
        if resolved.get("user_filter"):
            where = f"user_id = '{g.user_id}'"
            if resolved.get("fy_filter"):
                where += f" AND fy = '{resolved['fy_filter']}'"
            table = f"(SELECT * FROM {table} WHERE {where}) _uf"

        # Month ordering: month_order from monthly table, or derive from month key
        # NOTE: %% is required because psycopg2 interprets bare % as format specifiers
        # (e.g. %s in '%september%' was parsed as a parameter placeholder, causing IndexError)
        month_order_sql = """
            CASE
                WHEN month ILIKE '%%april%%' OR month ILIKE '%%apr%%' THEN 1
                WHEN month ILIKE '%%may%%' THEN 2
                WHEN month ILIKE '%%june%%' OR month ILIKE '%%jun%%' THEN 3
                WHEN month ILIKE '%%july%%' OR month ILIKE '%%jul%%' THEN 4
                WHEN month ILIKE '%%august%%' OR month ILIKE '%%aug%%' THEN 5
                WHEN month ILIKE '%%september%%' OR month ILIKE '%%sep%%' THEN 6
                WHEN month ILIKE '%%october%%' OR month ILIKE '%%oct%%' THEN 7
                WHEN month ILIKE '%%november%%' OR month ILIKE '%%nov%%' THEN 8
                WHEN month ILIKE '%%december%%' OR month ILIKE '%%dec%%' THEN 9
                WHEN month ILIKE '%%january%%' OR month ILIKE '%%jan%%' THEN 10
                WHEN month ILIKE '%%february%%' OR month ILIKE '%%feb%%' THEN 11
                WHEN month ILIKE '%%march%%' OR month ILIKE '%%mar%%' THEN 12
                ELSE 13
            END
        """

        if limit:
            # Preview mode: last N trades by month order descending, then by P&L
            cur.execute(f"""
                SELECT symbol, quantity, buy_value, sell_value,
                    realized_pl, realized_pl_pct, impact_on_pf,
                    month_label, month, is_winner,
                    ({month_order_sql}) as month_order
                FROM {table}
                ORDER BY ({month_order_sql}) DESC, realized_pl DESC
                LIMIT %s
            """, (limit,))
            trades = [dict(r) for r in cur.fetchall()]
            return jsonify({"trades": trades, "fy": fy, "base": base, "total_count": len(trades)})

        # Full mode: all trades grouped by month
        cur.execute(f"""
            SELECT symbol, quantity, buy_value, sell_value,
                realized_pl, realized_pl_pct, impact_on_pf,
                month_label, month, is_winner,
                ({month_order_sql}) as month_order
            FROM {table}
            ORDER BY ({month_order_sql}) ASC, realized_pl DESC
        """)
        all_trades = [dict(r) for r in cur.fetchall()]

        # Group by month
        from collections import OrderedDict
        months = OrderedDict()
        for t in all_trades:
            key = t["month"]
            if key not in months:
                months[key] = {
                    "month": key,
                    "month_label": t["month_label"],
                    "month_order": t["month_order"],
                    "trades": [],
                    "total_pl": 0, "winners": 0, "losers": 0,
                }
            months[key]["trades"].append(t)
            months[key]["total_pl"] += (t["realized_pl"] or 0)
            if t["is_winner"]:
                months[key]["winners"] += 1
            else:
                months[key]["losers"] += 1

        for m in months.values():
            tc = len(m["trades"])
            m["trade_count"] = tc
            m["win_rate"] = round(m["winners"] / tc * 100, 1) if tc > 0 else 0
            m["total_pl"] = round(m["total_pl"])

        return jsonify({
            "months": list(months.values()),
            "fy": fy, "base": base,
            "total_trades": len(all_trades),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[valvo-journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@valvo_journal_bp.route("/api/monthly-pl/full", methods=["GET"])
@limiter.limit("60 per minute")
def full_monthly_pl():
    """Monthly P&L from legacy monthly tables. Supports ?fy= parameter."""
    fy = request.args.get("fy", "2025-26")
    from services.user_analytics_service import get_user_base_capital
    conn = _get_db()
    try:
        cur = conn.cursor()

        fy_monthly_map = {
            "2020-21": ("legacy_monthly_fy2021", 6000000),
            "2021-22": ("legacy_monthly_fy2122", 9075419),
            "2022-23": ("legacy_monthly_fy2223", 16187147),
            "2023-24": ("legacy_monthly_fy2324", 18028240),
            "2024-25": ("legacy_monthly_fy2425", 28000000),
            "2025-26": ("legacy_monthly_summary", 50000000),
        }

        if fy == "all":
            # Combine all FYs chronologically — only FYs the user has configured
            all_months = []
            any_fy = False
            first_base = None
            for fy_key in ["2020-21", "2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]:
                tbl, _default_base = fy_monthly_map[fy_key]
                user_base = get_user_base_capital(cur, g.user_id, fy_key)
                if user_base is None:
                    continue
                any_fy = True
                base = float(user_base)
                if first_base is None:
                    first_base = base
                cur.execute(f"SELECT month_label, month_order, after_charges, scripts_traded FROM {tbl} ORDER BY month_order")
                rows = cur.fetchall()
                portfolio = base
                for m in rows:
                    pct = m["after_charges"] or 0
                    pl = round((pct / 100) * portfolio)
                    portfolio += pl
                    all_months.append({
                        "label": m["month_label"],
                        "order": len(all_months) + 1,
                        "pl": pl,
                        "pct": round(pct, 3),
                        "trades": m["scripts_traded"] or 0,
                        "source": fy_key,
                    })
            if not any_fy:
                return jsonify({"error": "setup_required", "message": "Set your FY base capital first"}), 400
            # Compute cumulative across all (use first configured FY's base as denominator)
            cumm = 0
            denom = first_base or 1
            for m in all_months:
                cumm += m["pl"]
                m["cumm_pl"] = cumm
                m["cumm_pct"] = round(cumm / denom * 100, 2)
            return jsonify({"months": all_months, "base_capital": int(denom), "fy": "all"})

        # FY 26-27: compute from journal_trades_computed (user-scoped)
        if fy == "2026-27":
            _base = get_user_base_capital(cur, g.user_id, "2026-27")
            if _base is None:
                return jsonify({"error": "setup_required", "message": "Set your FY 2026-27 base capital first"}), 400
            base = int(_base)
            cur.execute("""
                SELECT month_label, COUNT(*) as trades, ROUND(SUM(realized_pl)::numeric) as total_pl
                FROM journal_trades_computed
                WHERE user_id = %s
                GROUP BY month_label ORDER BY MIN(trade_date)
            """, (g.user_id,))
            rows = cur.fetchall()
            months = []
            portfolio = float(base)
            for i, m in enumerate(rows):
                pl = float(m["total_pl"] or 0)
                pct = round(pl / portfolio * 100, 3)
                portfolio += pl
                months.append({
                    "label": m["month_label"],
                    "order": i + 1,
                    "pl": round(pl),
                    "pct": pct,
                    "trades": m["trades"] or 0,
                    "source": "journal",
                    "cumm_pl": round(portfolio - base),
                    "cumm_pct": round((portfolio / base - 1) * 100, 2),
                })
            return jsonify({"months": months, "base_capital": base, "fy": "2026-27"})

        # Specific legacy FY — gate by user_fy_config
        user_base = get_user_base_capital(cur, g.user_id, fy)
        if user_base is None:
            return jsonify({"error": "setup_required", "message": f"Set your FY {fy} base capital first"}), 400
        tbl, _default = fy_monthly_map.get(fy, ("legacy_monthly_summary", 50000000))
        base = float(user_base)

        cur.execute(f"""
            SELECT month_label, month_order, after_charges, scripts_traded
            FROM {tbl} ORDER BY month_order
        """)
        legacy = cur.fetchall()

        months = []
        portfolio = base
        for m in legacy:
            pct = m["after_charges"] or 0
            pl = round((pct / 100) * portfolio)
            portfolio += pl
            cumm_pct = round((portfolio / base - 1) * 100, 2)
            months.append({
                "label": m["month_label"],
                "order": m["month_order"],
                "pl": pl,
                "pct": round(pct, 3),
                "trades": m["scripts_traded"] or 0,
                "source": "legacy",
                "cumm_pl": round(portfolio - base),
                "cumm_pct": cumm_pct,
            })

        return jsonify({"months": months, "base_capital": base, "fy": fy})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[valvo-journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@valvo_journal_bp.route("/api/analytics/trade-stats", methods=["GET"])
@limiter.limit("60 per minute")
def trade_stats_by_period():
    """
    Returns win_rate, avg_winner_pct, avg_loser_pct filtered by rolling time period.
    User-scoped: legacy tables only for users with user_fy_config entries,
    journal_trades_computed filtered by user_id.
    """
    from datetime import datetime, timedelta
    from services.user_analytics_service import get_user_base_capital, LEGACY_FY_TABLES
    period = request.args.get("period", "1y")
    # "fy" = current Indian FY (Apr 1 → Mar 31). Special-cased below.
    period_map = {"1m": 30, "2m": 60, "fy": "fy", "6m": 180, "1y": 365, "2y": 730, "full": 99999}
    period_order = ["1m", "2m", "fy", "6m", "1y", "2y", "full"]

    # Cutoff for "fy" — April 1 of the current Indian FY
    today = datetime.now()
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    fy_cutoff = f"{fy_start_year}-04-01"

    conn = _get_db()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 500
    try:
        cur = conn.cursor()

        # Build user-scoped union of trade tables.
        # `total_trades` counts every entry in the window (open + closed) so
        # the user sees what they actually took. Win/loss counts and the
        # win-rate denominator only consider CLOSED trades — open positions
        # have realized_pl=0 and would otherwise drag the rate to ~0.
        # Filter by entry date (trade_date / month_label) so the window means
        # "trades I took in the last N days", which is what users expect.
        parts = []
        # Legacy tables the user has access to (via user_fy_config).
        # All legacy rows are already realized; month_label is the close month.
        # Use canonical LEGACY_FY_TABLES so any newly-added FY is auto-included.
        for fy_key, tbl in LEGACY_FY_TABLES.items():
            if get_user_base_capital(cur, g.user_id, fy_key) is not None:
                parts.append(
                    f"SELECT symbol as name, realized_pl as pl, realized_pl_pct as move_pct, "
                    f"TO_DATE(month_label, 'Month YYYY') as trade_month, "
                    f"true as is_closed FROM {tbl} WHERE user_id = %s"
                )

        # Journal trades — user-filtered, includes open + closed. The view
        # itself now joins to positions so realized_pl/is_winner/position_status
        # are canonical (see migration 2026_04_24_journal_view_uses_positions).
        # Every consumer of the view — Stock Scoring, Analytics, Valvo AI —
        # sees the same numbers Position Manager shows.
        has_journal = get_user_base_capital(cur, g.user_id, "2026-27") is not None
        if has_journal:
            parts.append(
                "SELECT symbol as name, realized_pl as pl, realized_pl_pct as move_pct, "
                "trade_date as trade_month, "
                "(position_status = 'Closed') as is_closed "
                "FROM journal_trades_computed WHERE user_id = %s"
            )

        if not parts:
            return jsonify({
                "period": period, "total_trades": 0, "win_count": 0,
                "loss_count": 0, "win_rate": 0, "avg_winner_pct": 0,
                "avg_loser_pct": 0, "total_pl": 0,
            })

        # Each part has one user_id placeholder. Build the param tuple in order.
        user_id_count = len(parts)
        all_trades_cte = "WITH all_trades AS (" + " UNION ALL ".join(parts) + ")"
        cte_params = tuple([g.user_id] * user_id_count)

        def _cutoff_for(p):
            """Returns (cutoff_str_or_None) for a period key. None = no date filter."""
            if p == "fy":
                return fy_cutoff
            days = period_map.get(p, 365)
            if isinstance(days, int) and days >= 99999:
                return None
            return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        # Try each period starting from requested, cascade if empty
        start_idx = period_order.index(period) if period in period_order else period_order.index("1y")
        active_period = period

        for p in period_order[start_idx:]:
            cutoff = _cutoff_for(p)
            if cutoff is None:
                q = all_trades_cte + " SELECT COUNT(*) as cnt FROM all_trades"
                cur.execute(q, cte_params)
            else:
                q = all_trades_cte + " SELECT COUNT(*) as cnt FROM all_trades WHERE trade_month >= %s"
                cur.execute(q, cte_params + (cutoff,))
            cnt = cur.fetchone()["cnt"]
            active_period = p
            if cnt > 0:
                break

        # Compute stats for the active period
        cutoff = _cutoff_for(active_period)
        if cutoff is None:
            date_filter = ""
            params = cte_params
        else:
            date_filter = "WHERE trade_month >= %s"
            params = cte_params + (cutoff,)

        # Win rate = closed profitable / decided closed (matches Position
        # Manager). pl < 0 = loss, pl > 0 = win, pl == 0 (breakeven) is
        # excluded from the denominator.
        cur.execute(all_trades_cte + f"""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN is_closed AND pl > 0 THEN 1 ELSE 0 END) as win_count,
                SUM(CASE WHEN is_closed AND pl < 0 THEN 1 ELSE 0 END) as loss_count,
                SUM(CASE WHEN NOT is_closed THEN 1 ELSE 0 END) as open_count,
                ROUND(
                    (SUM(CASE WHEN is_closed AND pl > 0 THEN 1 ELSE 0 END)::numeric
                     / NULLIF(SUM(CASE WHEN is_closed AND pl <> 0 THEN 1 ELSE 0 END), 0) * 100),
                    1
                ) as win_rate,
                ROUND(AVG(CASE WHEN is_closed AND pl > 0 THEN move_pct END)::numeric, 2) as avg_winner_pct,
                ROUND(AVG(CASE WHEN is_closed AND pl < 0 THEN move_pct END)::numeric, 2) as avg_loser_pct,
                ROUND(SUM(CASE WHEN is_closed THEN pl ELSE 0 END)::numeric) as total_pl
            FROM all_trades {date_filter}
        """, params)
        s = cur.fetchone()

        return jsonify({
            "period": active_period,
            "total_trades": int(s["total_trades"] or 0),
            "win_count": int(s["win_count"] or 0),
            "loss_count": int(s["loss_count"] or 0),
            "open_count": int(s["open_count"] or 0),
            "win_rate": float(s["win_rate"] or 0),
            "avg_winner_pct": float(s["avg_winner_pct"] or 0),
            "avg_loser_pct": float(s["avg_loser_pct"] or 0),
            "total_pl": float(s["total_pl"] or 0),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[valvo-journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@valvo_journal_bp.route("/api/analytics/full", methods=["GET"])
@limiter.limit("60 per minute")
def full_analytics():
    """Combined analytics — user-scoped. Supports ?fy=2024-25 | 2025-26 | 2026-27 | all."""
    fy = request.args.get("fy", "2026-27")
    conn = _get_db()
    try:
        cur = conn.cursor()

        from services.user_analytics_service import (
            resolve_fy, get_user_role, get_user_base_capital, LEGACY_FY_TABLES
        )

        if fy == "all":
            parts_full = []
            parts_simple = []

            # Include legacy tables only if user has config entries for them
            for fy_key, tbl in LEGACY_FY_TABLES.items():
                if get_user_base_capital(cur, g.user_id, fy_key) is not None:
                    parts_full.append(
                        f"SELECT symbol as name, realized_pl as pl, realized_pl_pct as move_pct, "
                        f"ROUND((realized_pl_pct / 3.0)::numeric, 2) as r_multiple, "
                        f"month_label as period, '{fy_key}' as src FROM {tbl}"
                    )
                    parts_simple.append(
                        f"SELECT realized_pl as pl, realized_pl_pct as m, "
                        f"ROUND((realized_pl_pct / 3.0)::numeric, 2) as r FROM {tbl}"
                    )

            # Journal trades — always filtered by user, closed-only so the
            # stats line up with legacy FYs (which only contain closed trades).
            if get_user_base_capital(cur, g.user_id, "2026-27") is not None:
                parts_full.append(
                    f"SELECT symbol as name, realized_pl as pl, realized_pl_pct as move_pct, "
                    f"ROUND((realized_pl_pct / 3.0)::numeric, 2) as r_multiple, "
                    f"month_label as period, '2026-27' as src "
                    f"FROM journal_trades_computed "
                    f"WHERE user_id = '{g.user_id}' AND position_status = 'Closed'"
                )
                parts_simple.append(
                    f"SELECT realized_pl as pl, realized_pl_pct as m, "
                    f"ROUND((realized_pl_pct / 3.0)::numeric, 2) as r "
                    f"FROM journal_trades_computed "
                    f"WHERE user_id = '{g.user_id}' AND position_status = 'Closed'"
                )

            if not parts_full:
                return jsonify({"error": "setup_required", "message": "Set your base capital first"}), 400

            cte = "WITH all_trades AS (" + " UNION ALL ".join(parts_full) + ")"
            cte_simple = "WITH all_trades AS (" + " UNION ALL ".join(parts_simple) + ")"

            # Use earliest configured FY's base capital
            cur.execute("SELECT fy, base_capital FROM user_fy_config WHERE user_id = %s ORDER BY fy LIMIT 1", (g.user_id,))
            first_row = cur.fetchone()
            base = float(first_row["base_capital"]) if first_row else 6000000
        else:
            resolved = resolve_fy(cur, g.user_id, fy)
            if not resolved.get("allowed"):
                if resolved.get("needs_setup"):
                    return jsonify({"error": "setup_required", "message": "Set your base capital first"}), 400
                return jsonify({"error": "FY not available"}), 403

            base = resolved["base"]
            table = resolved["table"]
            if resolved.get("user_filter"):
                where = f"user_id = '{g.user_id}'"
                if resolved.get("fy_filter"):
                    where += f" AND fy = '{resolved['fy_filter']}'"
                # Only count fully-closed trades in stats. Open / partially
                # exited trades have realized_pl = 0 and is_winner = false,
                # which inflates loss count and tanks win rate. Legacy FYs
                # only have closed trades anyway, so this keeps FY-to-FY
                # comparisons apples-to-apples. closed_count / open_count
                # are still surfaced separately below.
                if resolved.get("source") == "journal":
                    where += " AND position_status = 'Closed'"
                table = f"(SELECT * FROM {table} WHERE {where}) _uf"

            cte = f"""WITH all_trades AS (
                SELECT symbol as name, realized_pl as pl, realized_pl_pct as move_pct,
                    ROUND((realized_pl_pct / 3.0)::numeric, 2) as r_multiple,
                    month_label as period, '{fy}' as src
                FROM {table}
            )"""
            cte_simple = f"WITH all_trades AS (SELECT realized_pl as pl, realized_pl_pct as m, ROUND((realized_pl_pct / 3.0)::numeric, 2) as r FROM {table})"

        # Main stats
        cur.execute(cte + """
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pl <= 0 THEN 1 ELSE 0 END) as losses,
                ROUND((SUM(CASE WHEN pl > 0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*),0) * 100), 1) as win_rate,
                ROUND(SUM(pl)::numeric) as total_pl,
                ROUND(AVG(CASE WHEN pl > 0 THEN move_pct END)::numeric, 2) as avg_winner_pct,
                ROUND(AVG(CASE WHEN pl <= 0 THEN move_pct END)::numeric, 2) as avg_loser_pct,
                ROUND(AVG(CASE WHEN pl > 0 THEN r_multiple END)::numeric, 2) as avg_winner_r,
                ROUND(AVG(CASE WHEN pl <= 0 THEN r_multiple END)::numeric, 2) as avg_loser_r,
                ROUND(AVG(CASE WHEN pl > 0 THEN pl END)::numeric) as avg_winner_pl,
                ROUND(AVG(CASE WHEN pl <= 0 THEN pl END)::numeric) as avg_loser_pl,
                ROUND(SUM(CASE WHEN pl > 0 THEN pl ELSE 0 END)::numeric) as gross_profit,
                ROUND(ABS(SUM(CASE WHEN pl <= 0 THEN pl ELSE 0 END))::numeric) as gross_loss,
                ROUND((SUM(CASE WHEN pl > 0 THEN pl ELSE 0 END) /
                    ABS(NULLIF(SUM(CASE WHEN pl <= 0 THEN pl ELSE 0 END), 0)))::numeric, 2) as profit_factor,
                ROUND((AVG(CASE WHEN pl > 0 THEN pl END) /
                    ABS(NULLIF(AVG(CASE WHEN pl <= 0 THEN pl END), 0)))::numeric, 2) as payoff_ratio,
                ROUND(AVG(r_multiple)::numeric, 3) as expectancy_r,
                ROUND(MAX(r_multiple)::numeric, 2) as best_r,
                ROUND(MIN(r_multiple)::numeric, 2) as worst_r,
                ROUND(MAX(move_pct)::numeric, 2) as best_move_pct,
                ROUND(MIN(move_pct)::numeric, 2) as worst_move_pct
            FROM all_trades
        """)
        s = cur.fetchone()

        # Top 10 winners
        cur.execute(cte + """
            SELECT name, ROUND(pl::numeric) as pl, ROUND(move_pct::numeric, 2) as move_pct,
                r_multiple, period, src
            FROM all_trades WHERE pl > 0 ORDER BY pl DESC LIMIT 10
        """)
        top_winners = [dict(r) for r in cur.fetchall()]

        # Top 10 losers
        cur.execute(cte + """
            SELECT name, ROUND(pl::numeric) as pl, ROUND(move_pct::numeric, 2) as move_pct,
                r_multiple, period, src
            FROM all_trades WHERE pl <= 0 ORDER BY pl ASC LIMIT 10
        """)
        top_losers = [dict(r) for r in cur.fetchall()]

        # Open positions (from Position Manager) — user-scoped
        cur.execute("SELECT COUNT(*) as cnt FROM positions WHERE status = 'active' AND user_id = %s", (g.user_id,))
        open_cnt = cur.fetchone()["cnt"]

        # ═══ ENRICHED INSIGHTS ═══

        # Concentration: top 5 winners as % of gross profit
        cur.execute(cte_simple + """
            , winners AS (SELECT pl FROM all_trades WHERE pl > 0)
            SELECT ROUND((SUM(t.pl) / NULLIF((SELECT SUM(pl) FROM winners), 0) * 100)::numeric, 1) as pct
            FROM (SELECT pl FROM winners ORDER BY pl DESC LIMIT 5) t
        """)
        top5_conc = float(cur.fetchone()["pct"] or 0)

        # P&L without top 3 trades
        cur.execute(cte_simple + """
            , ranked AS (SELECT pl, ROW_NUMBER() OVER (ORDER BY pl DESC) as rn FROM all_trades)
            SELECT ROUND(SUM(pl)::numeric) as pl FROM ranked WHERE rn > 3
        """)
        pl_no_top3 = float(cur.fetchone()["pl"] or 0)

        # Fat tail R-distribution
        cur.execute(cte_simple + """
            SELECT
                COUNT(*) FILTER (WHERE r >= 2) as above_2r,
                COUNT(*) FILTER (WHERE r >= 5) as above_5r,
                COUNT(*) FILTER (WHERE r >= 1) as above_1r
            FROM all_trades
        """)
        fat = cur.fetchone()

        # Win distribution by move %
        cur.execute(cte_simple + """
            SELECT
                COUNT(*) FILTER (WHERE pl>0 AND m BETWEEN 0 AND 1) as w_0_1,
                COUNT(*) FILTER (WHERE pl>0 AND m>1 AND m<=3) as w_1_3,
                COUNT(*) FILTER (WHERE pl>0 AND m>3 AND m<=5) as w_3_5,
                COUNT(*) FILTER (WHERE pl>0 AND m>5) as w_5p,
                COUNT(*) FILTER (WHERE pl<=0 AND ABS(m) BETWEEN 0 AND 1) as l_0_1,
                COUNT(*) FILTER (WHERE pl<=0 AND ABS(m)>1 AND ABS(m)<=2) as l_1_2,
                COUNT(*) FILTER (WHERE pl<=0 AND ABS(m)>2) as l_2p,
                ROUND((MAX(pl) / ABS(NULLIF(MIN(pl),0)))::numeric, 1) as asymmetry
            FROM all_trades
        """)
        dist = cur.fetchone()

        # Monthly returns — FY-aware table
        if fy == "all":
            # Combine only FYs the user has configured
            all_fy_monthly = [
                ("2020-21", "legacy_monthly_fy2021", 0,   "NULL::real"),
                ("2021-22", "legacy_monthly_fy2122", 100, "NULL::real"),
                ("2022-23", "legacy_monthly_fy2223", 200, "NULL::real"),
                ("2023-24", "legacy_monthly_fy2324", 300, "NULL::real"),
                ("2024-25", "legacy_monthly_fy2425", 400, "NULL::real"),
                ("2025-26", "legacy_monthly_summary", 500, "nifty_smallcap_change"),
            ]
            union_parts = []
            for fy_key, tbl, sort_offset, nifty_expr in all_fy_monthly:
                if get_user_base_capital(cur, g.user_id, fy_key) is not None:
                    union_parts.append(
                        f"SELECT month_label as label, SUBSTRING(month_label, 1, 3) as short, "
                        f"after_charges as pct, {nifty_expr} as nifty_sc, scripts_traded as trades, win_rate, "
                        f"'{fy_key}' as fy_src, month_order + {sort_offset} as sort_order "
                        f"FROM {tbl}"
                    )
            if union_parts:
                cur.execute(" UNION ALL ".join(union_parts) + " ORDER BY sort_order")
            else:
                cur.execute("SELECT NULL as label, NULL as short, NULL::real as pct, NULL::real as nifty_sc, NULL as trades, NULL as win_rate, NULL as fy_src, NULL as sort_order WHERE false")
        else:
            fy_monthly_map = {
                "2020-21": "legacy_monthly_fy2021",
                "2021-22": "legacy_monthly_fy2122",
                "2022-23": "legacy_monthly_fy2223",
                "2023-24": "legacy_monthly_fy2324",
                "2024-25": "legacy_monthly_fy2425",
                "2025-26": "legacy_monthly_summary",
            }
            if fy == "2026-27":
                # Compute monthly from journal_trades_computed on the fly (user-scoped).
                # NOTE: base is a float (resolve_fy → float(base_capital)). The old
                # f-string `{base}.0` became `50000000.0.0` which crashed Postgres
                # with a syntax error → every analytics call for FY 2026-27 returned
                # 500. Cast in SQL to keep the division as numeric.
                cur.execute(f"""
                    SELECT month_label as label,
                        SUBSTRING(month_label, 1, 3) as short,
                        ROUND((SUM(realized_pl) / NULLIF({float(base)}::numeric, 0) * 100)::numeric, 2) as pct,
                        NULL as nifty_sc,
                        COUNT(*) as trades,
                        ROUND((SUM(CASE WHEN is_winner THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100), 1) as win_rate
                    FROM journal_trades_computed
                    WHERE user_id = %s AND position_status = 'Closed'
                    GROUP BY month_label, month
                    ORDER BY MIN(trade_date)
                """, (g.user_id,))
            else:
                monthly_table = fy_monthly_map.get(fy, "legacy_monthly_summary")
                nifty_col = ", nifty_smallcap_change as nifty_sc" if fy == "2025-26" else ", NULL as nifty_sc"
                cur.execute(f"""
                    SELECT month_label as label,
                        SUBSTRING(month_label, 1, 3) as short,
                        after_charges as pct{nifty_col},
                        scripts_traded as trades, win_rate
                    FROM {monthly_table} ORDER BY month_order
                """)
        monthly = [dict(r) for r in cur.fetchall()]

        # Total P&L computation
        if fy == "all":
            # Compute each FY independently, sum total P&L
            total_pl = 0
            total_return_pct = 0
            for fy_key, _default_base, mtbl in [
                ("2020-21", 6000000, "legacy_monthly_fy2021"),
                ("2021-22", 9075419, "legacy_monthly_fy2122"),
                ("2022-23", 16187147, "legacy_monthly_fy2223"),
                ("2023-24", 18028240, "legacy_monthly_fy2324"),
                ("2024-25", 28000000, "legacy_monthly_fy2425"),
                ("2025-26", 50000000, "legacy_monthly_summary"),
            ]:
                user_base = get_user_base_capital(cur, g.user_id, fy_key)
                if user_base is None:
                    continue
                fy_base = float(user_base)
                cur.execute(f"SELECT after_charges FROM {mtbl} ORDER BY month_order")
                fy_months = [dict(r) for r in cur.fetchall()]
                fy_portfolio = fy_base
                for fm in fy_months:
                    pct_val = fm.get("after_charges") or 0
                    fy_portfolio += (pct_val / 100) * fy_portfolio
                total_pl += round(fy_portfolio - fy_base)
                total_return_pct += round((fy_portfolio / fy_base - 1) * 100, 2)
            return_pct = round(total_return_pct, 1)
        else:
            portfolio_calc = float(base)
            for m_row in monthly:
                # pct comes back as Decimal from the FY 2026-27 query (Postgres
                # ROUND(... ::numeric) → psycopg2 Decimal). Mixing with float
                # raises TypeError, so coerce explicitly.
                pct_val = float(m_row.get("pct") or 0)
                portfolio_calc += (pct_val / 100) * portfolio_calc
            total_pl = round(portfolio_calc - float(base))
            return_pct = round(total_pl / float(base) * 100, 1)

        # Last trade date
        last_trade_date = "2026-03-31"

        return jsonify({
            "total_pl": total_pl,
            "return_pct": return_pct,
            "last_trade_date": last_trade_date,
            "total_trades": s["total_trades"],
            "closed_count": s["total_trades"],
            "open_count": open_cnt,
            "win_count": s["wins"],
            "loss_count": s["losses"],
            "win_rate": float(s["win_rate"] or 0),
            "avg_winner_pct": float(s["avg_winner_pct"] or 0),
            "avg_loser_pct": float(s["avg_loser_pct"] or 0),
            "avg_winner_r": float(s["avg_winner_r"] or 0),
            "avg_loser_r": float(s["avg_loser_r"] or 0),
            "avg_winner_pl": float(s["avg_winner_pl"] or 0),
            "avg_loser_pl": float(s["avg_loser_pl"] or 0),
            "gross_profit": float(s["gross_profit"] or 0),
            "gross_loss": float(s["gross_loss"] or 0),
            "profit_factor": float(s["profit_factor"] or 0),
            "payoff_ratio": float(s["payoff_ratio"] or 0),
            "expectancy_r": float(s["expectancy_r"] or 0),
            "best_r": float(s["best_r"] or 0),
            "worst_r": float(s["worst_r"] or 0),
            "best_move_pct": float(s["best_move_pct"] or 0),
            "worst_move_pct": float(s["worst_move_pct"] or 0),
            "best_trade_name": top_winners[0]["name"] if top_winners else None,
            "best_trade_pl": float(top_winners[0]["pl"]) if top_winners else 0,
            "worst_trade_name": top_losers[0]["name"] if top_losers else None,
            "worst_trade_pl": float(top_losers[0]["pl"]) if top_losers else 0,
            "top_winners": top_winners,
            "top_losers": top_losers,
            "base_capital": base,
            "sl_assumption": "3% standard SL for legacy trades",
            # Enriched insights
            "top5_concentration_pct": top5_conc,
            "pl_without_top3": pl_no_top3,
            "trades_above_1r": fat["above_1r"],
            "trades_above_2r": fat["above_2r"],
            "trades_above_5r": fat["above_5r"],
            "win_dist": {"0_1": dist["w_0_1"], "1_3": dist["w_1_3"], "3_5": dist["w_3_5"], "5p": dist["w_5p"]},
            "loss_dist": {"0_1": dist["l_0_1"], "1_2": dist["l_1_2"], "2p": dist["l_2p"]},
            "asymmetry_ratio": float(dist["asymmetry"] or 0),
            "monthly": [{"label": m["label"], "short": m["short"], "pct": float(m["pct"] or 0),
                         "nifty_sc": float(m["nifty_sc"]) if m["nifty_sc"] else None,
                         "trades": m["trades"], "win_rate": float(m["win_rate"] or 0)} for m in monthly],
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[valvo-journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)



# ═══════════════════════════════════════════════════════════════
#   DRAWDOWN DEEP ANALYSIS
#   /api/analytics/drawdown-deep
# ═══════════════════════════════════════════════════════════════

@valvo_journal_bp.route("/api/analytics/drawdown-deep", methods=["GET"])
@limiter.limit("60 per minute")
def drawdown_deep():
    """
    Comprehensive drawdown analysis — tells the full story:
    1. Monthly portfolio values with drawdown from peak
    2. Identified drawdown periods with contributing trades
    3. Smallcap 100 market context for each period
    4. Recovery metrics and behavioral insights

    Supports ?fy=2020-21|...|2025-26|2026-27|all (default: all). When a single
    FY is specified, the timeline, drawdown periods, and Smallcap-comparison
    bars are restricted to that FY's months only — earlier-year data does not
    leak in.
    """
    from services.user_analytics_service import get_user_base_capital
    from flask import request as req
    fy_param = (req.args.get("fy") or "all").strip()
    conn = _get_db()
    try:
        cur = conn.cursor()

        fy_config = [
            {"fy": "2020-21", "label": "FY 20-21", "start_capital": 6000000,
             "monthly_table": "legacy_monthly_fy2021", "trades_table": "legacy_trades_fy2021",
             "start_date": "2020-04-01", "source": "legacy"},
            {"fy": "2021-22", "label": "FY 21-22", "start_capital": 9075419,
             "monthly_table": "legacy_monthly_fy2122", "trades_table": "legacy_trades_fy2122",
             "start_date": "2021-04-01", "source": "legacy"},
            {"fy": "2022-23", "label": "FY 22-23", "start_capital": 16187147,
             "monthly_table": "legacy_monthly_fy2223", "trades_table": "legacy_trades_fy2223",
             "start_date": "2022-04-01", "source": "legacy"},
            {"fy": "2023-24", "label": "FY 23-24", "start_capital": 18028240,
             "monthly_table": "legacy_monthly_fy2324", "trades_table": "legacy_trades_fy2324",
             "start_date": "2023-04-01", "source": "legacy"},
            {"fy": "2024-25", "label": "FY 24-25", "start_capital": 28000000,
             "monthly_table": "legacy_monthly_fy2425", "trades_table": "legacy_trades_fy2425",
             "start_date": "2024-04-01", "source": "legacy"},
            {"fy": "2025-26", "label": "FY 25-26", "start_capital": 50000000,
             "monthly_table": "legacy_monthly_summary", "trades_table": "legacy_trades",
             "start_date": "2025-04-01", "source": "legacy"},
        ]

        # ═══ User scoping: only show FYs this user has configured ═══
        filtered_fy_config = []
        for fc in fy_config:
            user_base = get_user_base_capital(cur, g.user_id, fc["fy"])
            if user_base is not None:
                fc_copy = dict(fc)
                fc_copy["start_capital"] = int(user_base)
                filtered_fy_config.append(fc_copy)

        # FY 2026-27 lives in journal_trades_computed (no legacy monthly table)
        # Synthesize a config entry so the same timeline/DD code path handles it.
        base_2627 = get_user_base_capital(cur, g.user_id, "2026-27")
        if base_2627 is not None:
            filtered_fy_config.append({
                "fy": "2026-27", "label": "FY 26-27", "start_capital": int(base_2627),
                "monthly_table": None, "trades_table": "journal_trades_computed",
                "start_date": "2026-04-01", "source": "journal",
            })

        # ═══ Restrict to a single FY if requested ═══
        if fy_param != "all":
            filtered_fy_config = [fc for fc in filtered_fy_config if fc["fy"] == fy_param]

        if not filtered_fy_config:
            return jsonify({"error": "setup_required", "message": "Set your FY base capital first"}), 400
        fy_config = filtered_fy_config

        month_dates = [
            "04-30", "05-31", "06-30", "07-31", "08-31", "09-30",
            "10-31", "11-30", "12-31", "01-31", "02-28", "03-31",
        ]

        # ─── Step 1: Build monthly timeline with actual PF values ───
        timeline = []  # [{date, month_label, fy, pf_value, month_pct, month_pl}]

        for fc in fy_config:
            base = float(fc["start_capital"])
            start_year = int(fc["fy"].split("-")[0])

            if fc.get("source") == "journal":
                # Build monthly summary on the fly from journal_trades_computed.
                # month_order: 1=Apr ... 12=Mar (matches legacy_monthly_* tables).
                end_year = start_year + 1
                cur.execute(
                    """
                    SELECT month_label,
                           CASE WHEN EXTRACT(MONTH FROM MIN(trade_date))::int >= 4
                                THEN EXTRACT(MONTH FROM MIN(trade_date))::int - 3
                                ELSE EXTRACT(MONTH FROM MIN(trade_date))::int + 9
                           END AS month_order,
                           ROUND((SUM(realized_pl) / %s * 100)::numeric, 2) AS after_charges,
                           ROUND(SUM(realized_pl)::numeric) AS net_pf_impact
                    FROM journal_trades_computed
                    WHERE user_id = %s
                      AND position_status = 'Closed'
                      AND trade_date >= %s::date
                      AND trade_date < %s::date
                    GROUP BY month_label
                    ORDER BY MIN(trade_date)
                    """,
                    (base, str(g.user_id), f"{start_year}-04-01", f"{end_year}-04-01"),
                )
                months = cur.fetchall()
            else:
                cur.execute(f"""
                    SELECT month_label, month_order, after_charges, net_pf_impact
                    FROM {fc['monthly_table']} ORDER BY month_order
                """)
                months = cur.fetchall()

            running = base
            # Add starting capital as first data point (so peak includes capital infusions)
            start_mo = months[0]["month_order"] if months else 1
            start_yr = start_year if start_mo <= 9 else start_year + 1

            # Calculate tax/adjustment from previous FY ending value
            prev_end = timeline[-1]["pf_value"] if timeline else 0
            fy_gap = round(base - prev_end) if prev_end > 0 else 0
            # Positive gap = capital added, Negative gap = tax paid + withdrawals
            gap_label = None
            if prev_end > 0 and fy_gap != 0:
                if fy_gap < 0:
                    gap_label = f"Tax & adj: -₹{abs(fy_gap) / 100000:.1f}L"
                else:
                    gap_label = f"Capital added: +₹{fy_gap / 100000:.1f}L"

            timeline.append({
                "date": f"{start_year}-04-01",
                "month_label": f"Start {fc['label']}",
                "fy": fc["fy"],
                "pf_value": round(base),
                "month_pct": 0,
                "month_pl": 0,
                "pf_at_start_of_month": round(base),
                "is_start": True,
                "fy_gap": fy_gap,
                "fy_gap_label": gap_label,
            })
            is_journal = fc.get("source") == "journal"
            for m in months:
                if is_journal:
                    # journal: net_pf_impact is the raw realized P&L for the month;
                    # compute pct off the running PF at month start to keep the
                    # compounding consistent with legacy_monthly_summary semantics.
                    month_pl = round(float(m["net_pf_impact"] or 0))
                    pct = round(month_pl / running * 100, 2) if running else 0.0
                else:
                    pct = float(m["after_charges"] or 0)
                    month_pl = round((pct / 100) * running)
                running += month_pl
                mo = m["month_order"]
                yr = start_year if mo <= 9 else start_year + 1
                dt = f"{yr}-{month_dates[mo-1]}"

                timeline.append({
                    "date": dt,
                    "month_label": m["month_label"],
                    "fy": fc["fy"],
                    "pf_value": round(running),
                    "month_pct": round(pct, 2),
                    "month_pl": month_pl,
                    "pf_at_start_of_month": round(running - month_pl),
                })

        # ─── Step 2: Compute drawdown from peak (₹ based) ───
        # Reset peak at FY boundaries so tax/capital changes don't create phantom drawdowns
        peak_value = 0
        prev_fy = None
        for i, m in enumerate(timeline):
            # Reset peak when FY changes (start entries mark new FY capital)
            if m.get("is_start") or m.get("fy") != prev_fy:
                peak_value = m["pf_value"]
                prev_fy = m.get("fy")
            if m["pf_value"] > peak_value:
                peak_value = m["pf_value"]
            dd_pct = round(((m["pf_value"] - peak_value) / peak_value) * 100, 2) if peak_value > 0 else 0
            dd_amount = m["pf_value"] - peak_value
            timeline[i]["peak_value"] = peak_value
            timeline[i]["dd_pct"] = dd_pct
            timeline[i]["dd_amount"] = dd_amount

        # ─── Step 3: Get Smallcap 100 monthly returns ───
        # When restricted to a single FY, anchor the SC100 query to that FY's
        # boundaries so we don't drag in irrelevant prior-year data.
        sc_start = fy_config[0]["start_date"]
        # Pull one extra month before so LAG() can compute the first month's pct
        sc_start_year, sc_start_month = sc_start.split("-")[0], sc_start.split("-")[1]
        sc_pre_year = int(sc_start_year) if int(sc_start_month) > 1 else int(sc_start_year) - 1
        sc_pre_month = int(sc_start_month) - 1 if int(sc_start_month) > 1 else 12
        sc_pre = f"{sc_pre_year:04d}-{sc_pre_month:02d}-01"
        cur.execute(
            """
            WITH monthly_close AS (
                SELECT date_trunc('month', date) as month,
                    (ARRAY_AGG(close ORDER BY date DESC))[1] as close_val
                FROM candles_indices
                WHERE symbol = 'NIFTY SMALLCAP 100' AND date >= %s::date
                GROUP BY date_trunc('month', date)
            )
            SELECT month::date, close_val,
                ((close_val / LAG(close_val) OVER (ORDER BY month)) - 1)::numeric * 100 as pct
            FROM monthly_close ORDER BY month
            """,
            (sc_pre,),
        )
        sc100_data = {}
        for row in cur.fetchall():
            # Map to our month format (use month end)
            month_key = str(row["month"])[:7]  # "2023-04"
            sc100_data[month_key] = {
                "close": float(row["close_val"]),
                "pct": round(float(row["pct"]), 2) if row["pct"] else None,
            }

        # Attach SC100 data to timeline
        for i, m in enumerate(timeline):
            mk = m["date"][:7]
            sc = sc100_data.get(mk, {})
            timeline[i]["sc100_pct"] = sc.get("pct")
            # Relative performance: you vs market
            if sc.get("pct") is not None:
                timeline[i]["relative_pct"] = round(m["month_pct"] - sc["pct"], 2)
            else:
                timeline[i]["relative_pct"] = None

        # ─── Step 4: Identify drawdown periods ───
        # A DD period starts when dd_pct goes below 0 and ends when it returns to 0
        dd_periods = []
        in_dd = False
        dd_start = None
        dd_trough_idx = None
        dd_trough_pct = 0

        for i, m in enumerate(timeline):
            if m["dd_pct"] < 0:
                if not in_dd:
                    in_dd = True
                    dd_start = i
                    dd_trough_idx = i
                    dd_trough_pct = m["dd_pct"]
                elif m["dd_pct"] < dd_trough_pct:
                    dd_trough_idx = i
                    dd_trough_pct = m["dd_pct"]
            else:
                if in_dd:
                    dd_periods.append({
                        "start_idx": dd_start,
                        "trough_idx": dd_trough_idx,
                        "recovery_idx": i,
                        "start_month": timeline[dd_start]["month_label"],
                        "trough_month": timeline[dd_trough_idx]["month_label"],
                        "recovery_month": timeline[i]["month_label"],
                        "max_dd_pct": round(dd_trough_pct, 2),
                        "max_dd_amount": timeline[dd_trough_idx]["dd_amount"],
                        "peak_value": timeline[dd_start]["peak_value"],
                        "trough_value": timeline[dd_trough_idx]["pf_value"],
                        "dd_months": dd_trough_idx - dd_start + 1,
                        "recovery_months": i - dd_trough_idx,
                        "total_months": i - dd_start,
                        "fy": timeline[dd_start]["fy"],
                    })
                    in_dd = False

        # Handle ongoing drawdown (not yet recovered)
        if in_dd and dd_start is not None:
            dd_periods.append({
                "start_idx": dd_start,
                "trough_idx": dd_trough_idx,
                "recovery_idx": None,
                "start_month": timeline[dd_start]["month_label"],
                "trough_month": timeline[dd_trough_idx]["month_label"],
                "recovery_month": None,
                "max_dd_pct": round(dd_trough_pct, 2),
                "max_dd_amount": timeline[dd_trough_idx]["dd_amount"],
                "peak_value": timeline[dd_start]["peak_value"],
                "trough_value": timeline[dd_trough_idx]["pf_value"],
                "dd_months": dd_trough_idx - dd_start + 1,
                "recovery_months": None,
                "total_months": len(timeline) - dd_start,
                "fy": timeline[dd_start]["fy"],
                "ongoing": True,
            })

        # ─── Step 5: Get trades during each DD period ───
        for dp in dd_periods:
            start_date = timeline[dp["start_idx"]]["date"]
            end_idx = dp["recovery_idx"] if dp["recovery_idx"] else len(timeline) - 1
            end_date = timeline[end_idx]["date"]
            fy = dp["fy"]

            # Find which FY configs overlap with this DD period
            dd_months_labels = [timeline[j]["month_label"] for j in range(dp["start_idx"], end_idx + 1)]

            # Get trades from all relevant FY tables during the DD period months
            dd_trades = []
            for fc in fy_config:
                try:
                    if fc.get("source") == "journal":
                        # journal_trades_computed: must filter by user_id and
                        # only Closed positions (matches outliers/advanced-v2).
                        cur.execute(
                            """
                            SELECT symbol, month_label, realized_pl, realized_pl_pct,
                                   impact_on_pf, is_winner, buy_value
                            FROM journal_trades_computed
                            WHERE user_id = %s
                              AND position_status = 'Closed'
                              AND month_label = ANY(%s)
                            ORDER BY realized_pl ASC
                            """,
                            (str(g.user_id), dd_months_labels),
                        )
                    else:
                        cur.execute(f"""
                            SELECT symbol, month_label, realized_pl, realized_pl_pct,
                                   impact_on_pf, is_winner, buy_value
                            FROM {fc['trades_table']}
                            WHERE user_id = %s
                              AND month_label = ANY(%s)
                            ORDER BY realized_pl ASC
                        """, (str(g.user_id), dd_months_labels))
                    dd_trades.extend([dict(r) for r in cur.fetchall()])
                except Exception:
                    pass

            # Sort by impact (worst first)
            dd_trades.sort(key=lambda t: float(t.get("impact_on_pf", 0) or 0))

            # Compute trade stats during DD
            losers = [t for t in dd_trades if not t.get("is_winner")]
            winners = [t for t in dd_trades if t.get("is_winner")]
            total_loss = sum(float(t.get("realized_pl", 0) or 0) for t in losers)
            total_gain = sum(float(t.get("realized_pl", 0) or 0) for t in winners)

            # Trades with >1% PF impact
            big_losers = [t for t in losers if abs(float(t.get("impact_on_pf", 0) or 0)) >= 0.5]

            # Sector concentration in losses
            sector_losses = {}
            for t in losers:
                sym = t.get("symbol", "Unknown")
                pl = float(t.get("realized_pl", 0) or 0)
                sector_losses[sym] = sector_losses.get(sym, 0) + pl

            # SC100 performance during this DD period
            dd_sc100 = []
            for j in range(dp["start_idx"], end_idx + 1):
                if timeline[j].get("sc100_pct") is not None:
                    dd_sc100.append(timeline[j]["sc100_pct"])
            sc100_total = sum(dd_sc100) if dd_sc100 else None

            dp["trades"] = {
                "total_trades": len(dd_trades),
                "losers_count": len(losers),
                "winners_count": len(winners),
                "total_loss": round(total_loss),
                "total_gain": round(total_gain),
                "net_pl": round(total_loss + total_gain),
                "big_impact_trades": [{
                    "symbol": t["symbol"],
                    "month": t["month_label"],
                    "pl": round(float(t.get("realized_pl", 0) or 0)),
                    "pct": round(float(t.get("realized_pl_pct", 0) or 0), 2),
                    "impact": round(float(t.get("impact_on_pf", 0) or 0), 3),
                } for t in big_losers[:10]],
                "top_losers": [{
                    "symbol": t["symbol"],
                    "month": t["month_label"],
                    "pl": round(float(t.get("realized_pl", 0) or 0)),
                    "pct": round(float(t.get("realized_pl_pct", 0) or 0), 2),
                    "impact": round(float(t.get("impact_on_pf", 0) or 0), 3),
                } for t in losers[:10]],
                "top_winners": [{
                    "symbol": t["symbol"],
                    "month": t["month_label"],
                    "pl": round(float(t.get("realized_pl", 0) or 0)),
                    "impact": round(float(t.get("impact_on_pf", 0) or 0), 3),
                } for t in sorted(winners, key=lambda t: float(t.get("realized_pl", 0) or 0), reverse=True)[:5]],
            }
            dp["sc100_during_dd"] = round(sc100_total, 2) if sc100_total is not None else None

            # Generate insight for this DD period
            insights = []
            if sc100_total is not None and dp["max_dd_pct"] is not None:
                if abs(dp["max_dd_pct"]) < abs(sc100_total):
                    insights.append(f"Outperformed market: You fell {abs(dp['max_dd_pct'])}% while SC100 fell {abs(sc100_total):.1f}%")
                elif sc100_total > 0:
                    insights.append(f"Underperformed: Market was up {sc100_total:.1f}% but you were in drawdown")

            if big_losers:
                top3 = ", ".join([f"{t['symbol']} ({float(t.get('impact_on_pf', 0) or 0):.1f}%)" for t in big_losers[:3]])
                insights.append(f"Big impact trades: {top3}")

            if dp.get("recovery_months") is not None:
                if dp["recovery_months"] <= 1:
                    insights.append("Fast recovery — bounced back within 1 month")
                else:
                    insights.append(f"Recovery took {dp['recovery_months']} months")

            dp["insights"] = insights

        # ─── Step 6: Summary stats ───
        # Filter out the "Start" entries from monthly stats
        data_months = [m for m in timeline if not m.get("is_start")]
        neg_months = [m for m in data_months if m["month_pct"] < 0]
        pos_months = [m for m in data_months if m["month_pct"] >= 0]
        max_dd = min((m["dd_pct"] for m in timeline), default=0)
        max_dd_month = next((m for m in timeline if m["dd_pct"] == max_dd), None)

        # Consecutive negative months
        max_consec = 0
        current_consec = 0
        for m in data_months:
            if m["month_pct"] < 0:
                current_consec += 1
                max_consec = max(max_consec, current_consec)
            else:
                current_consec = 0

        # Months where you lost but market gained
        underperf_months = [m for m in data_months if m["month_pct"] < 0 and (m.get("sc100_pct") or 0) > 0]
        # Months where you gained but market lost
        outperf_months = [m for m in data_months if m["month_pct"] > 0 and (m.get("sc100_pct") or 0) < 0]

        summary = {
            "total_months": len(data_months),
            "negative_months": len(neg_months),
            "positive_months": len(pos_months),
            "win_rate_months": round(len(pos_months) / len(data_months) * 100, 1) if data_months else 0,
            "max_dd_pct": round(max_dd, 2),
            "max_dd_month": max_dd_month["month_label"] if max_dd_month else None,
            "max_dd_amount": max_dd_month["dd_amount"] if max_dd_month else 0,
            "max_consecutive_negative": max_consec,
            "avg_negative_month": round(sum(m["month_pct"] for m in neg_months) / len(neg_months), 2) if neg_months else 0,
            "avg_positive_month": round(sum(m["month_pct"] for m in pos_months) / len(pos_months), 2) if pos_months else 0,
            "dd_periods_count": len(dd_periods),
            "avg_recovery_months": round(sum(dp.get("recovery_months", 0) or 0 for dp in dd_periods if dp.get("recovery_months")) / max(len([dp for dp in dd_periods if dp.get("recovery_months")]), 1), 1),
            "underperformance_months": len(underperf_months),
            "outperformance_months": len(outperf_months),
            "underperf_details": [{
                "month": m["month_label"], "you": m["month_pct"], "market": m.get("sc100_pct")
            } for m in underperf_months],
            "outperf_details": [{
                "month": m["month_label"], "you": m["month_pct"], "market": m.get("sc100_pct")
            } for m in outperf_months],
        }

        # ─── Step 7: Generate overall insights ───
        overall_insights = []

        # Insight 1: DD magnitude
        if abs(max_dd) < 5:
            overall_insights.append({
                "type": "positive",
                "text": f"Maximum drawdown of {abs(max_dd)}% is excellent for an active trader. Institutional funds typically see 10-20%."
            })
        elif abs(max_dd) < 10:
            overall_insights.append({
                "type": "neutral",
                "text": f"Maximum drawdown of {abs(max_dd)}% is moderate. Consider tightening stop losses during similar market conditions."
            })

        # Insight 2: Recovery speed
        avg_recov = summary["avg_recovery_months"]
        if avg_recov <= 1.5:
            overall_insights.append({
                "type": "positive",
                "text": f"Average recovery of {avg_recov} months shows strong ability to bounce back. Your edge reasserts quickly."
            })

        # Insight 3: Market outperformance
        if summary["outperformance_months"] > summary["underperformance_months"]:
            overall_insights.append({
                "type": "positive",
                "text": f"You outperformed in {summary['outperformance_months']} down-market months vs underperformed in {summary['underperformance_months']} up-market months. Net alpha generator."
            })

        # Insight 4: Consecutive negative months
        if max_consec >= 3:
            overall_insights.append({
                "type": "warning",
                "text": f"Longest losing streak: {max_consec} consecutive months. Consider reducing position sizing after 2 negative months."
            })

        # Insight 5: Big trade concentration
        all_big_losers = []
        for dp in dd_periods:
            all_big_losers.extend(dp["trades"]["big_impact_trades"])
        if all_big_losers:
            avg_impact = sum(abs(t["impact"]) for t in all_big_losers) / len(all_big_losers)
            overall_insights.append({
                "type": "warning" if avg_impact > 0.8 else "neutral",
                "text": f"{len(all_big_losers)} trades hit >0.5% PF during drawdowns (avg {avg_impact:.2f}%). These are the ones worth studying."
            })

        # Clean dd_periods for JSON (remove idx references)
        for dp in dd_periods:
            dp.pop("start_idx", None)
            dp.pop("trough_idx", None)
            dp.pop("recovery_idx", None)

        # Filter out start-of-FY entries from timeline (only needed for peak calc)
        response_timeline = [m for m in timeline if not m.get("is_start")]

        return jsonify({
            "timeline": response_timeline,
            "dd_periods": dd_periods,
            "summary": summary,
            "insights": overall_insights,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[valvo-journal] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)
