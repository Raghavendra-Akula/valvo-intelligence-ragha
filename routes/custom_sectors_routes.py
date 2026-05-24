"""
Custom Sectors — HTTP routes (foundation only, no frontend wired yet).

Exposed endpoints (all under /api/custom-sectors):

    GET  /api/custom-sectors                 — list taxonomy (optional ?parent=)
    GET  /api/custom-sectors/<slug>/stocks   — constituents of a sub-sector
    GET  /api/custom-sectors/performance     — per-sector returns over ?days=30
    POST /api/custom-sectors/seed            — (admin) seed taxonomy from config
    POST /api/custom-sectors/classify        — (admin) keyword-classify universe
                                                body: { limit?: int, overwrite?: bool }

Registered in Backend/app.py. The POST endpoints are gated by the
existing auth middleware; admin-only enforcement can be layered later
alongside the rest of the admin dashboard.
"""
from flask import Blueprint, jsonify, request, g

from extensions import limiter
from services import custom_sectors_service as svc

custom_sectors_bp = Blueprint("custom_sectors", __name__)


def _is_admin() -> bool:
    """Best-effort admin check — mirrors the pattern used elsewhere.
    Returns True if the authed user is flagged as admin on g, else False."""
    return bool(getattr(g, "is_admin", False)) or getattr(g, "user_email", "").endswith(
        "@valvointelligence.com"
    )


@custom_sectors_bp.route("/api/custom-sectors", methods=["GET"])
@limiter.limit("120 per minute")
def list_custom_sectors():
    parent = request.args.get("parent")
    try:
        rows = svc.list_sectors(parent=parent)
        return jsonify({"sectors": rows, "count": len(rows)})
    except Exception as e:
        print(f"[custom_sectors/list] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@custom_sectors_bp.route("/api/custom-sectors/<slug>/stocks", methods=["GET"])
@limiter.limit("60 per minute")
def sector_stocks(slug):
    try:
        rows = svc.get_constituents(slug)
        return jsonify({"slug": slug, "stocks": rows, "count": len(rows)})
    except Exception as e:
        print(f"[custom_sectors/stocks] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@custom_sectors_bp.route("/api/custom-sectors/performance", methods=["GET"])
@limiter.limit("60 per minute")
def sector_performance():
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        return jsonify({"error": "invalid days"}), 400
    try:
        rows = svc.summary_performance(days=days)
        return jsonify({"days": days, "sectors": rows, "count": len(rows)})
    except Exception as e:
        print(f"[custom_sectors/performance] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@custom_sectors_bp.route("/api/custom-sectors/seed", methods=["POST"])
@limiter.limit("10 per minute")
def seed_taxonomy_endpoint():
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    try:
        summary = svc.seed_taxonomy()
        return jsonify({"ok": True, **summary})
    except Exception as e:
        print(f"[custom_sectors/seed] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@custom_sectors_bp.route("/api/custom-sectors/classify", methods=["POST"])
@limiter.limit("5 per minute")
def classify_universe_endpoint():
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    body = request.get_json(silent=True) or {}
    limit = body.get("limit")
    overwrite = bool(body.get("overwrite", False))
    try:
        summary = svc.classify_universe(limit=limit, overwrite=overwrite)
        return jsonify({"ok": True, **summary})
    except Exception as e:
        print(f"[custom_sectors/classify] error: {e}")
        return jsonify({"error": "Internal error"}), 500
