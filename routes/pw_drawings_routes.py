"""Past-Winners chart drawings persistence — scope-aware per (user, stock, scope)."""
import json
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from database.database import get_db, close_db

pw_drawings_bp = Blueprint("pw_drawings", __name__)

# Same defensive cap as drawings_routes.MAX_DRAWINGS_PER_STOCK — see
# that module for context. Past-winners scoped drawings come from the
# same UI primitives (hlines, trendlines, text notes), so the same
# pathological accumulation would manifest here too if the frontend
# regressed.
MAX_DRAWINGS_PER_STOCK = 500


@pw_drawings_bp.route("/api/pw-drawings/<security_id>/<path:scope>", methods=["GET"])
@limiter.limit("60 per minute")
def get_pw_drawings(security_id, scope):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT drawings, updated_at FROM pw_chart_drawings "
            "WHERE user_id = %s AND security_id = %s AND scope = %s",
            (g.user_id, security_id, scope),
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


@pw_drawings_bp.route("/api/pw-drawings/<security_id>/<path:scope>", methods=["PUT"])
@limiter.limit("30 per minute")
def save_pw_drawings(security_id, scope):
    conn = get_db()
    try:
        data = request.get_json(force=True)
        drawings = data.get("drawings", [])
        if isinstance(drawings, list) and len(drawings) > MAX_DRAWINGS_PER_STOCK:
            return jsonify({
                "error": "too_many_drawings",
                "message": f"Refusing to save {len(drawings)} drawings; cap is {MAX_DRAWINGS_PER_STOCK} per stock",
                "limit": MAX_DRAWINGS_PER_STOCK,
                "received": len(drawings),
            }), 400
        cur = conn.cursor()

        if not drawings:
            cur.execute(
                "DELETE FROM pw_chart_drawings "
                "WHERE user_id = %s AND security_id = %s AND scope = %s",
                (g.user_id, security_id, scope),
            )
        else:
            cur.execute("""
                INSERT INTO pw_chart_drawings (user_id, security_id, scope, drawings, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (user_id, security_id, scope)
                DO UPDATE SET drawings = EXCLUDED.drawings, updated_at = NOW()
            """, (g.user_id, security_id, scope, json.dumps(drawings)))

        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


@pw_drawings_bp.route("/api/pw-drawings/list", methods=["GET"])
@limiter.limit("30 per minute")
def list_pw_drawings():
    """Return all PW scopes that have drawings for this user — used for the Playbook index later."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT security_id, scope, jsonb_array_length(drawings) AS n, updated_at "
            "FROM pw_chart_drawings WHERE user_id = %s ORDER BY updated_at DESC",
            (g.user_id,),
        )
        rows = cur.fetchall() or []
        out = [{
            "security_id": r["security_id"],
            "scope": r["scope"],
            "count": int(r["n"] or 0),
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        } for r in rows]
        return jsonify({"entries": out})
    except Exception as e:
        return jsonify({"error": str(e), "entries": []}), 500
    finally:
        close_db(conn)
