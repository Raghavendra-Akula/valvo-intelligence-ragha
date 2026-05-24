from __future__ import annotations

from flask import Blueprint, jsonify, request
from extensions import limiter
from services.valvo_ai_v2 import ValvoAIEngine


valvo_ai_v2_bp = Blueprint("valvo_ai_v2", __name__)
engine = ValvoAIEngine()


@valvo_ai_v2_bp.route("/api/valvo-ai/query", methods=["POST"])
@limiter.limit("20 per minute")
def valvo_ai_query():
    payload = request.json or {}
    result = engine.query(payload)
    status_code = 400 if result.get("error") else 200
    return jsonify(result), status_code


@valvo_ai_v2_bp.route("/api/valvo-ai/health", methods=["GET"])
@limiter.limit("60 per minute")
def valvo_ai_health():
    return jsonify(engine.health())


@valvo_ai_v2_bp.route("/api/valvo-ai/history", methods=["DELETE"])
@limiter.limit("15 per minute")
def valvo_ai_history_clear():
    page_context = request.args.get("page_context")
    return jsonify(engine.clear_history(page_context=page_context))
