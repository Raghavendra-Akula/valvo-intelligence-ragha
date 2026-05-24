"""
Score Routes - Calculate stock scores
Routes to the correct scoring engine based on setup_type
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from services import scoring_service, submission_service
from services import oob_scoring_service

score_bp = Blueprint("score", __name__)


@score_bp.route("/api/submission/<int:submission_id>/rescore", methods=["PUT"])
@limiter.limit("10 per minute")
def rescore_submission(submission_id):
    """Rescore an existing submission with updated params, update same record"""
    try:
        data = request.json

        # Get existing submission to merge unchanged fields (scoped to user)
        existing = submission_service.get_submission_by_id(submission_id, user_id=g.user_id)
        if not existing:
            return jsonify({'error': 'Submission not found'}), 404

        # Merge: keep original stock data, override with any updated params
        merged = {**existing, **data}
        merged['stock_name'] = existing['stock_name']  # never change stock name

        setup_type = merged.get('setup_type', 'large_base_institutional')

        # Rescore using correct engine
        if setup_type == 'out_of_base':
            result = oob_scoring_service.calculate_score_from_data(merged)
        else:
            result = scoring_service.calculate_score_from_data(merged)

        scores = result['individual_scores']
        final_score = result['final_score']
        rating = result['rating']

        print(f"\n{'='*60}")
        print(f"📊 RESCORE #{submission_id}: {merged.get('stock_name')} [{setup_type}]")
        print(f"   Final score: {final_score} → {rating}")
        print(f"{'='*60}\n")

        # Update same record
        submission_service.update_submission(
            submission_id, merged, scores, final_score, rating, user_id=g.user_id
        )

        return jsonify({
            'id': submission_id,
            'scores': scores,
            'final_score': final_score,
            'rating': rating,
            'raw_composite': result['raw_composite'],
            'gatekeepers': result['gatekeepers'],
            'combined_gatekeeper': result['combined_gatekeeper'],
            'setup_type': setup_type,
            'updated': True,
        }), 200

    except Exception as e:
        print(f"\n❌ ERROR in rescore_submission:")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Rescore failed'}), 500


@score_bp.route("/api/calculate-score", methods=["POST"])
@limiter.limit("30 per minute")
def calculate_score():
    """Calculate stock score and save to database"""
    try:
        data = request.json
        
        # Normalize stock name field
        if 'Stock_name' in data and 'stock_name' not in data:
            data['stock_name'] = data['Stock_name']
        
        setup_type = data.get('setup_type', 'large_base_institutional')
        
        # ── Route to correct scoring engine ──
        if setup_type == 'out_of_base':
            result = oob_scoring_service.calculate_score_from_data(data)
        else:
            result = scoring_service.calculate_score_from_data(data)
        
        scores = result['individual_scores']
        final_score = result['final_score']
        rating = result['rating']
        
        print(f"\n{'='*60}")
        print(f"📊 {data.get('stock_name', 'N/A')} [{setup_type}]")
        print(f"   Raw composite: {result['raw_composite']}")
        print(f"   Gatekeepers: {result['gatekeepers']}")
        print(f"   Combined gatekeeper: {result['combined_gatekeeper']}")
        print(f"   Final score: {final_score} → {rating}")
        print(f"{'='*60}\n")
        
        # Save to database
        submission_id = submission_service.save_submission(
            data, scores, final_score, rating, user_id=g.user_id
        )
        
        response = {
            'id': submission_id,
            'scores': scores,
            'final_score': final_score,
            'rating': rating,
            'raw_composite': result['raw_composite'],
            'gatekeepers': result['gatekeepers'],
            'combined_gatekeeper': result['combined_gatekeeper'],
            'setup_type': setup_type,
        }
        
        # Include ferocity_ratio for OOB
        if 'ferocity_ratio' in result:
            response['ferocity_ratio'] = result['ferocity_ratio']
        
        return jsonify(response), 200
        
    except Exception as e:
        print(f"\n❌ ERROR in calculate_score:")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Score calculation failed'}), 500


@score_bp.route("/api/scores/recent", methods=["POST"])
@limiter.limit("30 per minute")
def get_recent_scores():
    """
    Given a list of stock symbols/names, return the most recent score
    for each (within last 21 days). Used by watchlist to show scores.
    Body: { "stocks": ["TATAMOTORS", "RELIANCE", ...] }
    """
    from database.database import get_db, close_db
    try:
        data = request.json
        stock_list = data.get("stocks", [])
        if not stock_list:
            return jsonify({"scores": {}})
        if len(stock_list) > 500:
            stock_list = stock_list[:500]

        conn = get_db()
        if not conn:
            return jsonify({"scores": {}, "error": "DB unavailable"}), 500
        cur = conn.cursor()

        # Get the most recent score for each stock within 21 days (scoped to user)
        # Match on stock_name OR symbol (nsecode)
        placeholders = ",".join(["%s"] * len(stock_list))
        cur.execute(f"""
            SELECT DISTINCT ON (COALESCE(symbol, stock_name))
                id, stock_name, symbol, final_score, rating, setup_type, timestamp
            FROM submissions
            WHERE user_id = %s
              AND timestamp >= NOW() - INTERVAL '21 days'
              AND (symbol IN ({placeholders}) OR stock_name IN ({placeholders}))
            ORDER BY COALESCE(symbol, stock_name), timestamp DESC
        """, [g.user_id] + stock_list + stock_list)

        rows = cur.fetchall()
        scores = {}
        for r in rows:
            key = r["symbol"] or r["stock_name"]
            scores[key] = {
                "id": r["id"],
                "score": float(r["final_score"]) if r["final_score"] else None,
                "rating": r["rating"],
                "setup_type": r["setup_type"],
                "scored_at": r["timestamp"].isoformat() if r["timestamp"] else None,
            }
            # Also map by stock_name if different from symbol
            if r["stock_name"] and r["stock_name"] != key:
                scores[r["stock_name"]] = scores[key]

        close_db(conn)
        return jsonify({"scores": scores})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"scores": {}, "error": "Failed to fetch recent scores"}), 500

