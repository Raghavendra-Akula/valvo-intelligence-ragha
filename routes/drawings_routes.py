"""Chart drawings persistence — GET + PUT upsert per user+stock."""
import json
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from database.database import get_db, close_db

drawings_bp = Blueprint("drawings", __name__)

# Defensive cap: refuse to persist more than this many drawings on a single
# stock. A frontend bug once accumulated 8,589 phantom hlines on one stock,
# producing a ~900KB localStorage blob that locked Chrome's main thread on
# every chart switch. The frontend bug is fixed (ValvoChart.jsx clears stale
# refs at the top of _renderCandles), but this cap is the backend safety
# net so any future regression — or an unrelated client — can't bloat the
# row to a hang-inducing size.
MAX_DRAWINGS_PER_STOCK = 500


@drawings_bp.route("/api/drawings/<security_id>", methods=["GET"])
@limiter.limit("60 per minute")
def get_drawings(security_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT drawings, updated_at FROM chart_drawings WHERE user_id = %s AND security_id = %s",
            (g.user_id, security_id),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"drawings": [], "updated_at": None})
        return jsonify({
            "drawings": row["drawings"] if isinstance(row["drawings"], list) else json.loads(row["drawings"]),
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        })
    except Exception as e:
        return jsonify({"error": str(e), "drawings": []}), 500
    finally:
        close_db(conn)


@drawings_bp.route("/api/drawings/<security_id>", methods=["PUT"])
@limiter.limit("30 per minute")
def save_drawings(security_id):
    conn = get_db()
    try:
        data = request.get_json(force=True)
        drawings = data.get("drawings", [])
        if isinstance(drawings, list) and len(drawings) > MAX_DRAWINGS_PER_STOCK:
            return jsonify({
                "error": f"too_many_drawings",
                "message": f"Refusing to save {len(drawings)} drawings; cap is {MAX_DRAWINGS_PER_STOCK} per stock",
                "limit": MAX_DRAWINGS_PER_STOCK,
                "received": len(drawings),
            }), 400
        cur = conn.cursor()

        if not drawings:
            cur.execute(
                "DELETE FROM chart_drawings WHERE user_id = %s AND security_id = %s",
                (g.user_id, security_id),
            )
        else:
            cur.execute("""
                INSERT INTO chart_drawings (user_id, security_id, drawings, updated_at)
                VALUES (%s, %s, %s::jsonb, NOW())
                ON CONFLICT (user_id, security_id)
                DO UPDATE SET drawings = EXCLUDED.drawings, updated_at = NOW()
            """, (g.user_id, security_id, json.dumps(drawings)))

        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)
