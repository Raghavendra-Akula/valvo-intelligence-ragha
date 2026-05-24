"""
Settings API Routes — Synced via Supabase (not localStorage)
- GET/PUT /api/settings — user settings (single row)
- GET/POST /api/settings/sectors — leading sectors (with history)
- GET/POST /api/settings/regime — market regime (with history)
"""
from flask import Blueprint, request, jsonify, g
from extensions import limiter

settings_bp = Blueprint("settings", __name__)

def _get_db():
    from database.database import get_db
    return get_db()

def _close_db(conn):
    from database.database import close_db
    close_db(conn)


def _current_fy_label() -> str:
    """Indian FY label (April-March) for today, e.g. '2026-27'."""
    from datetime import date
    today = date.today()
    year = today.year if today.month >= 4 else today.year - 1
    end = str((year + 1) % 100).zfill(2)
    return f"{year}-{end}"


@settings_bp.route("/api/settings", methods=["GET"])
@limiter.limit("60 per minute")
def get_settings():
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT display_name, palette, show_52w FROM user_settings WHERE user_id = %s", (g.user_id,))
        row = cur.fetchone()

        # Auto-create row if missing
        if not row:
            try:
                cur.execute("INSERT INTO user_settings (user_id) VALUES (%s)", (g.user_id,))
                conn.commit()
                cur.execute("SELECT display_name, palette, show_52w FROM user_settings WHERE user_id = %s", (g.user_id,))
                row = cur.fetchone()
            except Exception:
                conn.rollback()

        # base_capital now lives in user_fy_config per FY. user_settings.base_capital
        # is deprecated and no longer read. For the Settings UI we return the
        # current FY's value (which the UI displays as "Base Capital (FY 26-27)").
        from services.user_analytics_service import get_user_base_capital
        base_capital = None
        try:
            base_capital = get_user_base_capital(cur, g.user_id, _current_fy_label())
        except Exception:
            base_capital = None

        # leading_sectors and market_regime_history are GLOBAL reference tables
        # (no user_id column). Single-user app today; when multi-user arrives
        # this is the spot to re-add per-user scoping + a proper DB migration.
        cur.execute("SELECT sectors, regime, updated_at FROM leading_sectors ORDER BY updated_at DESC LIMIT 1")
        sec = cur.fetchone()

        cur.execute("SELECT regime, updated_at FROM market_regime_history ORDER BY updated_at DESC LIMIT 1")
        reg = cur.fetchone()

        return jsonify({
            "display_name":        row.get("display_name") if row else None,
            "base_capital":        base_capital,
            "base_capital_fy":     _current_fy_label(),
            "palette":             row.get("palette") if row else None,
            "show_52w":            row.get("show_52w") if row else None,

            "sectors":             sec["sectors"] if sec else [],
            "sectors_updated_at":  sec["updated_at"].isoformat() if sec and sec["updated_at"] else None,
            "regime":              reg["regime"] if reg else "bull",
            "regime_updated_at":   reg["updated_at"].isoformat() if reg and reg["updated_at"] else None,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[settings] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@settings_bp.route("/api/settings", methods=["PUT"])
@limiter.limit("30 per minute")
def update_settings():
    conn = _get_db()
    try:
        data = request.get_json() or {}
        cur = conn.cursor()

        # Ensure row exists for this user. Swallow errors here so a transient
        # failure on user_settings (RLS, constraint, etc.) doesn't abort the
        # whole PUT — base_capital lives on user_fy_config and doesn't depend
        # on user_settings having a row.
        try:
            cur.execute("SELECT user_id FROM user_settings WHERE user_id = %s", (g.user_id,))
            if not cur.fetchone():
                cur.execute("INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (g.user_id,))
                conn.commit()
        except Exception as seed_err:
            print(f"[settings] user_settings seed failed (non-fatal): {seed_err}")
            try:
                conn.rollback()
            except Exception:
                pass

        # base_capital: single source of truth is user_fy_config (per-FY).
        # The Settings UI sends a flat `base_capital` payload — we route it to
        # user_fy_config for the CURRENT FY unless base_capital_fy is provided.
        saved_base_capital = None
        saved_fy = None
        if "base_capital" in data:
            try:
                amount = float(data["base_capital"])
                if amount >= 0:
                    fy = data.get("base_capital_fy") or _current_fy_label()
                    from services.user_analytics_service import set_user_base_capital, get_user_base_capital
                    set_user_base_capital(cur, g.user_id, fy, amount)
                    conn.commit()
                    # Read-back so the response reflects actual DB state. If the
                    # write silently no-op'd (RLS, trigger, whatever) the caller
                    # will see the stale value instead of optimistic success.
                    saved_base_capital = get_user_base_capital(cur, g.user_id, fy)
                    saved_fy = fy
                    print(f"[settings] base_capital save user={g.user_id} fy={fy} requested={amount} stored={saved_base_capital}")
            except (TypeError, ValueError) as parse_err:
                print(f"[settings] base_capital parse error: {parse_err}")

        # Remaining display-only settings still live in user_settings.
        fields = []
        vals = []
        for k in ["display_name", "palette", "show_52w"]:
            if k in data:
                fields.append(f"{k} = %s")
                vals.append(data[k])
        if fields:
            fields.append("updated_at = NOW()")
            vals.append(g.user_id)
            cur.execute(f"UPDATE user_settings SET {', '.join(fields)} WHERE user_id = %s", vals)
            conn.commit()
        return jsonify({
            "ok": True,
            "base_capital": float(saved_base_capital) if saved_base_capital is not None else None,
            "base_capital_fy": saved_fy,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[settings] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@settings_bp.route("/api/settings/sectors", methods=["GET"])
@limiter.limit("60 per minute")
def get_sectors():
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, sectors, regime, updated_at, note FROM leading_sectors ORDER BY updated_at DESC LIMIT 20")
        rows = cur.fetchall()
        return jsonify({"history": [
            {"id": r["id"], "sectors": r["sectors"], "regime": r["regime"],
             "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
             "note": r.get("note")}
            for r in rows
        ], "current": rows[0] if rows else None})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[settings] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@settings_bp.route("/api/settings/sectors", methods=["POST"])
@limiter.limit("30 per minute")
def update_sectors():
    conn = _get_db()
    try:
        data = request.get_json() or {}
        sectors = data.get("sectors", [])
        regime = data.get("regime")
        note = data.get("note", "")

        if not sectors or not isinstance(sectors, list):
            return jsonify({"error": "sectors must be a non-empty array"}), 400

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO leading_sectors (sectors, regime, note) VALUES (%s, %s, %s) RETURNING id, updated_at",
            (sectors, regime, note)
        )
        row = cur.fetchone()
        conn.commit()
        return jsonify({"ok": True, "id": row["id"], "updated_at": row["updated_at"].isoformat()})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[settings] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@settings_bp.route("/api/settings/regime", methods=["GET"])
@limiter.limit("60 per minute")
def get_regime():
    conn = _get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, regime, updated_at, note FROM market_regime_history ORDER BY updated_at DESC LIMIT 20")
        rows = cur.fetchall()
        return jsonify({"history": [
            {"id": r["id"], "regime": r["regime"],
             "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
             "note": r.get("note")}
            for r in rows
        ], "current": rows[0]["regime"] if rows else "bull"})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[settings] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)


@settings_bp.route("/api/settings/regime", methods=["POST"])
@limiter.limit("30 per minute")
def update_regime():
    conn = _get_db()
    try:
        data = request.get_json() or {}
        regime = data.get("regime")
        note = data.get("note", "")

        if not regime:
            return jsonify({"error": "regime is required"}), 400

        cur = conn.cursor()
        cur.execute(
            "INSERT INTO market_regime_history (regime, note) VALUES (%s, %s) RETURNING id, updated_at",
            (regime, note)
        )
        row = cur.fetchone()
        conn.commit()
        return jsonify({"ok": True, "id": row["id"], "updated_at": row["updated_at"].isoformat()})
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[settings] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        _close_db(conn)
