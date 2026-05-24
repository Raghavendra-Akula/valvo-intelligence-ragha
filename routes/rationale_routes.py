"""
/api/rationale/* — admin-gated behavioral coaching endpoints.

Frontend flow:
  1. Page mount: GET /pending → if any, auto-open Valvo AI floating chat
     with question_text pre-loaded. Mark "seen" client-side so we don't
     re-pop on the same prompt.
  2. User answers in chat: POST /answer with prompt_id + answer_text.
     Backend extracts tags via Gemini Flash, stores everything.
  3. User clicks dismiss: POST /dismiss/<id>.
  4. Admin graph page: GET /graph → bubble chart data + drilldown.

The trigger logic itself lives in services/rationale_service.py and
runs inline from valvo_ai_v2/actions.py after a position closes.
"""
from __future__ import annotations

from flask import Blueprint, request, jsonify, g

from extensions import limiter
from database.database import get_db, close_db
from services.admin_service import is_admin
from services import rationale_service

rationale_bp = Blueprint("rationale", __name__)


def _require_admin():
    """All rationale endpoints are admin-only for now (the user explicitly
    asked for an admin-gated rollout). Returns Flask response on failure,
    None on success."""
    if not getattr(g, "user_id", None):
        return jsonify({"error": "Login required"}), 401
    if not is_admin(g.user_id):
        return jsonify({"error": "Admin only"}), 403
    return None


@rationale_bp.route("/api/rationale/pending", methods=["GET"])
@limiter.limit("60 per minute")
def list_pending():
    """Frontend polls this on page mount + every ~60s. Returns at most
    5 pending prompts; the popup shows them one at a time."""
    err = _require_admin()
    if err:
        return err
    conn = get_db()
    try:
        cur = conn.cursor()
        rows = rationale_service.list_pending(cur, g.user_id, limit=5)
        return jsonify({
            "prompts": [
                {
                    "id": int(r["id"]),
                    "trigger_kind": r["trigger_kind"],
                    "trigger_details": r.get("trigger_details") or {},
                    "position_ids": r.get("position_ids") or [],
                    "question_text": r["question_text"],
                    "pnl_impact": float(r["pnl_impact"]) if r.get("pnl_impact") is not None else None,
                    "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
                }
                for r in rows
            ]
        })
    except Exception as exc:
        print(f"[rationale] list_pending error: {exc}")
        return jsonify({"error": "Could not load prompts"}), 500
    finally:
        close_db(conn)


@rationale_bp.route("/api/rationale/answer", methods=["POST"])
@limiter.limit("20 per minute")
def submit_answer():
    """Body: { prompt_id: int, answer_text: str }
    Extracts tags, persists, returns the answered prompt with tags."""
    err = _require_admin()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    prompt_id = body.get("prompt_id")
    answer_text = (body.get("answer_text") or "").strip()
    if not prompt_id or not answer_text:
        return jsonify({"error": "prompt_id and answer_text required"}), 400
    if len(answer_text) > 5000:
        return jsonify({"error": "answer_text too long (max 5000 chars)"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        try:
            row = rationale_service.record_answer(cur, int(prompt_id), g.user_id, answer_text)
        except ValueError as ve:
            conn.rollback()
            return jsonify({"error": str(ve)}), 404
        conn.commit()

        # Reshape for the client
        tags = row.get("extracted_tags") or []
        if isinstance(tags, str):
            import json
            try: tags = json.loads(tags)
            except Exception: tags = []
        return jsonify({
            "id": int(row["id"]),
            "status": row["status"],
            "extracted_tags": tags,
            "answer_text": row["answer_text"],
            "answered_at": row["answered_at"].isoformat() if row.get("answered_at") else None,
        })
    except Exception as exc:
        try: conn.rollback()
        except Exception: pass
        print(f"[rationale] submit_answer error: {exc}")
        return jsonify({"error": "Could not save answer"}), 500
    finally:
        close_db(conn)


@rationale_bp.route("/api/rationale/dismiss/<int:prompt_id>", methods=["POST"])
@limiter.limit("30 per minute")
def dismiss(prompt_id: int):
    """User clicked 'not now'. Mark dismissed so the popup stops appearing."""
    err = _require_admin()
    if err:
        return err
    conn = get_db()
    try:
        cur = conn.cursor()
        ok = rationale_service.dismiss_prompt(cur, prompt_id, g.user_id)
        conn.commit()
        return jsonify({"dismissed": bool(ok)})
    except Exception as exc:
        try: conn.rollback()
        except Exception: pass
        print(f"[rationale] dismiss error: {exc}")
        return jsonify({"error": "Could not dismiss"}), 500
    finally:
        close_db(conn)


@rationale_bp.route("/api/rationale/graph", methods=["GET"])
@limiter.limit("30 per minute")
def graph():
    """Admin graph page data — bubble chart nodes + raw prompts for drilldown.
    Query: ?days=180 (default 180)"""
    err = _require_admin()
    if err:
        return err
    try:
        days = int(request.args.get("days", "180"))
        days = max(7, min(days, 730))  # clamp 1 week .. 2 years
    except (TypeError, ValueError):
        days = 180

    conn = get_db()
    try:
        cur = conn.cursor()
        data = rationale_service.get_graph_data(cur, g.user_id, days=days)
        return jsonify(data)
    except Exception as exc:
        print(f"[rationale] graph error: {exc}")
        return jsonify({"error": "Could not load graph"}), 500
    finally:
        close_db(conn)
