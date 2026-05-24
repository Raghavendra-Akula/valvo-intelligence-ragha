"""
Daily insight endpoint — returns one personalised, deterministic insight
per (user, calendar date). Same user gets the same insight all day; a
fresh one tomorrow. The pool is populated only with insights that have
data (e.g. "longest hold" is skipped if you have no open positions).
"""
import hashlib
from datetime import date, datetime
from flask import Blueprint, g, jsonify
from extensions import limiter

dashboard_insights_bp = Blueprint("dashboard_insights", __name__)


def _get_db():
    from database.database import get_db
    return get_db()


def _close_db(conn):
    from database.database import close_db
    close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/daily-insight", methods=["GET"])
@limiter.limit("30 per minute")
def daily_insight():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"insight": None}), 401

    today_iso = date.today().isoformat()
    seed_hex = hashlib.md5(f"{user_id}|{today_iso}".encode()).hexdigest()
    seed = int(seed_hex, 16)

    candidates = []
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"insight": _fallback_insight()})
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")

        # ── Best active position (highest % gain on open book)
        cur.execute(
            """
            SELECT stock_name, entry_price, current_price, entry_date,
                   CASE WHEN entry_price > 0
                        THEN ((current_price - entry_price) / entry_price) * 100
                        ELSE 0 END AS pct
            FROM positions
            WHERE user_id = %s AND status = 'active'
              AND current_price IS NOT NULL AND entry_price > 0
            ORDER BY pct DESC
            LIMIT 1
            """,
            (user_id,),
        )
        r = cur.fetchone()
        if r and (r["pct"] or 0) > 0:
            candidates.append({
                "id": "best_active",
                "icon": "trending_up",
                "accent": "green",
                "title": "Your best open position",
                "body": f"{r['stock_name']} is leading your book",
                "value": f"+{float(r['pct']):.2f}%",
                "cta": {"label": "View positions", "route": "position"},
            })

        # ── Longest hold among active positions
        cur.execute(
            """
            SELECT stock_name, entry_date, (CURRENT_DATE - entry_date) AS days_held
            FROM positions
            WHERE user_id = %s AND status = 'active' AND entry_date IS NOT NULL
            ORDER BY entry_date ASC
            LIMIT 1
            """,
            (user_id,),
        )
        r = cur.fetchone()
        if r and r["days_held"] is not None and r["days_held"] >= 5:
            candidates.append({
                "id": "longest_hold",
                "icon": "schedule",
                "accent": "blue",
                "title": "Your oldest open position",
                "body": f"{r['stock_name']} — held for a while",
                "value": f"{int(r['days_held'])} days",
                "cta": {"label": "Open in Positions", "route": "position"},
            })

        # ── Best closed trade all-time (by %)
        cur.execute(
            """
            SELECT stock_name, total_pnl_pct, exit_date, current_r_multiple
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND total_pnl_pct IS NOT NULL
            ORDER BY total_pnl_pct DESC NULLS LAST
            LIMIT 1
            """,
            (user_id,),
        )
        r = cur.fetchone()
        if r and (r["total_pnl_pct"] or 0) > 0:
            r_val = r["current_r_multiple"]
            extra = f" · {float(r_val):.1f}R" if r_val and r_val > 0 else ""
            candidates.append({
                "id": "best_closed",
                "icon": "emoji_events",
                "accent": "amber",
                "title": "Your best closed trade",
                "body": f"{r['stock_name']}{extra}",
                "value": f"+{float(r['total_pnl_pct']):.2f}%",
                "cta": {"label": "Review journal", "route": "journal"},
            })

        # ── Win rate over the last 30 days of closed trades
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE total_pnl > 0)      AS wins,
                COUNT(*) FILTER (WHERE total_pnl IS NOT NULL) AS total
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND exit_date >= (CURRENT_DATE - 30)
            """,
            (user_id,),
        )
        r = cur.fetchone()
        if r and (r["total"] or 0) >= 3:
            wins = int(r["wins"] or 0)
            total = int(r["total"])
            pct = (wins / total) * 100 if total else 0
            candidates.append({
                "id": "win_rate_30d",
                "icon": "insights",
                "accent": "violet",
                "title": "Your 30-day win rate",
                "body": f"{wins} of {total} closed trades were winners",
                "value": f"{pct:.0f}%",
                "cta": {"label": "See analytics", "route": "trade-analytics"},
            })

        # ── Most recently closed trade
        cur.execute(
            """
            SELECT stock_name, exit_date, total_pnl_pct, total_pnl
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND exit_date IS NOT NULL AND total_pnl_pct IS NOT NULL
            ORDER BY exit_date DESC
            LIMIT 1
            """,
            (user_id,),
        )
        r = cur.fetchone()
        if r:
            pct = float(r["total_pnl_pct"] or 0)
            sign = "+" if pct >= 0 else ""
            candidates.append({
                "id": "recent_close",
                "icon": "history",
                "accent": "slate",
                "title": "Your most recent close",
                "body": f"{r['stock_name']} on {r['exit_date'].strftime('%d %b') if r['exit_date'] else ''}",
                "value": f"{sign}{pct:.2f}%",
                "cta": {"label": "Review journal", "route": "journal"},
            })

        # ── Portfolio risk-on snapshot (open exposure)
        cur.execute(
            """
            SELECT
                COUNT(*) AS n,
                COALESCE(SUM(entry_price * quantity), 0) AS invested
            FROM positions
            WHERE user_id = %s AND status = 'active'
            """,
            (user_id,),
        )
        r = cur.fetchone()
        if r and (r["n"] or 0) > 0:
            invested = float(r["invested"] or 0)
            if invested >= 10000:
                lakh = invested / 100000
                val = f"₹{lakh:.1f}L" if lakh >= 1 else f"₹{invested/1000:.1f}K"
                candidates.append({
                    "id": "invested",
                    "icon": "account_balance_wallet",
                    "accent": "blue",
                    "title": "Capital at work today",
                    "body": f"{int(r['n'])} active position{'s' if r['n'] != 1 else ''}",
                    "value": val,
                    "cta": {"label": "View positions", "route": "position"},
                })

        # Deterministic pick
        if not candidates:
            return jsonify({"insight": _fallback_insight()})

        chosen = candidates[seed % len(candidates)]
        chosen["date"] = today_iso
        chosen["pool_size"] = len(candidates)
        return jsonify({"insight": chosen})

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[daily-insight] error: {e}")
        return jsonify({"insight": _fallback_insight()})
    finally:
        if conn:
            _close_db(conn)


def _fallback_insight():
    """Shown when the user has no position history yet."""
    return {
        "id": "welcome",
        "icon": "auto_awesome",
        "accent": "violet",
        "title": "Welcome back",
        "body": "Add a position to start tracking your edge.",
        "value": "",
        "cta": {"label": "Go to Positions", "route": "position"},
        "date": date.today().isoformat(),
        "pool_size": 0,
    }


def _fy_start():
    """India FY starts 1 Apr; returns the date for the current FY."""
    today = date.today()
    if today.month >= 4:
        return date(today.year, 4, 1)
    return date(today.year - 1, 4, 1)


@dashboard_insights_bp.route("/api/dashboard/equity-curve", methods=["GET"])
@limiter.limit("60 per minute")
def equity_curve():
    """Last-N-day portfolio_value series for the logged-in user."""
    from flask import request
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    days = max(1, min(int(request.args.get("days", 90)), 365))
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"series": []}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT date_ist, portfolio_value, day_pnl, day_pnl_pct
            FROM portfolio_snapshots
            WHERE user_id = %s AND date_ist >= CURRENT_DATE - %s
            ORDER BY date_ist ASC
            """,
            (user_id, days),
        )
        rows = cur.fetchall()
        series = [
            {
                "date": r["date_ist"].isoformat() if r["date_ist"] else None,
                "value": float(r["portfolio_value"] or 0),
                "day_pnl": float(r["day_pnl"] or 0),
                "day_pnl_pct": float(r["day_pnl_pct"] or 0),
            }
            for r in rows
        ]
        return jsonify({"series": series})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[equity-curve] error: {e}")
        return jsonify({"series": []}), 500
    finally:
        if conn:
            _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/fy-extremes", methods=["GET"])
