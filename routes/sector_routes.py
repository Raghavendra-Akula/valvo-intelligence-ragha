"""
sector_routes.py — Sector / index summary endpoints.

Currently exposes:
  POST /api/sectors/refresh-summary   — manually rebuild index_daily_summary
  GET  /api/sectors/leading           — read-only: top N sector indices

The heavy lifting lives in services/index_summary.py. This file is just the
HTTP surface. Settings UI / admin tools call POST to force a rebuild after
adding new candles or fixing a data issue; Valvo AI v5's get_leading_sectors
tool calls the service directly (no HTTP).
"""
from flask import Blueprint, jsonify, request
from extensions import limiter

from services.index_summary import (
    refresh_index_summary,
    get_leading_sectors,
    get_leading_sectors_with_stats,
    trigger_background_refresh_if_stale,
)

sector_bp = Blueprint("sector", __name__)


@sector_bp.route("/api/sectors/refresh-summary", methods=["POST"])
@limiter.limit("6 per minute")
def refresh_summary():
    """Manual refresh trigger. Synchronous — returns when the rebuild finishes.
    Idempotent: re-running only UPDATEs existing rows. Background-thread
    refresh from the read path is separate; this is for admin / debug."""
    result = refresh_index_summary()
    status = 200 if result.get("ok") else 500
    return jsonify(result), status


@sector_bp.route("/api/sectors/leading", methods=["GET"])
@limiter.limit("60 per minute")
def leading_sectors():
    try:
        limit = max(1, min(int(request.args.get("limit", 5)), 20))
    except (TypeError, ValueError):
        limit = 5
    try:
        spark_days = max(10, min(int(request.args.get("spark_days", 30)), 90))
    except (TypeError, ValueError):
        spark_days = 30
    with_stats = request.args.get("stats", "0") in ("1", "true", "True", "yes")

    # Refresh the summary if it's older than 12 hours so the order isn't
    # frozen on yesterday's data.
    try:
        trigger_background_refresh_if_stale()
    except Exception:
        pass

    if with_stats:
        rows = get_leading_sectors_with_stats(limit=limit, spark_days=spark_days)
    else:
        rows = get_leading_sectors(limit=limit)
    return jsonify({"sectors": rows, "count": len(rows)})
