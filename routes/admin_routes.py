"""
Admin Routes - Manage settings
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from database.database import get_db, close_db
from datetime import datetime

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/api/admin/settings", methods=["GET"])
@limiter.limit("60 per minute")
def get_settings():
    """Get all parameter settings"""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM settings ORDER BY parameter_name')
        settings = [dict(row) for row in cursor.fetchall()]
        return jsonify(settings), 200
    except Exception as e:
        print(f"[admin] error: {e}")
        return jsonify({'error': 'Internal error'}), 500
    finally:
        close_db(conn)


@admin_bp.route("/api/admin/settings", methods=["PUT"])
@limiter.limit("30 per minute")
def update_settings():
    """Update parameter weightages"""
    conn = get_db()
    try:
        settings = request.json
        cursor = conn.cursor()

        for setting in settings:
            cursor.execute('''
                UPDATE settings
                SET weightage = %s, enabled = %s, updated_at = %s
                WHERE parameter_name = %s
            ''', (
                setting['weightage'],
                setting['enabled'],
                datetime.now().isoformat(),
                setting['parameter_name']
            ))

        conn.commit()
        return jsonify({'success': True, 'message': 'Settings updated'}), 200
    except Exception as e:
        print(f"[admin] error: {e}")
        return jsonify({'error': 'Internal error'}), 500
    finally:
        close_db(conn)


REGIME_MAP = {1: "bull", 2: "sideways", 3: "grind_down", 4: "sharp_downtrend"}
REGIME_REVERSE = {v: k for k, v in REGIME_MAP.items()}


@admin_bp.route("/api/admin/settings/regime", methods=["GET"])
@limiter.limit("60 per minute")
def get_global_regime():
    """Get current global market regime + active position counts per regime"""
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Get global regime
        cursor.execute("SELECT weightage FROM settings WHERE parameter_name = 'global_market_regime'")
        row = cursor.fetchone()
        regime_code = int(row["weightage"]) if row else 1
        regime = REGIME_MAP.get(regime_code, "bull")

        # Count active positions per regime. Previously aggregated across
        # every user's positions — now scoped to g.user_id so each caller
        # sees only their own per-regime counts. The file is called
        # admin_routes but this endpoint is user-facing (/api/settings/regime).
        uid = getattr(g, "user_id", None)
        counts = {}
        if uid:
            cursor.execute("""
                SELECT market_regime, COUNT(*) as cnt
                FROM positions
                WHERE status = 'active' AND market_regime IS NOT NULL
                  AND user_id = %s
                GROUP BY market_regime
            """, (uid,))
            counts = {r["market_regime"]: int(r["cnt"]) for r in cursor.fetchall()}
        conn.close()

        return jsonify({
            "regime": regime,
            "position_counts": {
                "bull": counts.get("bull", 0),
                "sideways": counts.get("sideways", 0),
                "grind_down": counts.get("grind_down", 0),
                "sharp_downtrend": counts.get("sharp_downtrend", 0),
            },
            "total_active": sum(counts.values()),
        }), 200
    except Exception as e:
        return jsonify({"regime": "bull", "position_counts": {}, "total_active": 0}), 200


@admin_bp.route("/api/admin/settings/regime", methods=["PUT"])
@limiter.limit("30 per minute")
def update_global_regime():
    """Update the global market regime"""
    data = request.json
    regime = data.get("regime", "bull")
    if regime not in REGIME_REVERSE:
        return jsonify({"error": f"Invalid regime. Must be one of: {list(REGIME_REVERSE.keys())}"}), 400
    code = REGIME_REVERSE[regime]
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO settings (parameter_name, weightage, enabled)
               VALUES ('global_market_regime', %s, TRUE)
               ON CONFLICT (parameter_name)
               DO UPDATE SET weightage = %s, updated_at = %s""",
            (code, code, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True, "regime": regime}), 200
    except Exception as e:
        print(f"[admin] error: {e}")
        return jsonify({"error": "Internal error"}), 500