@limiter.limit("60 per minute")
def fy_extremes():
    """Best & worst closed positions in the current Indian FY (1 Apr–31 Mar)."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    fy_start = _fy_start()
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"best": None, "worst": None}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        # Best
        cur.execute(
            """
            SELECT stock_name, security_id, total_pnl_pct, total_pnl,
                   entry_date, entry_price, exit_date, exit_price, current_r_multiple
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND total_pnl_pct IS NOT NULL
              AND exit_date >= %s
            ORDER BY total_pnl_pct DESC NULLS LAST
            LIMIT 1
            """,
            (user_id, fy_start),
        )
        best_row = cur.fetchone()
        # Worst
        cur.execute(
            """
            SELECT stock_name, security_id, total_pnl_pct, total_pnl,
                   entry_date, entry_price, exit_date, exit_price, current_r_multiple
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND total_pnl_pct IS NOT NULL
              AND exit_date >= %s
            ORDER BY total_pnl_pct ASC NULLS LAST
            LIMIT 1
            """,
            (user_id, fy_start),
        )
        worst_row = cur.fetchone()

        def _as(p):
            if not p: return None
            return {
                "stock":       p["stock_name"],
                "security_id": p["security_id"],
                "pct":         float(p["total_pnl_pct"]) if p["total_pnl_pct"] is not None else None,
                "pnl":         float(p["total_pnl"]) if p["total_pnl"] is not None else None,
                "r_multiple":  float(p["current_r_multiple"]) if p["current_r_multiple"] is not None else None,
                "entry_price": float(p["entry_price"]) if p["entry_price"] is not None else None,
                "exit_price":  float(p["exit_price"]) if p["exit_price"] is not None else None,
                "entry_date":  p["entry_date"].isoformat() if p["entry_date"] else None,
                "exit_date":   p["exit_date"].isoformat() if p["exit_date"] else None,
                "date":        p["exit_date"].isoformat() if p["exit_date"] else None,
            }

        return jsonify({
            "fy_start": fy_start.isoformat(),
            "best":  _as(best_row),
            "worst": _as(worst_row),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[fy-extremes] error: {e}")
        return jsonify({"best": None, "worst": None}), 500
    finally:
        if conn:
            _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/sector-allocation", methods=["GET"])
@limiter.limit("60 per minute")
def sector_allocation():
    """Capital deployed by sector across active positions. Resolves in priority:
    primary stock_themes_v2 (most granular framework tag — e.g. 'AI Compute Hardware')
    → stock_universe.valvo_sector (v2 canonical) → stock_universe.sector (raw NSE)
    → journal_trades.sector (user-tagged) → 'Uncategorized'."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"buckets": [], "total": 0}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT COALESCE(
                     NULLIF(th.name, ''),
                     NULLIF(su.valvo_sector, ''),
                     NULLIF(su.sector, ''),
                     NULLIF(jt.sector, ''),
                     'Uncategorized'
                   ) AS sector,
                   SUM(p.entry_price * p.quantity) AS invested,
                   COUNT(*) AS n
            FROM positions p
            LEFT JOIN stock_universe su ON su.security_id = p.security_id
            LEFT JOIN LATERAL (
              SELECT t.name FROM stock_themes_v2 st
              JOIN themes_v2 t ON t.slug = st.theme_slug
              WHERE st.security_id = p.security_id AND st.is_primary = true
              LIMIT 1
            ) th ON true
            LEFT JOIN LATERAL (
              SELECT sector FROM journal_trades
              WHERE user_id = p.user_id AND security_id = p.security_id
              ORDER BY trade_date DESC NULLS LAST LIMIT 1
            ) jt ON true
            WHERE p.user_id = %s AND p.status = 'active'
              AND p.entry_price IS NOT NULL AND p.quantity IS NOT NULL
            GROUP BY 1
            ORDER BY invested DESC NULLS LAST
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        total = sum(float(r["invested"] or 0) for r in rows)
        buckets = [
            {
                "sector": r["sector"],
                "invested": float(r["invested"] or 0),
                "count":    int(r["n"] or 0),
                "pct":      round((float(r["invested"] or 0) / total) * 100, 2) if total > 0 else 0,
            }
            for r in rows
        ]
        return jsonify({"buckets": buckets, "total": total})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[sector-allocation] error: {e}")
        return jsonify({"buckets": [], "total": 0}), 500
    finally:
        if conn:
            _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/streak", methods=["GET"])
