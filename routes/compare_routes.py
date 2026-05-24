"""
Compare Routes — Fetch stocks for comparison, send email reports
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from database.database import get_db, close_db
from services.email_service import send_comparison_email, build_mailto_body

compare_bp = Blueprint("compare", __name__)


@compare_bp.route("/api/compare/stocks", methods=["GET"])
@limiter.limit("60 per minute")
def get_comparison_stocks():
    """
    Get all submissions for comparison selection.
    Returns lightweight data: id, stock_name, final_score, rating, sector, timestamp
    Supports ?dedupe=true to return only the latest entry per stock name.
    Supports ?today=true to return only today's entries.
    Supports ?last=N to return only the last N entries.
    """
    conn = get_db()
    try:
        cursor = conn.cursor()

        dedupe = request.args.get("dedupe", "false").lower() == "true"
        today_only = request.args.get("today", "false").lower() == "true"
        last_n = request.args.get("last", None)

        if dedupe:
            # Get only the latest entry per stock name
            cursor.execute('''
                SELECT DISTINCT ON (LOWER(stock_name))
                    id, stock_name, final_score, rating, sector, market_cap,
                    liquidity, adr, linearity, market_price, freefloat,
                    sector_strength, institutional_participation, relative_strength,
                    symmetry, market_trend, quarterly_results,
                    market_cap_score, linearity_score, liquidity_score,
                    adr_score, sector_strength_score, symmetry_score,
                    institutional_participation_score, relative_strength_score,
                    ferocity_score, magnitude_score, move_ema_score, market_trend_score,
                    chart_image_path, timestamp
                FROM submissions
                WHERE user_id = %s
                ORDER BY LOWER(stock_name), timestamp DESC
            ''', (g.user_id,))
        elif today_only:
            cursor.execute('''
                SELECT id, stock_name, final_score, rating, sector, market_cap,
                    liquidity, adr, linearity, market_price, freefloat,
                    sector_strength, institutional_participation, relative_strength,
                    symmetry, market_trend, quarterly_results,
                    market_cap_score, linearity_score, liquidity_score,
                    adr_score, sector_strength_score, symmetry_score,
                    institutional_participation_score, relative_strength_score,
                    ferocity_score, magnitude_score, move_ema_score, market_trend_score,
                    chart_image_path, timestamp
                FROM submissions
                WHERE user_id = %s AND DATE(timestamp) = CURRENT_DATE
                ORDER BY timestamp DESC
            ''', (g.user_id,))
        else:
            limit_clause = ""
            params = [g.user_id]
            if last_n:
                try:
                    limit_clause = "LIMIT %s"
                    params.append(int(last_n))
                except ValueError:
                    pass

            query = f'''
                SELECT id, stock_name, final_score, rating, sector, market_cap,
                    liquidity, adr, linearity, market_price, freefloat,
                    sector_strength, institutional_participation, relative_strength,
                    symmetry, market_trend, quarterly_results,
                    market_cap_score, linearity_score, liquidity_score,
                    adr_score, sector_strength_score, symmetry_score,
                    institutional_participation_score, relative_strength_score,
                    ferocity_score, magnitude_score, move_ema_score, market_trend_score,
                    chart_image_path, timestamp
                FROM submissions
                WHERE user_id = %s
                ORDER BY timestamp DESC
                {limit_clause}
            '''
            cursor.execute(query, params)

        rows = [dict(row) for row in cursor.fetchall()]

        # Convert timestamps to string for JSON
        for row in rows:
            if row.get("timestamp"):
                row["timestamp"] = str(row["timestamp"])

        return jsonify({"stocks": rows}), 200

    except Exception as e:
        print(f"❌ Compare stocks error: {e}")
        import traceback
        traceback.print_exc()
        print(f"[compare] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@compare_bp.route("/api/compare/email", methods=["POST"])
@limiter.limit("30 per minute")
def email_comparison():
    """
    Send comparison report via email.

    Request JSON:
    {
        "to_email": "user@example.com",
        "stock_ids": [1, 3, 7, ...]  // submission IDs to include
    }
    """
    data = request.json
    to_email = data.get("to_email", "").strip()
    stock_ids = data.get("stock_ids", [])

    if not to_email:
        return jsonify({"error": "Email address is required"}), 400
    if not stock_ids:
        return jsonify({"error": "No stocks selected"}), 400

    conn = get_db()
    try:
        cursor = conn.cursor()

        placeholders = ",".join(["%s"] * len(stock_ids))
        cursor.execute(f'''
            SELECT * FROM submissions
            WHERE id IN ({placeholders}) AND user_id = %s
            ORDER BY final_score DESC
        ''', stock_ids + [g.user_id])

        stocks = [dict(row) for row in cursor.fetchall()]

        if not stocks:
            return jsonify({"error": "No stocks found for given IDs"}), 404

        # Send the email
        result = send_comparison_email(to_email, stocks)

        if result["success"]:
            return jsonify(result), 200
        else:
            return jsonify(result), 400

    except Exception as e:
        print(f"❌ Email comparison error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@compare_bp.route("/api/compare/mailto-body", methods=["POST"])
@limiter.limit("30 per minute")
def get_mailto_body():
    """
    Get plain-text report body for mailto: fallback.

    Request JSON:
    {
        "stock_ids": [1, 3, 7, ...]
    }
    """
    data = request.json
    stock_ids = data.get("stock_ids", [])

    if not stock_ids:
        return jsonify({"error": "No stocks selected"}), 400

    conn = get_db()
    try:
        cursor = conn.cursor()

        placeholders = ",".join(["%s"] * len(stock_ids))
        cursor.execute(f'''
            SELECT * FROM submissions
            WHERE id IN ({placeholders}) AND user_id = %s
            ORDER BY final_score DESC
        ''', stock_ids + [g.user_id])

        stocks = [dict(row) for row in cursor.fetchall()]

        if not stocks:
            return jsonify({"error": "No stocks found"}), 404

        body = build_mailto_body(stocks)
        return jsonify({"body": body}), 200

    except Exception as e:
        print(f"[compare] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)
