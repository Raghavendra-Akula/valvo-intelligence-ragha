"""
History Routes - Get and delete past submissions
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from services import submission_service

history_bp = Blueprint("history", __name__)


@history_bp.route("/api/submissions", methods=["GET"])
@limiter.limit("60 per minute")
def get_submissions():
    """Get paginated submissions"""
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 10))
        result = submission_service.get_all_submissions(page, per_page, user_id=g.user_id)
        return jsonify(result), 200
    except Exception as e:
        print(f"[history] error: {e}")
        return jsonify({'error': 'Internal error'}), 500


@history_bp.route("/api/submission/<int:submission_id>", methods=["GET"])
@limiter.limit("60 per minute")
def get_submission(submission_id):
    """Get specific submission"""
    try:
        submission = submission_service.get_submission_by_id(submission_id, user_id=g.user_id)
        if not submission:
            return jsonify({'error': 'Submission not found'}), 404
        return jsonify(submission), 200
    except Exception as e:
        print(f"[history] error: {e}")
        return jsonify({'error': 'Internal error'}), 500


@history_bp.route("/api/submission/<int:submission_id>", methods=["DELETE"])
@limiter.limit("15 per minute")
def delete_submission(submission_id):
    """Delete a submission by ID"""
    try:
        deleted = submission_service.delete_submission(submission_id, user_id=g.user_id)
        if not deleted:
            return jsonify({'error': 'Submission not found'}), 404
        return jsonify({'success': True, 'message': f'Submission {submission_id} deleted'}), 200
    except Exception as e:
        print(f"[history] error: {e}")
        return jsonify({'error': 'Internal error'}), 500


@history_bp.route("/api/check-duplicates", methods=["POST"])
@limiter.limit("30 per minute")
def check_duplicates():
    """Check if stock exists in last 7 days"""
    try:
        data = request.json
        stock_name = data.get('stock_name', '').strip()
        if not stock_name:
            return jsonify({'duplicates': []}), 200
        dupes = submission_service.check_duplicates(stock_name, days=7, user_id=g.user_id)
        return jsonify({'duplicates': dupes}), 200
    except Exception as e:
        print(f"[history] error: {e}")
        return jsonify({'error': 'Internal error'}), 500


@history_bp.route("/api/submissions/bulk-delete", methods=["POST"])
@limiter.limit("30 per minute")
def bulk_delete_submissions():
    """Delete multiple submissions by IDs"""
    try:
        data = request.json
        ids = data.get('ids', [])
        if not ids:
            return jsonify({'error': 'No IDs provided'}), 400
        deleted = submission_service.delete_submissions_bulk(ids, user_id=g.user_id)
        return jsonify({'success': True, 'deleted': deleted}), 200
    except Exception as e:
        print(f"[history] error: {e}")
        return jsonify({'error': 'Internal error'}), 500


@history_bp.route("/api/submission/<int:submission_id>/trade", methods=["PUT"])
@limiter.limit("30 per minute")
def update_trade(submission_id):
    """Update trade tracking fields (traded, entry_price, position_size, exit_price)"""
    try:
        data = request.json
        updated = submission_service.update_trade(
            submission_id,
            traded=data.get('traded', False),
            entry_price=data.get('entry_price'),
            position_size=data.get('position_size'),
            exit_price=data.get('exit_price'),
            user_id=g.user_id,
        )
        if not updated:
            return jsonify({'error': 'Submission not found'}), 404
        return jsonify({'success': True}), 200
    except Exception as e:
        print(f"[history] error: {e}")
        return jsonify({'error': 'Internal error'}), 500