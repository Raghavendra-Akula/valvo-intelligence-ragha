"""
Advanced Analytics — Outlier analysis, distribution, concentration, monthly trends.
Separate page in the sidebar.
"""
from flask import Blueprint, jsonify, g
from extensions import limiter

analytics_bp = Blueprint("analytics", __name__)

def _get_db():
    from database.database import get_db
    return get_db()

def _close_db(conn):
    from database.database import close_db
    close_db(conn)


@analytics_bp.route("/api/analytics/outliers", methods=["GET"])
@limiter.limit("60 per minute")
def outlier_analysis():
    """Complete outlier/distribution/concentration data for Advanced Analytics page.
    User-scoped. Supports ?fy=2023-24|2024-25|2025-26 (default: 2026-27)."""
    from flask import request as req
    fy = req.args.get("fy", "2026-27")
    conn = _get_db()
    try:
        cur = conn.cursor()
        from services.user_analytics_service import resolve_fy
        resolved = resolve_fy(cur, g.user_id, fy)
        if not resolved.get("allowed"):
            if resolved.get("needs_setup"):
                return jsonify({"error": "setup_required", "message": "Set your base capital first"}), 400
            return jsonify({"error": "FY not available"}), 403

        tbl = resolved["table"]
        base = resolved["base"]
        if resolved.get("user_filter"):
            where = f"user_id = '{g.user_id}'"
            if resolved.get("fy_filter"):
                where += f" AND fy = '{resolved['fy_filter']}'"
            # Journal view exposes both Open & Closed rows; only Closed rows
            # represent realized FY P&L the way Journal/Positions report it.
            # Without this filter, Open positions with partial exits inflate
            # analytics totals (was 24L vs Journal's 17.77L for FY26-27 admin).
            if resolved.get("source") == "journal":
                where += " AND position_status = 'Closed'"
            tbl = f"(SELECT * FROM {tbl} WHERE {where}) _uf"

        # ═══ COMPUTE IQR-BASED OUTLIER THRESHOLD FOR THIS FY ═══
        cur.execute(f"""
            SELECT
                PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY realized_pl_pct) as q1,
                PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY realized_pl_pct) as q3
            FROM {tbl} WHERE realized_pl > 0
        """)
        iqr_row = cur.fetchone()
        q1 = float(iqr_row["q1"] or 0)
        q3 = float(iqr_row["q3"] or 0)
        iqr = q3 - q1
        # Upper fence: Q3 + 1.5*IQR — trades above this are statistical outliers
        outlier_threshold = round(q3 + 1.5 * iqr, 2)
        # Clamp to reasonable range: minimum 3%, maximum 30%
        outlier_threshold = max(3.0, min(30.0, outlier_threshold))

        # All trades with R-multiples
        cur.execute(f"""
            WITH all_trades AS (
                SELECT symbol as name, realized_pl as pl, realized_pl_pct as move_pct,
                    ROUND((realized_pl_pct / 3.0)::numeric, 2) as r_multiple,
                    month_label as period, 'legacy' as src
                FROM {tbl}
            )
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN pl > 0 THEN 1 ELSE 0 END) as winners,
                SUM(CASE WHEN pl <= 0 THEN 1 ELSE 0 END) as losers,
                ROUND(SUM(CASE WHEN pl > 0 THEN pl ELSE 0 END)::numeric) as gross_profit,
                ROUND(ABS(SUM(CASE WHEN pl <= 0 THEN pl ELSE 0 END))::numeric) as gross_loss
            FROM all_trades
        """)
        summary = cur.fetchone()

        # ═══ WINNER DISTRIBUTION BY MOVE % ═══
        cur.execute(f"""
            WITH all_trades AS (
                SELECT realized_pl as pl, realized_pl_pct as m FROM {tbl} WHERE realized_pl > 0
            )
            SELECT
                COUNT(*) FILTER (WHERE m > 0 AND m <= 1) as w_0_1,
                COUNT(*) FILTER (WHERE m > 1 AND m <= 2) as w_1_2,
                COUNT(*) FILTER (WHERE m > 2 AND m <= 3) as w_2_3,
                COUNT(*) FILTER (WHERE m > 3 AND m <= 5) as w_3_5,
                COUNT(*) FILTER (WHERE m > 5 AND m <= 8) as w_5_8,
                COUNT(*) FILTER (WHERE m > 8 AND m <= 10) as w_8_10,
                COUNT(*) FILTER (WHERE m > 10) as w_10p,
                ROUND(SUM(pl) FILTER (WHERE m > 0 AND m <= 1)::numeric) as pl_0_1,
                ROUND(SUM(pl) FILTER (WHERE m > 1 AND m <= 2)::numeric) as pl_1_2,
                ROUND(SUM(pl) FILTER (WHERE m > 2 AND m <= 3)::numeric) as pl_2_3,
                ROUND(SUM(pl) FILTER (WHERE m > 3 AND m <= 5)::numeric) as pl_3_5,
                ROUND(SUM(pl) FILTER (WHERE m > 5 AND m <= 8)::numeric) as pl_5_8,
                ROUND(SUM(pl) FILTER (WHERE m > 8 AND m <= 10)::numeric) as pl_8_10,
                ROUND(SUM(pl) FILTER (WHERE m > 10)::numeric) as pl_10p
            FROM all_trades
        """)
        wd = cur.fetchone()

        # ═══ WINNER DISTRIBUTION BY R-MULTIPLE ═══
        cur.execute(f"""
            WITH all_trades AS (
                SELECT realized_pl as pl, ROUND((realized_pl_pct / 3.0)::numeric, 2) as r FROM {tbl} WHERE realized_pl > 0
            )
            SELECT
                COUNT(*) FILTER (WHERE r > 0 AND r <= 0.5) as r_0_05,
                COUNT(*) FILTER (WHERE r > 0.5 AND r <= 1) as r_05_1,
                COUNT(*) FILTER (WHERE r > 1 AND r <= 2) as r_1_2,
                COUNT(*) FILTER (WHERE r > 2 AND r <= 3) as r_2_3,
                COUNT(*) FILTER (WHERE r > 3 AND r <= 5) as r_3_5,
                COUNT(*) FILTER (WHERE r > 5) as r_5p,
                ROUND(SUM(pl) FILTER (WHERE r > 0 AND r <= 0.5)::numeric) as rpl_0_05,
                ROUND(SUM(pl) FILTER (WHERE r > 0.5 AND r <= 1)::numeric) as rpl_05_1,
                ROUND(SUM(pl) FILTER (WHERE r > 1 AND r <= 2)::numeric) as rpl_1_2,
                ROUND(SUM(pl) FILTER (WHERE r > 2 AND r <= 3)::numeric) as rpl_2_3,
                ROUND(SUM(pl) FILTER (WHERE r > 3 AND r <= 5)::numeric) as rpl_3_5,
                ROUND(SUM(pl) FILTER (WHERE r > 5)::numeric) as rpl_5p
            FROM all_trades
        """)
        rd = cur.fetchone()

        # ═══ LOSER DISTRIBUTION BY MOVE % ═══
        cur.execute(f"""
            WITH all_trades AS (
                SELECT realized_pl as pl, ABS(realized_pl_pct) as m FROM {tbl} WHERE realized_pl <= 0
            )
            SELECT
                COUNT(*) FILTER (WHERE m >= 0 AND m <= 0.5) as l_0_05,
                COUNT(*) FILTER (WHERE m > 0.5 AND m <= 1) as l_05_1,
                COUNT(*) FILTER (WHERE m > 1 AND m <= 2) as l_1_2,
                COUNT(*) FILTER (WHERE m > 2 AND m <= 3) as l_2_3,
                COUNT(*) FILTER (WHERE m > 3 AND m <= 5) as l_3_5,
                COUNT(*) FILTER (WHERE m > 5) as l_5p,
                ROUND(ABS(SUM(pl) FILTER (WHERE m >= 0 AND m <= 0.5))::numeric) as lpl_0_05,
                ROUND(ABS(SUM(pl) FILTER (WHERE m > 0.5 AND m <= 1))::numeric) as lpl_05_1,
                ROUND(ABS(SUM(pl) FILTER (WHERE m > 1 AND m <= 2))::numeric) as lpl_1_2,
                ROUND(ABS(SUM(pl) FILTER (WHERE m > 2 AND m <= 3))::numeric) as lpl_2_3,
                ROUND(ABS(SUM(pl) FILTER (WHERE m > 3 AND m <= 5))::numeric) as lpl_3_5,
                ROUND(ABS(SUM(pl) FILTER (WHERE m > 5))::numeric) as lpl_5p
            FROM all_trades
        """)
        ld = cur.fetchone()

        # ═══ CONCENTRATION — Cumulative top N ═══
        cur.execute(f"""
            WITH all_trades AS (
                SELECT realized_pl as pl FROM {tbl} WHERE realized_pl > 0
            ), ranked AS (
                SELECT pl, ROW_NUMBER() OVER (ORDER BY pl DESC) as rn FROM all_trades
            )
            SELECT rn as n, ROUND(SUM(pl) OVER (ORDER BY rn)::numeric) as cum_pl
            FROM ranked WHERE rn <= 30
        """)
        concentration = [{"n": r["n"], "cum_pl": float(r["cum_pl"])} for r in cur.fetchall()]

        # ═══ ALL WINNERS with details (for slider explorer) ═══
        cur.execute(f"""
            WITH all_trades AS (
                SELECT symbol as name, realized_pl as pl, realized_pl_pct as move_pct,
                    ROUND((realized_pl_pct / 3.0)::numeric, 2) as r_multiple, month_label as period
                FROM {tbl} WHERE realized_pl > 0
            )
            SELECT name, ROUND(pl::numeric) as pl, ROUND(move_pct::numeric, 2) as move_pct,
                r_multiple, period
            FROM all_trades ORDER BY pl DESC
        """)
        top_winners = [dict(r) for r in cur.fetchall()]

        # ═══ ALL LOSERS with details (for slider explorer) ═══
        cur.execute(f"""
            WITH all_trades AS (
                SELECT symbol as name, realized_pl as pl, ABS(realized_pl_pct) as move_pct,
                    ROUND(ABS(realized_pl_pct / 3.0)::numeric, 2) as r_multiple, month_label as period
                FROM {tbl} WHERE realized_pl <= 0
            )
            SELECT name, ROUND(pl::numeric) as pl, ROUND(move_pct::numeric, 2) as move_pct,
                r_multiple, period
            FROM all_trades ORDER BY pl ASC
        """)
        all_losers = [dict(r) for r in cur.fetchall()]

        # ═══ MONTHLY OUTLIER TREND (uses IQR-computed threshold) ═══
        ot = outlier_threshold  # computed above
        cur.execute(f"""
            WITH all_trades AS (
                SELECT realized_pl as pl, realized_pl_pct as move_pct,
                    ROUND((realized_pl_pct / 3.0)::numeric, 2) as r_multiple,
                    month_label as period
                FROM {tbl}
            )
            SELECT period,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE pl > 0) as wins,
                COUNT(*) FILTER (WHERE pl > 0 AND move_pct > {ot}) as outlier_5p,
                COUNT(*) FILTER (WHERE pl > 0 AND r_multiple > 2) as outlier_2r,
                ROUND(COALESCE(SUM(pl) FILTER (WHERE pl > 0 AND move_pct > {ot}), 0)::numeric) as outlier_5p_pl,
                ROUND(COALESCE(SUM(pl) FILTER (WHERE pl > 0 AND move_pct <= {ot}), 0)::numeric) as regular_win_pl,
                ROUND(COALESCE(SUM(pl) FILTER (WHERE pl <= 0), 0)::numeric) as loss_pl
            FROM all_trades
            GROUP BY period
            ORDER BY 
                CASE period
                    WHEN 'April 2025' THEN 1 WHEN 'May 2025' THEN 2 WHEN 'June 2025' THEN 3
                    WHEN 'July 2025' THEN 4 WHEN 'August 2025' THEN 5 WHEN 'September 2025' THEN 6
                    WHEN 'October 2025' THEN 7 WHEN 'November 2025' THEN 8 WHEN 'December 2025' THEN 9
                    WHEN 'January 2026' THEN 10 WHEN 'February 2026' THEN 11 WHEN 'March 2026' THEN 12
                    ELSE 99 END
        """)
        monthly_trend = [dict(r) for r in cur.fetchall()]

        gp = float(summary["gross_profit"] or 0)

        # Last trade date for staleness tracking
        cur.execute("""
            SELECT GREATEST(
                '2026-01-31'::date,
                '2026-03-31'::date
            ) as last_date
        """)
        last_trade_date = str(cur.fetchone()["last_date"])

        def pct_of(v): return round(float(v or 0) / gp * 100, 1) if gp > 0 else 0

        return jsonify({
            "outlier_threshold": outlier_threshold,
            "outlier_method": "IQR (Q3 + 1.5×IQR)",
            "iqr_stats": {"q1": q1, "q3": q3, "iqr": round(iqr, 2)},
            "summary": {
                "total_trades": summary["total"],
                "winners": summary["winners"],
                "losers": summary["losers"],
                "gross_profit": gp,
                "gross_loss": float(summary["gross_loss"] or 0),
            },
            "win_dist_move": [
                {"bucket": "0-1%", "count": wd["w_0_1"], "pl": float(wd["pl_0_1"] or 0), "pct": pct_of(wd["pl_0_1"])},
                {"bucket": "1-2%", "count": wd["w_1_2"], "pl": float(wd["pl_1_2"] or 0), "pct": pct_of(wd["pl_1_2"])},
                {"bucket": "2-3%", "count": wd["w_2_3"], "pl": float(wd["pl_2_3"] or 0), "pct": pct_of(wd["pl_2_3"])},
                {"bucket": "3-5%", "count": wd["w_3_5"], "pl": float(wd["pl_3_5"] or 0), "pct": pct_of(wd["pl_3_5"])},
                {"bucket": "5-8%", "count": wd["w_5_8"], "pl": float(wd["pl_5_8"] or 0), "pct": pct_of(wd["pl_5_8"])},
                {"bucket": "8-10%", "count": wd["w_8_10"], "pl": float(wd["pl_8_10"] or 0), "pct": pct_of(wd["pl_8_10"])},
                {"bucket": "10%+", "count": wd["w_10p"], "pl": float(wd["pl_10p"] or 0), "pct": pct_of(wd["pl_10p"])},
            ],
            "win_dist_r": [
                {"bucket": "0-0.5R", "count": rd["r_0_05"], "pl": float(rd["rpl_0_05"] or 0), "pct": pct_of(rd["rpl_0_05"])},
                {"bucket": "0.5-1R", "count": rd["r_05_1"], "pl": float(rd["rpl_05_1"] or 0), "pct": pct_of(rd["rpl_05_1"])},
                {"bucket": "1-2R", "count": rd["r_1_2"], "pl": float(rd["rpl_1_2"] or 0), "pct": pct_of(rd["rpl_1_2"])},
                {"bucket": "2-3R", "count": rd["r_2_3"], "pl": float(rd["rpl_2_3"] or 0), "pct": pct_of(rd["rpl_2_3"])},
                {"bucket": "3-5R", "count": rd["r_3_5"], "pl": float(rd["rpl_3_5"] or 0), "pct": pct_of(rd["rpl_3_5"])},
                {"bucket": "5R+", "count": rd["r_5p"], "pl": float(rd["rpl_5p"] or 0), "pct": pct_of(rd["rpl_5p"])},
            ],
            "loss_dist_move": [
                {"bucket": "0-0.5%", "count": ld["l_0_05"], "pl": float(ld["lpl_0_05"] or 0)},
                {"bucket": "0.5-1%", "count": ld["l_05_1"], "pl": float(ld["lpl_05_1"] or 0)},
                {"bucket": "1-2%", "count": ld["l_1_2"], "pl": float(ld["lpl_1_2"] or 0)},
                {"bucket": "2-3%", "count": ld["l_2_3"], "pl": float(ld["lpl_2_3"] or 0)},
                {"bucket": "3-5%", "count": ld["l_3_5"], "pl": float(ld["lpl_3_5"] or 0)},
                {"bucket": "5%+", "count": ld["l_5p"], "pl": float(ld["lpl_5p"] or 0)},
            ],
            "concentration": concentration,
            "top_winners": top_winners,
            "all_losers": all_losers,
            "monthly_trend": monthly_trend,
            "last_trade_date": last_trade_date,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[analytics] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@analytics_bp.route("/api/analytics/config", methods=["GET"])
@limiter.limit("60 per minute")
def get_analytics_config():
    """Get outlier threshold config."""
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT config_value FROM analytics_config WHERE config_key = 'outlier_thresholds'")
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "No config found"}), 404
        import json
        return jsonify(json.loads(row["config_value"]) if isinstance(row["config_value"], str) else row["config_value"])
    except Exception as e:
        print(f"[analytics] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@analytics_bp.route("/api/analytics/config", methods=["PUT"])
@limiter.limit("30 per minute")
def update_analytics_config():
    """Update the selected outlier method."""
    conn = _get_db()
    try:
        data = __import__("flask").request.get_json()
        method = data.get("method")
        if not method:
            return jsonify({"error": "method required"}), 400
        cur = conn.cursor()
        # Update the current_method field inside the JSONB
        cur.execute("""
            UPDATE analytics_config 
            SET config_value = jsonb_set(
                jsonb_set(config_value, '{current_method}', %s::jsonb),
                '{current_threshold_pct}',
                (config_value->'methods'->%s->>'threshold')::jsonb
            ),
            updated_at = NOW()
            WHERE config_key = 'outlier_thresholds'
            RETURNING config_value
        """, (f'"{method}"', method))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Config not found"}), 404
        import json
        return jsonify(json.loads(row["config_value"]) if isinstance(row["config_value"], str) else row["config_value"])
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[analytics] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@analytics_bp.route("/api/analytics/advanced-v2", methods=["GET"])
@limiter.limit("60 per minute")
def advanced_v2():
    """Equity Curve + Drawdown + Streak Analysis + Holding Period vs Return.
    User-scoped. Supports ?fy=2023-24|2024-25|2025-26|2026-27|all."""
    from flask import request as req
    fy = req.args.get("fy", "2026-27")
    conn = _get_db()
    try:
        import json as _json
        cur = conn.cursor()

        from services.user_analytics_service import resolve_fy, get_user_base_capital, LEGACY_FY_TABLES

        if fy == "all":
            resolved = resolve_fy(cur, g.user_id, "all")
            if not resolved.get("allowed"):
                if resolved.get("needs_setup"):
                    return jsonify({"error": "setup_required", "message": "Set your base capital first"}), 400
                return jsonify({"error": "FY not available"}), 403
            tbl = resolved["table"]
            base = resolved["base"]

            # Build monthly equity curve from legacy monthly tables the user has access to
            monthly_parts = []
            offset = 0
            legacy_monthly_map = {
                "2021-22": "legacy_monthly_fy2122",
                "2022-23": "legacy_monthly_fy2223",
                "2023-24": "legacy_monthly_fy2324",
                "2024-25": "legacy_monthly_fy2425",
                "2025-26": "legacy_monthly_summary",
            }
            for lfy, mtbl in legacy_monthly_map.items():
                if get_user_base_capital(cur, g.user_id, lfy) is not None:
                    monthly_parts.append(
                        f"SELECT month_label, month_order + {offset}, after_charges, win_rate, approx_trades FROM {mtbl}"
                    )
                    offset += 100

            # Add uploaded FY monthly summaries
            from services.user_analytics_service import UPLOADED_TRADES_TABLE
            cur.execute(
                "SELECT DISTINCT fy FROM user_uploaded_trades WHERE user_id = %s ORDER BY fy",
                (g.user_id,)
            )
            uploaded_fys = [r["fy"] for r in cur.fetchall()]
            for ufy in uploaded_fys:
                if get_user_base_capital(cur, g.user_id, ufy) is not None:
                    monthly_parts.append(f"""
                        SELECT month_label,
                            month + {offset} as month_order,
                            ROUND((SUM(realized_pl) / {base} * 100)::numeric, 2) as after_charges,
                            ROUND((SUM(CASE WHEN is_winner THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100), 1) as win_rate,
                            COUNT(*) as approx_trades
                        FROM {UPLOADED_TRADES_TABLE}
                        WHERE user_id = '{g.user_id}' AND fy = '{ufy}'
                        GROUP BY month_label, month
                    """)
                    offset += 100

            # Add journal monthly if user has 2026-27 config
            if get_user_base_capital(cur, g.user_id, "2026-27") is not None:
                monthly_parts.append(f"""
                    SELECT month_label,
                        EXTRACT(MONTH FROM MIN(trade_date))::int + {offset} as month_order,
                        ROUND((SUM(realized_pl) / {base} * 100)::numeric, 2) as after_charges,
                        ROUND((SUM(CASE WHEN is_winner THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100), 1) as win_rate,
                        COUNT(*) as approx_trades
                    FROM journal_trades_computed
                    WHERE user_id = '{g.user_id}' AND position_status = 'Closed'
                    GROUP BY month_label, month
                """)

            if monthly_parts:
                cur.execute(" UNION ALL ".join(monthly_parts) + " ORDER BY 2")
            else:
                cur.execute("SELECT NULL as month_label, NULL as month_order, NULL as after_charges, NULL as win_rate, NULL as approx_trades WHERE false")

        elif fy == "2026-27" or fy not in ("2021-22","2022-23","2023-24","2024-25","2025-26"):
            # Journal-based or uploaded FY — user-scoped
            resolved = resolve_fy(cur, g.user_id, fy)
            if not resolved.get("allowed"):
                if resolved.get("needs_setup"):
                    return jsonify({"error": "setup_required", "message": "Set your base capital first"}), 400
                return jsonify({"error": "FY not available"}), 403
            tbl = resolved["table"]
            base = resolved["base"]
            if resolved.get("user_filter"):
                where = f"user_id = '{g.user_id}'"
                if resolved.get("fy_filter"):
                    where += f" AND fy = '{resolved['fy_filter']}'"
                # Match outliers endpoint: only Closed positions count toward
                # realized FY analytics for the journal source.
                if resolved.get("source") == "journal":
                    where += " AND position_status = 'Closed'"
                tbl = f"(SELECT * FROM {tbl} WHERE {where}) _uf"

            # Compute monthly — source-aware
            if resolved.get("source") == "uploaded":
                cur.execute(f"""
                    SELECT month_label,
                        month as month_order,
                        ROUND((SUM(realized_pl) / {base} * 100)::numeric, 2) as after_charges,
                        ROUND((SUM(CASE WHEN is_winner THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100), 1) as win_rate,
                        COUNT(*) as approx_trades
                    FROM user_uploaded_trades
                    WHERE user_id = '{g.user_id}' AND fy = '{fy}'
                    GROUP BY month_label, month
                    ORDER BY month
                """)
            else:
                cur.execute(f"""
                    SELECT month_label,
                        EXTRACT(MONTH FROM MIN(trade_date))::int as month_order,
                        ROUND((SUM(realized_pl) / {base} * 100)::numeric, 2) as after_charges,
                        ROUND((SUM(CASE WHEN is_winner THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100), 1) as win_rate,
                        COUNT(*) as approx_trades
                    FROM journal_trades_computed
                    WHERE user_id = '{g.user_id}' AND position_status = 'Closed'
                    GROUP BY month_label, month
                    ORDER BY MIN(trade_date)
                """)
        else:
            # Legacy single FY — allowed only if user has config for it
            resolved = resolve_fy(cur, g.user_id, fy)
            if not resolved.get("allowed"):
                return jsonify({"error": "FY not available"}), 403
            tbl = resolved["table"]
            base = resolved["base"]
            legacy_monthly_map = {
                "2021-22": "legacy_monthly_fy2122",
                "2022-23": "legacy_monthly_fy2223",
                "2023-24": "legacy_monthly_fy2324",
                "2024-25": "legacy_monthly_fy2425",
                "2025-26": "legacy_monthly_summary",
            }
            mtbl = legacy_monthly_map.get(fy, "legacy_monthly_summary")
            cur.execute(f"SELECT month_label, month_order, after_charges, win_rate, approx_trades FROM {mtbl} ORDER BY month_order")
        legacy = [dict(r) for r in cur.fetchall()]

        equity_points = []
        portfolio = float(base)
        for m in legacy:
            pct = float(m["after_charges"] or 0)
            month_pl = (pct / 100) * portfolio
            portfolio += month_pl
            cumulative_pct = round((portfolio / base - 1) * 100, 2)
            equity_points.append({
                "label": m["month_label"], "period": "legacy",
                "pct_change": round(pct, 2),
                "cumulative_pct": cumulative_pct,
                "equity": round(portfolio),
                "trades": m["approx_trades"] or 0,
                "win_rate": round(float(m["win_rate"] or 0), 1),
            })

        # Drawdown series
        peak_equity = base
        max_dd_pct = 0
        max_dd_to = ""
        current_dd_pct = 0
        months_in_dd = 0
        longest_dd_months = 0
        current_dd_streak = 0
        drawdown_points = []

        for ep in equity_points:
            eq = ep["equity"]
            if eq > peak_equity:
                peak_equity = eq
                current_dd_streak = 0
            dd = round((eq - peak_equity) / peak_equity * 100, 2) if peak_equity else 0
            drawdown_points.append({"label": ep["label"], "dd_pct": dd})
            if dd < 0:
                months_in_dd += 1
                current_dd_streak += 1
                longest_dd_months = max(longest_dd_months, current_dd_streak)
            if dd < max_dd_pct:
                max_dd_pct = dd
                max_dd_to = ep["label"]

        current_dd_pct = drawdown_points[-1]["dd_pct"] if drawdown_points else 0

        drawdown_stats = {
            "max_dd_pct": round(max_dd_pct, 2),
            "max_dd_amount": round(base * abs(max_dd_pct) / 100),
            "max_dd_month": max_dd_to,
            "current_dd_pct": round(current_dd_pct, 2),
            "months_in_drawdown": months_in_dd,
            "longest_dd_months": longest_dd_months,
            "peak_equity": peak_equity,
            "current_equity": equity_points[-1]["equity"] if equity_points else base,
        }

        # === 2. STREAK ANALYSIS ===
        # Pull trade_date for journal/uploaded sources (legacy tables only have month).
        has_trade_date = resolved.get("source") in ("journal", "uploaded")
        date_col = ", trade_date" if has_trade_date else ""
        cur.execute(f"""
            SELECT symbol as stock_name, id as trade_id, realized_pl as pl,
                   ROUND(realized_pl_pct::numeric, 2) as move_pct,
                   ROUND((realized_pl_pct / 3.0)::numeric, 2) as rr{date_col}
            FROM {tbl}
            ORDER BY id
        """)
        trades_chrono = [dict(r) for r in cur.fetchall()]

        streaks = []
        cs_type = None
        cs_len = 0
        cs_pl = 0
        cs_names = []
        max_win = {"len": 0, "pl": 0, "names": []}
        max_loss = {"len": 0, "pl": 0, "names": []}

        trade_sequence = []
        for t in trades_chrono:
            pl_val = float(t["pl"] or 0)
            is_win = pl_val > 0
            td = t.get("trade_date")
            pf_impact = round(pl_val / base * 100, 3) if base else 0
            trade_sequence.append({
                "name": t["stock_name"],
                "date": td.strftime("%Y-%m-%d") if td else "",
                "pl": round(pl_val), "move_pct": float(t["move_pct"] or 0),
                "pf_impact_pct": pf_impact,
                "rr": float(t["rr"] or 0), "is_win": is_win,
            })
            st = "W" if is_win else "L"
            if st == cs_type:
                cs_len += 1
                cs_pl += float(t["pl"] or 0)
                cs_names.append(t["stock_name"])
            else:
                if cs_type:
                    streaks.append({"type": cs_type, "len": cs_len, "pl": round(cs_pl), "names": cs_names})
                cs_type = st
                cs_len = 1
                cs_pl = float(t["pl"] or 0)
                cs_names = [t["stock_name"]]

            if st == "W" and cs_len > max_win["len"]:
                max_win = {"len": cs_len, "pl": round(cs_pl), "names": list(cs_names)}
            if st == "L" and cs_len > max_loss["len"]:
                max_loss = {"len": cs_len, "pl": round(cs_pl), "names": list(cs_names)}

        if cs_type:
            streaks.append({"type": cs_type, "len": cs_len, "pl": round(cs_pl), "names": cs_names})

        # After-streak patterns
        after_2l = []
        after_3w = []
        seq = trade_sequence
        for i in range(2, len(seq)):
            if not seq[i-1]["is_win"] and not seq[i-2]["is_win"]:
                after_2l.append(seq[i]["is_win"])
            if i >= 3 and seq[i-1]["is_win"] and seq[i-2]["is_win"] and seq[i-3]["is_win"]:
                after_3w.append(seq[i]["is_win"])

        streak_data = {
            "sequence": trade_sequence, "streaks": streaks,
            "current": {"type": cs_type or "N/A", "len": cs_len},
            "best_streak": max_win, "worst_streak": max_loss,
            "after_2_losses": {"sample_size": len(after_2l), "next_win_pct": round(sum(after_2l) / len(after_2l) * 100, 1) if after_2l else None},
            "after_3_wins": {"sample_size": len(after_3w), "next_win_pct": round(sum(after_3w) / len(after_3w) * 100, 1) if after_3w else None},
        }

        # === 3. HOLDING PERIOD VS RETURN ===
        # For journal source, use real entry/exit dates from journal_trades + positions.
        # exit_date fallback chain: positions.exit_date -> e3_date -> e2_date -> e1_date.
        # For legacy/uploaded sources, no per-trade exit dates -> hold_days = None.
        hold_data = []
        if resolved.get("source") == "journal":
            cur.execute(f"""
                SELECT j.symbol as stock_name,
                       j.trade_date as entry_date,
                       COALESCE(p.exit_date::date, j.e3_date, j.e2_date, j.e1_date) as exit_date,
                       jc.realized_pl as pl,
                       ROUND(jc.realized_pl_pct::numeric, 2) as move_pct,
                       ROUND((jc.realized_pl_pct / 3.0)::numeric, 2) as rr,
                       jc.buy_value as position_size
                FROM journal_trades_computed jc
                JOIN journal_trades j ON j.id = jc.id
                LEFT JOIN positions p ON p.id = j.position_id
                WHERE jc.user_id = '{g.user_id}' AND jc.position_status = 'Closed'
                ORDER BY jc.id
            """)
            for r in cur.fetchall():
                entry = r["entry_date"]
                exit_d = r["exit_date"]
                hold_days = None
                if entry and exit_d:
                    hold_days = max(0, (exit_d - entry).days)
                hold_data.append({
                    "name": r["stock_name"],
                    "date": entry.strftime("%Y-%m-%d") if entry else "",
                    "hold_days": hold_days,
                    "move_pct": round(float(r["move_pct"] or 0), 2),
                    "rr": round(float(r["rr"] or 0), 2),
                    "pl": round(float(r["pl"] or 0)),
                    "size": round(float(r["position_size"] or 0)),
                })
        else:
            cur.execute(f"""
                SELECT symbol as stock_name, realized_pl as pl,
                       ROUND(realized_pl_pct::numeric, 2) as move_pct,
                       ROUND((realized_pl_pct / 3.0)::numeric, 2) as rr,
                       buy_value as position_size
                FROM {tbl} ORDER BY id
            """)
            for r in cur.fetchall():
                hold_data.append({
                    "name": r["stock_name"], "date": "",
                    "hold_days": None, "move_pct": round(float(r["move_pct"] or 0), 2),
                    "rr": round(float(r["rr"] or 0), 2), "pl": round(float(r["pl"] or 0)),
                    "size": round(float(r["position_size"] or 0)),
                })

        buckets = {"0-2": [], "3-5": [], "6-10": [], "11+": []}
        for h in hold_data:
            d = h["hold_days"]
            if d is None:
                continue
            elif d <= 2:
                buckets["0-2"].append(h)
            elif d <= 5:
                buckets["3-5"].append(h)
            elif d <= 10:
                buckets["6-10"].append(h)
            else:
                buckets["11+"].append(h)

        bucket_stats = []
        for label in ["0-2", "3-5", "6-10", "11+"]:
            trades = buckets[label]
            if not trades:
                continue
            total_pl = sum(t["pl"] for t in trades)
            avg_move = sum(t["move_pct"] for t in trades) / len(trades)
            avg_rr = sum(t["rr"] for t in trades) / len(trades)
            wins = sum(1 for t in trades if t["pl"] > 0)
            bucket_stats.append({
                "bucket": label + " days", "count": len(trades),
                "total_pl": round(total_pl), "avg_move_pct": round(avg_move, 2),
                "avg_rr": round(avg_rr, 2),
                "win_rate": round(wins / len(trades) * 100, 1),
            })

        best_bucket = max(bucket_stats, key=lambda b: b["avg_rr"]) if bucket_stats else None
        winners = [h for h in hold_data if h["pl"] > 0 and h["hold_days"] is not None]
        losers = [h for h in hold_data if h["pl"] <= 0 and h["hold_days"] is not None]
        avg_winner_hold = round(sum(w["hold_days"] for w in winners) / len(winners), 1) if winners else 0
        avg_loser_hold = round(sum(l["hold_days"] for l in losers) / len(losers), 1) if losers else 0

        holding_data = {
            "trades": hold_data, "buckets": bucket_stats,
            "optimal_bucket": best_bucket["bucket"] if best_bucket else "N/A",
            "avg_winner_hold": avg_winner_hold, "avg_loser_hold": avg_loser_hold,
            "total_with_hold_data": len([h for h in hold_data if h["hold_days"] is not None]),
        }

        return jsonify({
            "equity_curve": equity_points, "drawdown": drawdown_points,
            "drawdown_stats": drawdown_stats, "streak": streak_data, "holding": holding_data,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[analytics] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)