@limiter.limit("60 per minute")
def streak():
    """Current win/loss streak + longest win streak ever, lifetime."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"current": 0, "current_kind": None, "longest_win": 0}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT total_pnl
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND total_pnl IS NOT NULL AND exit_date IS NOT NULL
            ORDER BY exit_date ASC, id ASC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        outcomes = ["win" if (r["total_pnl"] or 0) > 0
                    else "loss" if (r["total_pnl"] or 0) < 0
                    else "be"
                    for r in rows]
        # Longest winning streak ever
        longest_win = 0
        cur_run = 0
        for o in outcomes:
            if o == "win":
                cur_run += 1
                longest_win = max(longest_win, cur_run)
            else:
                cur_run = 0
        # Current streak (from end, of one kind)
        current = 0
        current_kind = None
        for o in reversed(outcomes):
            if o == "be":
                continue
            if current_kind is None:
                current_kind = o
                current = 1
            elif o == current_kind:
                current += 1
            else:
                break
        return jsonify({
            "total_closed": len(outcomes),
            "current": current,
            "current_kind": current_kind,
            "longest_win": longest_win,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[streak] error: {e}")
        return jsonify({"current": 0, "current_kind": None, "longest_win": 0}), 500
    finally:
        if conn:
            _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/my-stats", methods=["GET"])
@limiter.limit("60 per minute")
def my_stats():
    """
    Personal performance trio for the dashboard's right column:
    win rate, best closed trade %, worst closed trade %.

    Window: lifetime closed positions for the logged-in user. Returns
    nulls inside each block when there are no closed trades yet.
    """
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401

    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"win_rate": None, "best_trade": None, "worst_trade": None}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")

        # Win rate (lifetime closed positions where total_pnl is known)
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE total_pnl > 0)         AS wins,
                COUNT(*) FILTER (WHERE total_pnl IS NOT NULL) AS total
            FROM positions
            WHERE user_id = %s AND status <> 'active'
            """,
            (user_id,),
        )
        r = cur.fetchone()
        wins = int(r["wins"] or 0) if r else 0
        total = int(r["total"] or 0) if r else 0
        win_rate = (
            {"wins": wins, "total": total, "pct": round((wins / total) * 100, 1)}
            if total > 0 else None
        )

        # Best closed trade (highest total_pnl_pct)
        cur.execute(
            """
            SELECT stock_name, total_pnl_pct, exit_date, current_r_multiple
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND total_pnl_pct IS NOT NULL
            ORDER BY total_pnl_pct DESC NULLS LAST
            LIMIT 1
            """,
            (user_id,),
        )
        r = cur.fetchone()
        best_trade = (
            {
                "stock": r["stock_name"],
                "pct": float(r["total_pnl_pct"]),
                "r_multiple": float(r["current_r_multiple"]) if r["current_r_multiple"] is not None else None,
                "date": r["exit_date"].isoformat() if r["exit_date"] else None,
            }
            if r else None
        )

        # Worst closed trade (lowest total_pnl_pct)
        cur.execute(
            """
            SELECT stock_name, total_pnl_pct, exit_date, current_r_multiple
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND total_pnl_pct IS NOT NULL
            ORDER BY total_pnl_pct ASC NULLS LAST
            LIMIT 1
            """,
            (user_id,),
        )
        r = cur.fetchone()
        worst_trade = (
            {
                "stock": r["stock_name"],
                "pct": float(r["total_pnl_pct"]),
                "r_multiple": float(r["current_r_multiple"]) if r["current_r_multiple"] is not None else None,
                "date": r["exit_date"].isoformat() if r["exit_date"] else None,
            }
            if r else None
        )

        return jsonify({
            "win_rate": win_rate,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[my-stats] error: {e}")
        return jsonify({"error": "internal"}), 500
    finally:
        if conn:
            _close_db(conn)


# ─────────────────────────────────────────────────────────────
#  Additional dashboard cards / trios (V4 rotating slots)
# ─────────────────────────────────────────────────────────────

@dashboard_insights_bp.route("/api/dashboard/monthly-pnl", methods=["GET"])
@limiter.limit("60 per minute")
def monthly_pnl():
    """Realised P&L grouped by exit_date month, last 12 months."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"months": []}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT date_trunc('month', exit_date)::date AS m,
                   SUM(CASE WHEN total_pnl > 0 THEN total_pnl ELSE 0 END)  AS profit,
                   COUNT(*)  FILTER (WHERE total_pnl > 0)                  AS profit_trades,
                   SUM(CASE WHEN total_pnl < 0 THEN total_pnl ELSE 0 END)  AS loss,
                   COUNT(*)  FILTER (WHERE total_pnl < 0)                  AS loss_trades,
                   SUM(total_pnl) AS pnl,
                   COUNT(*) AS n
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND exit_date IS NOT NULL AND total_pnl IS NOT NULL
              AND exit_date >= (CURRENT_DATE - INTERVAL '12 months')
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        months = [{
            "month":         r["m"].isoformat() if r["m"] else None,
            "pnl":           float(r["pnl"] or 0),
            "trades":        int(r["n"] or 0),
            "profit":        float(r["profit"] or 0),
            "profit_trades": int(r["profit_trades"] or 0),
            "loss":          float(r["loss"] or 0),
            "loss_trades":   int(r["loss_trades"] or 0),
        } for r in rows]
        return jsonify({"months": months})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[monthly-pnl] error: {e}")
        return jsonify({"months": []}), 500
    finally:
        if conn: _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/r-distribution", methods=["GET"])
@limiter.limit("60 per minute")
def r_distribution():
    """Histogram of closed-trade R-multiples + summary stats (avg win R,
    avg loss R, expectancy, total)."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"buckets": [], "summary": {}}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT current_r_multiple AS r
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND current_r_multiple IS NOT NULL
            """,
            (user_id,),
        )
        rs = [float(r["r"]) for r in cur.fetchall()]
        # Bucketing: <-2, -2..-1, -1..0, 0..1, 1..2, 2..3, >3
        edges = [(-99, -2), (-2, -1), (-1, 0), (0, 1), (1, 2), (2, 3), (3, 99)]
        labels = ["<-2R", "-2 to -1R", "-1 to 0R", "0 to 1R", "1 to 2R", "2 to 3R", ">3R"]
        buckets = []
        for (lo, hi), label in zip(edges, labels):
            n = sum(1 for v in rs if lo <= v < hi)
            buckets.append({"label": label, "count": n, "tone": "win" if lo >= 0 else "loss"})
        wins = [v for v in rs if v > 0]
        losses = [v for v in rs if v < 0]
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        win_rate = (len(wins) / len(rs)) if rs else 0
        expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss) if rs else 0
        return jsonify({
            "buckets": buckets,
            "summary": {
                "total":      len(rs),
                "wins":       len(wins),
                "losses":     len(losses),
                "avg_win_r":  round(avg_win, 2),
                "avg_loss_r": round(avg_loss, 2),
                "expectancy": round(expectancy, 2),
                "win_rate":   round(win_rate * 100, 1),
            },
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[r-distribution] error: {e}")
        return jsonify({"buckets": [], "summary": {}}), 500
    finally:
        if conn: _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/hold-times", methods=["GET"])
@limiter.limit("60 per minute")
def hold_times():
    """Per-trade hold lengths + winners/losers averages + recent
    daily P&L pulses (for a heatmap)."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"avg_winners": None, "avg_losers": None, "days": []}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT (exit_date - entry_date) AS days,
                   total_pnl
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND exit_date IS NOT NULL AND entry_date IS NOT NULL
              AND total_pnl IS NOT NULL
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        winners = [int(r["days"]) for r in rows if r["days"] is not None and (r["total_pnl"] or 0) > 0]
        losers  = [int(r["days"]) for r in rows if r["days"] is not None and (r["total_pnl"] or 0) < 0]
        avg_w = round(sum(winners) / len(winners), 1) if winners else None
        avg_l = round(sum(losers)  / len(losers),  1) if losers  else None

        # Daily-aggregated P&L for the heatmap, last 90 days
        cur.execute(
            """
            SELECT exit_date AS d, SUM(total_pnl) AS pnl, COUNT(*) AS n
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND exit_date >= (CURRENT_DATE - 90)
            GROUP BY exit_date
            ORDER BY exit_date ASC
            """,
            (user_id,),
        )
        days = [{
            "date": r["d"].isoformat() if r["d"] else None,
            "pnl":  float(r["pnl"] or 0),
            "trades": int(r["n"] or 0),
        } for r in cur.fetchall()]
        return jsonify({
            "avg_winners": avg_w,
            "avg_losers":  avg_l,
            "total_closed": len(rows),
            "days": days,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[hold-times] error: {e}")
        return jsonify({"avg_winners": None, "avg_losers": None, "days": []}), 500
    finally:
        if conn: _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/positions-snapshot", methods=["GET"])
@limiter.limit("60 per minute")
def positions_snapshot():
    """Open-book risk + counts + 3 longest-held active winners."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(entry_price * quantity), 0) AS invested,
                   COALESCE(SUM(GREATEST(risk_pct, 0)), 0) AS open_risk
            FROM positions
            WHERE user_id = %s AND status = 'active'
            """,
            (user_id,),
        )
        snap = cur.fetchone() or {}
        cur.execute(
            """
            SELECT stock_name, entry_date, current_price, entry_price,
                   (CURRENT_DATE - entry_date) AS days_held
            FROM positions
            WHERE user_id = %s AND status = 'active'
              AND entry_date IS NOT NULL
            ORDER BY entry_date ASC
            LIMIT 3
            """,
            (user_id,),
        )
        oldest = []
        for r in cur.fetchall():
            entry = float(r["entry_price"] or 0)
            cur_p = float(r["current_price"] or entry)
            pct = ((cur_p - entry) / entry) * 100 if entry else 0
            oldest.append({
                "stock":     r["stock_name"],
                "days":      int(r["days_held"]) if r["days_held"] is not None else None,
                "pct":       round(pct, 2),
            })
        return jsonify({
            "open_count": int(snap.get("n") or 0),
            "invested":   float(snap.get("invested") or 0),
            "open_risk":  float(snap.get("open_risk") or 0),
            "oldest":     oldest,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[positions-snapshot] error: {e}")
        return jsonify({}), 500
    finally:
        if conn: _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/this-month", methods=["GET"])
@limiter.limit("60 per minute")
def this_month():
    """MTD: trades closed, win rate, net P&L."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE total_pnl > 0) AS wins,
                   COUNT(*) FILTER (WHERE total_pnl IS NOT NULL) AS total,
                   COALESCE(SUM(total_pnl), 0) AS pnl
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND exit_date >= date_trunc('month', CURRENT_DATE)
            """,
            (user_id,),
        )
        r = cur.fetchone() or {}
        total = int(r.get("total") or 0)
        wins = int(r.get("wins") or 0)
        return jsonify({
            "trades": total,
            "wins":   wins,
            "win_rate": round((wins / total) * 100, 1) if total else None,
            "pnl":    float(r.get("pnl") or 0),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[this-month] error: {e}")
        return jsonify({}), 500
    finally:
        if conn: _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/discipline", methods=["GET"])
@limiter.limit("60 per minute")
def discipline():
    """Plan-followed % + avg self-rating from journal_trades."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT COUNT(*) FILTER (WHERE LOWER(plan_followed) IN ('yes','y','true')) AS plan_yes,
                   COUNT(*) FILTER (WHERE plan_followed IS NOT NULL AND plan_followed <> '') AS plan_total,
                   AVG(self_rating) FILTER (WHERE self_rating IS NOT NULL AND self_rating > 0) AS avg_rating,
                   COUNT(*) AS n
            FROM journal_trades
            WHERE user_id = %s
            """,
            (user_id,),
        )
        r = cur.fetchone() or {}
        plan_total = int(r.get("plan_total") or 0)
        plan_yes = int(r.get("plan_yes") or 0)
        return jsonify({
            "trades": int(r.get("n") or 0),
            "plan_followed_pct": round((plan_yes / plan_total) * 100, 1) if plan_total else None,
            "avg_rating": round(float(r["avg_rating"]), 1) if r.get("avg_rating") is not None else None,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[discipline] error: {e}")
        return jsonify({}), 500
    finally:
        if conn: _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/defensive", methods=["GET"])
@limiter.limit("60 per minute")
def defensive():
    """V2 'defensive' impact: total drawdown saved across closed trades."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT COALESCE(SUM(GREATEST(drawdown_saved, 0)), 0) AS saved,
                   COUNT(*) FILTER (WHERE drawdown_saved IS NOT NULL AND drawdown_saved > 0) AS saved_n,
                   COUNT(*) AS n
            FROM positions
            WHERE user_id = %s AND status <> 'active'
            """,
            (user_id,),
        )
        r = cur.fetchone() or {}
        saved = float(r.get("saved") or 0)
        saved_n = int(r.get("saved_n") or 0)
        n = int(r.get("n") or 0)
        return jsonify({
            "trades":           n,
            "drawdown_saved":   saved,
            "saves":            saved_n,
            "avg_saved":        round(saved / saved_n, 2) if saved_n else None,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[defensive] error: {e}")
        return jsonify({}), 500
    finally:
        if conn: _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/sector-edge", methods=["GET"])
@limiter.limit("60 per minute")
def sector_edge():
    """Avg P&L % by sector across closed trades. Returns best/worst + count."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT COALESCE(
                     NULLIF(th.name, ''),
                     NULLIF(su.valvo_sector, ''),
                     NULLIF(su.sector, ''),
                     NULLIF(jt.sector, ''),
                     'Uncategorized'
                   ) AS sector,
                   AVG(p.total_pnl_pct) AS avg_pct,
                   COUNT(*) AS n
            FROM positions p
            LEFT JOIN stock_universe su ON su.security_id = p.security_id
            LEFT JOIN LATERAL (
              SELECT t.name FROM stock_themes_v2 st
              JOIN themes_v2 t ON t.slug = st.theme_slug
              WHERE st.security_id = p.security_id AND st.is_primary = true
              LIMIT 1
            ) th ON true
            LEFT JOIN LATERAL (
              SELECT sector FROM journal_trades
              WHERE user_id = p.user_id AND security_id = p.security_id
              ORDER BY trade_date DESC NULLS LAST LIMIT 1
            ) jt ON true
            WHERE p.user_id = %s AND p.status <> 'active'
              AND p.total_pnl_pct IS NOT NULL
            GROUP BY 1
            HAVING COUNT(*) >= 1
            ORDER BY avg_pct DESC NULLS LAST
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return jsonify({"best": None, "worst": None, "sector_count": 0})
        best_row  = rows[0]
        worst_row = rows[-1]
        best  = {"sector": best_row["sector"],  "avg_pct": round(float(best_row["avg_pct"]),  2), "trades": int(best_row["n"])}
        # Only surface `worst` if it's actually negative — otherwise there is
        # no losing sector to call out.
        worst = None
        if worst_row["avg_pct"] is not None and float(worst_row["avg_pct"]) < 0:
            worst = {"sector": worst_row["sector"], "avg_pct": round(float(worst_row["avg_pct"]), 2), "trades": int(worst_row["n"])}
        return jsonify({
            "best":  best,
            "worst": worst,
            "sector_count": len(rows),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[sector-edge] error: {e}")
        return jsonify({}), 500
    finally:
        if conn: _close_db(conn)


@dashboard_insights_bp.route("/api/dashboard/volume", methods=["GET"])
@limiter.limit("60 per minute")
def volume():
    """Trade volume profile in the current FY: total trades, avg/month,
    most active month."""
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    fy_start = _fy_start()
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT date_trunc('month', exit_date)::date AS m, COUNT(*) AS n
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND exit_date >= %s
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            (user_id, fy_start),
        )
        rows = cur.fetchall()
        total = sum(int(r["n"] or 0) for r in rows)
        # months elapsed in the FY
        today = date.today()
        months_elapsed = max(1, (today.year - fy_start.year) * 12 + (today.month - fy_start.month) + 1)
        avg = round(total / months_elapsed, 1) if total else 0
        peak = max(rows, key=lambda r: int(r["n"] or 0)) if rows else None
        peak_dict = (
            {"month": peak["m"].strftime("%b %Y") if peak["m"] else None, "trades": int(peak["n"] or 0)}
            if peak else None
        )
        return jsonify({
            "fy_start": fy_start.isoformat(),
            "total":    total,
            "avg_per_month": avg,
            "peak":     peak_dict,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[volume] error: {e}")
        return jsonify({}), 500
    finally:
        if conn: _close_db(conn)


# Extend streak: also return longest LOSS streak (current /api/streak only
# computes longest_win). Re-implement here as /streak-detail to avoid
# breaking the V4 daily-insight pool.
@dashboard_insights_bp.route("/api/dashboard/streak-detail", methods=["GET"])
@limiter.limit("60 per minute")
def streak_detail():
    user_id = getattr(g, "user_id", None)
    if not user_id:
        return jsonify({"error": "auth required"}), 401
    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            """
            SELECT total_pnl
            FROM positions
            WHERE user_id = %s AND status <> 'active'
              AND total_pnl IS NOT NULL AND exit_date IS NOT NULL
            ORDER BY exit_date ASC, id ASC
            """,
            (user_id,),
        )
        outcomes = ["win" if (r["total_pnl"] or 0) > 0
                    else "loss" if (r["total_pnl"] or 0) < 0 else "be"
                    for r in cur.fetchall()]
        longest_win = longest_loss = run_w = run_l = 0
        for o in outcomes:
            if o == "win":   run_w += 1; longest_win  = max(longest_win,  run_w); run_l = 0
            elif o == "loss": run_l += 1; longest_loss = max(longest_loss, run_l); run_w = 0
            else:             run_w = run_l = 0
        current = 0; current_kind = None
        for o in reversed(outcomes):
            if o == "be": continue
            if current_kind is None: current_kind = o; current = 1
            elif o == current_kind: current += 1
            else: break
        return jsonify({
            "total_closed": len(outcomes),
            "current": current,
            "current_kind": current_kind,
            "longest_win": longest_win,
            "longest_loss": longest_loss,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[streak-detail] error: {e}")
        return jsonify({}), 500
    finally:
        if conn: _close_db(conn)