@admin_bp.route("/api/admin/settings/reset", methods=["POST"])
@limiter.limit("30 per minute")
def reset_settings():
    """Reset all weightages to default"""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE settings
            SET weightage = CASE parameter_name
                WHEN 'sector_strength' THEN 26
                WHEN 'magnitude' THEN 14
                WHEN 'move_ema' THEN 12
                WHEN 'relative_strength' THEN 10
                WHEN 'institutional_participation' THEN 10
                WHEN 'symmetry' THEN 8
                WHEN 'ferocity' THEN 8
                WHEN 'adr' THEN 7
                WHEN 'market_trend' THEN 5
                ELSE 0
            END,
            enabled = 1, updated_at = %s
        ''', (datetime.now().isoformat(),))
        conn.commit()
        return jsonify({'success': True, 'message': 'Settings reset to default'}), 200
    except Exception as e:
        print(f"[admin] error: {e}")
        return jsonify({'error': 'Internal error'}), 500
    finally:
        close_db(conn)


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN API KEYS — long-lived bearer tokens for headless workers
#
#  Example (after JWT-authenticated POST):
#    {
#      "id": 5, "label": "research-worker-laptop", "key_prefix": "vk_AbCdEf12",
#      "plaintext": "vk_AbCdEf12_full_secret…",
#      ...
#    }
#  Plaintext is shown ONCE — store it in your worker's env (VALVO_ADMIN_TOKEN).
# ═════════════════════════════════════════════════════════════════════════════

from services.admin_service import is_admin
from services import admin_api_keys


def _require_admin_token_op():
    """Helper: ensure caller is an admin (and not authenticating WITH a key,
    which would be a chicken-and-egg). API-key holders cannot mint new keys."""
    if not getattr(g, "user_id", None):
        return jsonify({"error": "Login required"}), 401
    if not is_admin(g.user_id):
        return jsonify({"error": "Admin only"}), 403
    if getattr(g, "auth_via", None) == "admin_api_key":
        return jsonify({"error": "API key cannot be used to mint new keys; use a Supabase session"}), 403
    return None


@admin_bp.route("/api/admin/api-keys", methods=["POST"])
@limiter.limit("10 per minute")
def create_admin_api_key():
    """Mint a new admin API key. Plaintext returned ONCE."""
    err = _require_admin_token_op()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    label = (body.get("label") or "").strip()[:100]
    if not label:
        return jsonify({"error": "label required"}), 400
    expires_at = body.get("expires_at")  # ISO timestamp or null
    try:
        out = admin_api_keys.create_key(user_id=g.user_id, label=label, expires_at=expires_at)
        return jsonify(out), 201
    except Exception as e:
        print(f"[admin/api-keys] create error: {e}")
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/api-keys", methods=["GET"])
@limiter.limit("60 per minute")
def list_admin_api_keys():
    """List the caller's keys. Plaintext NEVER returned."""
    err = _require_admin_token_op()
    if err:
        return err
    try:
        return jsonify({"keys": admin_api_keys.list_keys(user_id=g.user_id)}), 200
    except Exception as e:
        print(f"[admin/api-keys] list error: {e}")
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/admin/api-keys/<int:key_id>", methods=["DELETE"])
@limiter.limit("60 per minute")
def revoke_admin_api_key(key_id: int):
    """Revoke a key. Idempotent."""
    err = _require_admin_token_op()
    if err:
        return err
    try:
        ok = admin_api_keys.revoke_key(user_id=g.user_id, key_id=key_id)
        if not ok:
            return jsonify({"error": "Key not found or already revoked"}), 404
        return jsonify({"revoked": True, "id": key_id}), 200
    except Exception as e:
        print(f"[admin/api-keys] revoke error: {e}")
        return jsonify({"error": str(e)}), 500
