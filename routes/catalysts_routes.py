"""
Catalysts route — DeepSeek-synthesized list of "what's driving this stock"
bullets rendered in the new Catalysts card on the Explore strip.

    GET /api/v2/explore/<symbol_or_sid>/catalysts

Returns:
    {
      "symbol": "MTAR",
      "catalysts": [{title, detail, kind}, ...],
      "concall_period": "2025-12-31" | null,
      "model_used": "deepseek-chat",
      "generated_at": "...",
      "prompt_version": 1,
      "cached": bool
    }

Synthesis is grounded in INTERNAL data only (concall summary + last 4
quarters + segment mix) — no external web search. So catalysts that
never made the call won't surface here. By design.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from extensions import limiter
from services.classification_v2 import spine as v2_spine
from services.catalyst_service import get_or_generate


catalysts_bp = Blueprint("catalysts", __name__)


@catalysts_bp.route(
    "/api/v2/explore/<symbol_or_sid>/catalysts",
    methods=["GET"],
)
@limiter.limit("30 per minute")
def get_catalysts(symbol_or_sid: str):
    try:
        spine = v2_spine.get_spine(symbol_or_sid)
    except Exception as exc:
        print(f"[catalysts] spine lookup error for {symbol_or_sid}: {exc}")
        return jsonify({"catalysts": None, "reason": "spine_error"}), 500

    if isinstance(spine, dict) and "error" in spine:
        code = 404 if "not found" in (spine.get("error") or "").lower() else 400
        return jsonify({"catalysts": None, "reason": spine["error"]}), code

    sid = (spine or {}).get("security_id")
    if not sid:
        return jsonify({
            "symbol": (spine or {}).get("symbol"),
            "catalysts": None,
            "reason": "no_security_id",
        }), 200

    result = get_or_generate(sid)
    if result is None:
        return jsonify({
            "symbol": spine.get("symbol"),
            "catalysts": None,
            "reason": "generation_failed",
        }), 503

    return jsonify({
        "symbol": spine.get("symbol"),
        **result,
    })
