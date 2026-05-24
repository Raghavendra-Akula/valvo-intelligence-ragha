"""
Sector Thesis route — DeepSeek-generated "why this stock is in this sector"
sentence rendered in the new crisp Explore card.

    GET /api/v2/classification/<symbol_or_sid>/sector-thesis

Mirrors the theme-thesis route shape so the frontend hook can be a clone.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from extensions import limiter
from services.classification_v2 import spine as v2_spine
from services.sector_thesis_service import get_or_generate


sector_thesis_bp = Blueprint("sector_thesis", __name__)


@sector_thesis_bp.route(
    "/api/v2/classification/<symbol_or_sid>/sector-thesis",
    methods=["GET"],
)
@limiter.limit("30 per minute")
def get_sector_thesis(symbol_or_sid: str):
    try:
        spine = v2_spine.get_spine(symbol_or_sid)
    except Exception as exc:
        print(f"[sector_thesis] spine lookup error for {symbol_or_sid}: {exc}")
        return jsonify({"explanation": None, "reason": "spine_error"}), 500

    if isinstance(spine, dict) and "error" in spine:
        code = 404 if "not found" in (spine.get("error") or "").lower() else 400
        return jsonify({"explanation": None, "reason": spine["error"]}), code

    sid = (spine or {}).get("security_id")
    sector_obj = (spine or {}).get("sector") or {}
    sector_name = sector_obj.get("name")

    if not sid or not sector_name:
        # Stock has no sector yet (rare — usually V1 sector is populated).
        # 200 with explanation=None so the hook treats this as "valid empty".
        return jsonify({
            "symbol": (spine or {}).get("symbol"),
            "sector": None,
            "explanation": None,
            "reason": "no_sector",
        }), 200

    result = get_or_generate(sid, sector_name)
    if result is None:
        return jsonify({
            "symbol": spine.get("symbol"),
            "sector": sector_name,
            "explanation": None,
            "reason": "generation_failed",
        }), 503

    return jsonify({
        "symbol": spine.get("symbol"),
        "sector": sector_name,
        **result,
    })
