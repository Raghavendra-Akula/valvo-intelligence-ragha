"""
Theme Thesis route — serves the DeepSeek-generated "why is this stock in this
theme" paragraph rendered under the Company Summary card on the Explore tab.

    GET /api/v2/classification/<symbol_or_sid>/theme-thesis

Behaviour:
  • Resolves the stock + its primary V2 theme via the existing spine reader.
  • Hands off to `theme_explanation_service.get_or_generate(...)` which is
    cache-first, lazy-generate, persist-once.
  • Returns a stable JSON shape on every code path so the frontend can render
    or hide the section without branching:
        { explanation, theme_slug, theme_name, model_used,
          generated_at, prompt_version, cached, reason? }
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from extensions import limiter
from services.classification_v2 import spine as v2_spine
from services.theme_explanation_service import get_or_generate


theme_explanation_bp = Blueprint("theme_explanation", __name__)


@theme_explanation_bp.route(
    "/api/v2/classification/<symbol_or_sid>/theme-thesis",
    methods=["GET"],
)
@limiter.limit("30 per minute")
def get_theme_thesis(symbol_or_sid: str):
    try:
        spine = v2_spine.get_spine(symbol_or_sid)
    except Exception as exc:
        print(f"[theme_thesis] spine lookup error for {symbol_or_sid}: {exc}")
        return jsonify({"explanation": None, "reason": "spine_error"}), 500

    if isinstance(spine, dict) and "error" in spine:
        # Distinguish missing stock from internal failure — same convention
        # as classification_v2_routes.get_spine_route.
        code = 404 if "not found" in (spine.get("error") or "").lower() else 400
        return jsonify({"explanation": None, "reason": spine["error"]}), code

    primary = (spine or {}).get("primary_theme") or {}
    sid = (spine or {}).get("security_id")
    slug = primary.get("slug")

    if not sid or not slug:
        # Stock is not classified into any V2 theme yet — frontend hides the
        # section. 200 (not 404) so the hook treats it as "valid empty".
        return jsonify({
            "symbol": (spine or {}).get("symbol"),
            "theme_slug": None,
            "theme_name": None,
            "explanation": None,
            "reason": "no_primary_theme",
        }), 200

    result = get_or_generate(sid, slug)
    if result is None:
        # Generation refused (no API key / sanity-check failed / DeepSeek 5xx).
        # 503 so the frontend can quietly retry on the next page load while
        # treating "no explanation today" as the not-shown state.
        return jsonify({
            "symbol": spine.get("symbol"),
            "theme_slug": slug,
            "theme_name": primary.get("name"),
            "explanation": None,
            "reason": "generation_failed",
        }), 503

    return jsonify({
        "symbol": spine.get("symbol"),
        "theme_slug": slug,
        "theme_name": primary.get("name"),
        "theme_accent": primary.get("accent_color"),
        **result,
    })
