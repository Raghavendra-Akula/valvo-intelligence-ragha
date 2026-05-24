"""
Themes (Waves) — HTTP routes.

All endpoints under /api/themes:

    GET  /api/themes                     — taxonomy (waves + themes) for UI hydration
    GET  /api/themes/matrix?days=30      — wave × theme grid with counts + median return
    GET  /api/themes/constituents/<slug> — stocks tagged to a theme
    GET  /api/themes/stock/<security_id> — themes for one stock
    POST /api/themes/seed                — (admin) init_schema + seed taxonomy
    POST /api/themes/classify            — (admin) run classifier
                                            body: { dry_run?: bool, limit?: int, symbols?: [] }

Registered in Backend/app.py. Admin-gated writes mirror the custom_sectors_routes pattern.
"""
from flask import Blueprint, jsonify, request, g

from extensions import limiter
from services import themes_service as svc

themes_bp = Blueprint("themes", __name__)


def _is_admin() -> bool:
    return bool(getattr(g, "is_admin", False)) or getattr(g, "user_email", "").endswith(
        "@valvointelligence.com"
    )


@themes_bp.route("/api/themes", methods=["GET"])
@limiter.limit("120 per minute")
def list_themes():
    try:
        data = svc.list_taxonomy()
        return jsonify(data)
    except Exception as e:
        print(f"[themes/list] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@themes_bp.route("/api/themes/matrix", methods=["GET"])
@limiter.limit("60 per minute")
def theme_matrix():
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        return jsonify({"error": "invalid days"}), 400
    try:
        rows = svc.get_matrix(days=days)
        return jsonify({"days": days, "rows": rows, "count": len(rows)})
    except Exception as e:
        print(f"[themes/matrix] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@themes_bp.route("/api/themes/constituents/<slug>", methods=["GET"])
@limiter.limit("60 per minute")
def theme_constituents(slug):
    try:
        rows = svc.get_constituents(slug)
        return jsonify({"slug": slug, "stocks": rows, "count": len(rows)})
    except Exception as e:
        print(f"[themes/constituents] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@themes_bp.route("/api/themes/<slug>/composite", methods=["GET"])
@limiter.limit("60 per minute")
def theme_composite(slug):
    try:
        days = int(request.args.get("days", 252))
    except ValueError:
        return jsonify({"error": "invalid days"}), 400
    try:
        series = svc.get_composite("theme", slug, days=days)
        series = [{"date": str(r["date"]), "value": float(r["value"]), "n": int(r["n"])} for r in series]
        return jsonify({"slug": slug, "scope": "theme", "days": days, "series": series, "count": len(series)})
    except Exception as e:
        print(f"[themes/composite] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@themes_bp.route("/api/waves/<slug>/composite", methods=["GET"])
@limiter.limit("60 per minute")
def wave_composite(slug):
    try:
        days = int(request.args.get("days", 252))
    except ValueError:
        return jsonify({"error": "invalid days"}), 400
    try:
        series = svc.get_composite("wave", slug, days=days)
        series = [{"date": str(r["date"]), "value": float(r["value"]), "n": int(r["n"])} for r in series]
        return jsonify({"slug": slug, "scope": "wave", "days": days, "series": series, "count": len(series)})
    except Exception as e:
        print(f"[waves/composite] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@themes_bp.route("/api/waves/<slug>/constituents", methods=["GET"])
@limiter.limit("60 per minute")
def wave_constituents(slug):
    try:
        rows = svc.get_wave_constituents(slug)
        return jsonify({"slug": slug, "stocks": rows, "count": len(rows)})
    except Exception as e:
        print(f"[waves/constituents] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@themes_bp.route("/api/themes/stock/<security_id>", methods=["GET"])
@limiter.limit("120 per minute")
def stock_themes(security_id):
    try:
        rows = svc.get_stock_themes(security_id)
        return jsonify({"security_id": security_id, "themes": rows, "count": len(rows)})
    except Exception as e:
        print(f"[themes/stock] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@themes_bp.route("/api/themes/seed", methods=["POST"])
@limiter.limit("10 per minute")
def seed_themes():
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    try:
        summary = svc.seed_taxonomy()
        return jsonify({"ok": True, **summary})
    except Exception as e:
        print(f"[themes/seed] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@themes_bp.route("/api/themes/classify", methods=["POST"])
@limiter.limit("5 per minute")
def classify_themes():
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    body = request.get_json(silent=True) or {}
    try:
        summary = svc.classify_all(
            limit=body.get("limit"),
            symbols=body.get("symbols"),
            dry_run=bool(body.get("dry_run", False)),
            skip_manual=bool(body.get("skip_manual", True)),
            skip_web_verified=bool(body.get("skip_web_verified", True)),
        )
        # Don't ship full decisions array in HTTP response for huge runs
        summary.pop("decisions", None)
        return jsonify({"ok": True, **summary})
    except Exception as e:
        print(f"[themes/classify] error: {e}")
        return jsonify({"error": "Internal error"}), 500
