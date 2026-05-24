"""
Explore Strip extras endpoint — concall snapshot + peer set, fetched
together so the right-hand panel only pays for one extra round-trip
per stock.

    GET /api/v2/classification/<symbol_or_sid>/extras

Returns:
    {
      symbol, security_id, industry,
      concall: {
        period, period_end_date,
        themes: [{slug, name, accent_color}, ...],
        model_used, generated_at,
      } | null,
      peers: [
        {security_id, symbol, company_name,
         market_cap_cr, pe_ratio, roe, current_price},
        ...
      ],
    }
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from extensions import limiter
from services.explore_extras_service import get_extras


explore_extras_bp = Blueprint("explore_extras", __name__)


@explore_extras_bp.route(
    "/api/v2/classification/<symbol_or_sid>/extras",
    methods=["GET"],
)
@limiter.limit("60 per minute")
def explore_extras(symbol_or_sid: str):
    try:
        result = get_extras(symbol_or_sid)
    except Exception as exc:
        print(f"[explore_extras] error for {symbol_or_sid}: {exc}")
        return jsonify({"error": "internal_error"}), 500
    if isinstance(result, dict) and "error" in result:
        code = 404 if "not found" in (result.get("error") or "").lower() else 400
        return jsonify(result), code
    return jsonify(result)
