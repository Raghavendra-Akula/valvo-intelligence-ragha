"""
CSV Upload Routes — Upload Zerodha P&L CSVs for historical analytics.
Premium feature — requires active subscription.
"""
import uuid
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from database.database import get_db, close_db

csv_upload_bp = Blueprint("csv_upload", __name__)


def _check_subscription(conn):
    """Check if user has an active paid subscription or trial. Returns None if OK, error response if not."""
    cur = conn.cursor()
    cur.execute("""
        SELECT plan, status, end_date
        FROM user_subscriptions
        WHERE user_id = %s
    """, (g.user_id,))
    row = cur.fetchone()

    if not row:
        return jsonify({
            "error": "subscription_required",
            "message": "Past analytics is a premium feature. Upgrade to unlock.",
        }), 403

    if row["plan"] == "free" or row["status"] not in ("active", "trial"):
        return jsonify({
            "error": "subscription_required",
            "message": "Past analytics is a premium feature. Upgrade to unlock.",
        }), 403

    return None


@csv_upload_bp.route("/api/csv-upload/preview", methods=["POST"])
@limiter.limit("20 per minute")
def preview_csv():
    """Parse CSV(s) and return preview without storing. No paywall — let users see what they'll get."""
    from services.csv_upload_service import parse_zerodha_csv

    if "files" not in request.files and "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    files = request.files.getlist("files") or [request.files.get("file")]
    results = []

    for f in files:
        if not f or not f.filename:
            continue
        raw = f.read()
        if len(raw) > 10 * 1024 * 1024:  # 10MB limit per file
            results.append({"filename": f.filename, "errors": ["File too large (max 10MB)"], "trade_count": 0})
            continue

        parsed = parse_zerodha_csv(raw, f.filename)
        results.append({
            "filename": f.filename,
            "fy": parsed.get("fy"),
            "month": parsed.get("month"),
            "year": parsed.get("year"),
            "trade_count": parsed["trade_count"],
            "total_pl": parsed["total_pl"],
            "warnings": parsed["warnings"],
            "errors": parsed["errors"],
            "needs_ai": parsed.get("needs_ai", False),
            "sample_trades": parsed["trades"][:5],
        })

    # Detect unique FYs across all files
    detected_fys = list(set(r["fy"] for r in results if r.get("fy")))

    # Check which FYs already have base capital configured
    conn = get_db()
    try:
        cur = conn.cursor()
        fy_capitals = {}
        if detected_fys:
            cur.execute(
                "SELECT fy, base_capital FROM user_fy_config WHERE user_id = %s AND fy = ANY(%s)",
                (g.user_id, detected_fys)
            )
            fy_capitals = {r["fy"]: float(r["base_capital"]) for r in cur.fetchall()}
    finally:
        close_db(conn)

    return jsonify({
        "files": results,
        "detected_fys": sorted(detected_fys),
        "fy_capitals": fy_capitals,
        "needs_capital_setup": [fy for fy in detected_fys if fy not in fy_capitals],
    })


@csv_upload_bp.route("/api/csv-upload/upload", methods=["POST"])
@limiter.limit("10 per minute")
def upload_csv():
    """Parse + store CSV trades. Requires subscription."""
    from services.csv_upload_service import (
        parse_zerodha_csv, ai_parse_csv, store_trades,
        create_upload_record, complete_upload_record, fail_upload_record,
    )

    conn = get_db()
    try:
        # Paywall check
        paywall = _check_subscription(conn)
        if paywall:
            return paywall

        if "files" not in request.files and "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        files = request.files.getlist("files") or [request.files.get("file")]

        # FY and base_capital can come from form data or JSON
        forced_fy = request.form.get("fy")
        base_capital = request.form.get("base_capital")
        if base_capital:
            base_capital = float(base_capital)

        batch_id = uuid.uuid4()
        results = []
        total_inserted = 0
        total_errors = []

        cur = conn.cursor()

        for f in files:
            if not f or not f.filename:
                continue

            raw = f.read()
            if len(raw) > 10 * 1024 * 1024:
                total_errors.append(f"{f.filename}: File too large")
                continue

            # Try header-based parsing first
            parsed = parse_zerodha_csv(raw, f.filename)
            ai_tokens = 0

            # Fall back to AI parsing if header detection failed
            if parsed.get("needs_ai") or (not parsed["trades"] and parsed["errors"]):
                ai_result = ai_parse_csv(raw, f.filename)
                if ai_result["trades"]:
                    parsed = ai_result
                    ai_tokens = ai_result.get("ai_tokens_used", 0)

            fy = forced_fy or parsed.get("fy")
            if not fy:
                total_errors.append(f"{f.filename}: Could not detect fiscal year")
                continue

            # Ensure base capital exists for this FY
            if not base_capital:
                cur.execute(
                    "SELECT base_capital FROM user_fy_config WHERE user_id = %s AND fy = %s",
                    (g.user_id, fy)
                )
                row = cur.fetchone()
                if row:
                    base_capital = float(row["base_capital"])
                else:
                    total_errors.append(f"{f.filename}: Base capital not set for FY {fy}. Set it first.")
                    continue

            # Auto-create user_fy_config if base_capital was provided
            cur.execute("""
                INSERT INTO user_fy_config (user_id, fy, base_capital)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, fy) DO NOTHING
            """, (g.user_id, fy, base_capital))

            # Ensure user_profiles row exists
            cur.execute("""
                INSERT INTO user_profiles (user_id) VALUES (%s)
                ON CONFLICT (user_id) DO NOTHING
            """, (g.user_id,))
            conn.commit()

            # Create upload record
            upload_id = create_upload_record(conn, g.user_id, batch_id, fy, f.filename)

            if not parsed["trades"]:
                fail_upload_record(conn, upload_id, parsed["errors"])
                total_errors.extend(parsed["errors"])
                continue

            # Store trades
            try:
                store_result = store_trades(conn, g.user_id, batch_id, parsed["trades"], fy, base_capital)
                complete_upload_record(
                    conn, upload_id,
                    rows_parsed=parsed["trade_count"],
                    rows_inserted=store_result["inserted"],
                    rows_skipped=store_result["skipped"],
                    errors=parsed["warnings"],
                    ai_tokens=ai_tokens,
                )
                total_inserted += store_result["inserted"]

                results.append({
                    "filename": f.filename,
                    "fy": fy,
                    "trades_imported": store_result["inserted"],
                    "trades_updated": store_result["updated"],
                    "total_pl": parsed["total_pl"],
                    "ai_assisted": parsed.get("ai_assisted", False),
                })
            except Exception as e:
                fail_upload_record(conn, upload_id, [str(e)])
                total_errors.append(f"{f.filename}: Storage failed — {str(e)}")
                conn.rollback()

        return jsonify({
            "batch_id": str(batch_id),
            "files_processed": len(results),
            "total_trades_imported": total_inserted,
            "results": results,
            "errors": total_errors,
        })

    except Exception as e:
        print(f"[csv-upload] error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Upload failed", "detail": str(e)}), 500
    finally:
        close_db(conn)


@csv_upload_bp.route("/api/csv-upload/upload-bulk", methods=["POST"])
@limiter.limit("5 per minute")
def upload_bulk():
    """
    Bulk upload: accepts multiple CSVs with per-FY base capitals.
    Body (form data): files=..., fy_capitals=JSON string like {"2022-23": 5000000, ...}
    """
    from services.csv_upload_service import (
        parse_zerodha_csv, ai_parse_csv, store_trades,
        create_upload_record, complete_upload_record, fail_upload_record,
    )

    conn = get_db()
    try:
        paywall = _check_subscription(conn)
        if paywall:
            return paywall

        if "files" not in request.files:
            return jsonify({"error": "No files provided"}), 400

        files = request.files.getlist("files")
        fy_capitals_raw = request.form.get("fy_capitals", "{}")

        import json
        try:
            fy_capitals = json.loads(fy_capitals_raw)
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid fy_capitals JSON"}), 400

        batch_id = uuid.uuid4()
        results = []
        total_inserted = 0
        errors = []

        cur = conn.cursor()

        # Ensure user_profiles row
        cur.execute("INSERT INTO user_profiles (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (g.user_id,))

        # Create user_fy_config entries for all provided capitals
        for fy, capital in fy_capitals.items():
            cur.execute("""
                INSERT INTO user_fy_config (user_id, fy, base_capital)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, fy) DO UPDATE SET base_capital = EXCLUDED.base_capital
            """, (g.user_id, fy, float(capital)))
        conn.commit()

        for f in files:
            if not f or not f.filename:
                continue

            raw = f.read()
            if len(raw) > 10 * 1024 * 1024:
                errors.append(f"{f.filename}: File too large")
                continue

            parsed = parse_zerodha_csv(raw, f.filename)
            ai_tokens = 0

            if parsed.get("needs_ai") or (not parsed["trades"] and parsed["errors"]):
                ai_result = ai_parse_csv(raw, f.filename)
                if ai_result["trades"]:
                    parsed = ai_result
                    ai_tokens = ai_result.get("ai_tokens_used", 0)

            fy = parsed.get("fy")
            if not fy:
                errors.append(f"{f.filename}: Could not detect FY")
                continue

            base_capital = fy_capitals.get(fy)
            if not base_capital:
                # Check DB
                cur.execute(
                    "SELECT base_capital FROM user_fy_config WHERE user_id = %s AND fy = %s",
                    (g.user_id, fy)
                )
                row = cur.fetchone()
                base_capital = float(row["base_capital"]) if row else None

            if not base_capital:
                errors.append(f"{f.filename}: No base capital for FY {fy}")
                continue

            upload_id = create_upload_record(conn, g.user_id, batch_id, fy, f.filename)

            if not parsed["trades"]:
                fail_upload_record(conn, upload_id, parsed["errors"])
                errors.extend(parsed["errors"])
                continue

            try:
                store_result = store_trades(conn, g.user_id, batch_id, parsed["trades"], fy, base_capital)
                complete_upload_record(
                    conn, upload_id,
                    rows_parsed=parsed["trade_count"],
                    rows_inserted=store_result["inserted"],
                    rows_skipped=store_result["skipped"],
                    errors=parsed["warnings"],
                    ai_tokens=ai_tokens,
                )
                total_inserted += store_result["inserted"]
                results.append({
                    "filename": f.filename,
                    "fy": fy,
                    "trades_imported": store_result["inserted"],
                    "total_pl": parsed["total_pl"],
                    "ai_assisted": parsed.get("ai_assisted", False),
                })
            except Exception as e:
                fail_upload_record(conn, upload_id, [str(e)])
                errors.append(f"{f.filename}: {str(e)}")
                conn.rollback()

        return jsonify({
            "batch_id": str(batch_id),
            "files_processed": len(results),
            "total_trades_imported": total_inserted,
            "results": results,
            "errors": errors,
        })
    except Exception as e:
        print(f"[csv-upload] bulk error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


@csv_upload_bp.route("/api/csv-upload/status", methods=["GET"])
@limiter.limit("60 per minute")
def upload_status():
    """Get upload history for current user, grouped by FY."""
    from services.csv_upload_service import get_upload_status

    conn = get_db()
    try:
        status = get_upload_status(conn, g.user_id)

        # Also get trade counts per FY
        cur = conn.cursor()
        cur.execute("""
            SELECT fy, COUNT(*) as trade_count, SUM(realized_pl) as total_pl
            FROM user_uploaded_trades
            WHERE user_id = %s
            GROUP BY fy
            ORDER BY fy
        """, (g.user_id,))
        fy_stats = {r["fy"]: {"trade_count": r["trade_count"], "total_pl": float(r["total_pl"] or 0)}
                    for r in cur.fetchall()}

        return jsonify({
            "uploads": status,
            "fy_stats": fy_stats,
            "has_uploads": len(fy_stats) > 0,
        })
    except Exception as e:
        print(f"[csv-upload] status error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


@csv_upload_bp.route("/api/csv-upload/fy/<fy>", methods=["DELETE"])
@limiter.limit("10 per minute")
def delete_fy(fy):
    """Delete all uploaded trades for a specific FY."""
    from services.csv_upload_service import delete_fy_uploads

    conn = get_db()
    try:
        trades_deleted = delete_fy_uploads(conn, g.user_id, fy)
        return jsonify({
            "message": f"Deleted {trades_deleted} trades for FY {fy}",
            "trades_deleted": trades_deleted,
        })
    except Exception as e:
        print(f"[csv-upload] delete error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


@csv_upload_bp.route("/api/csv-upload/replace", methods=["POST"])
@limiter.limit("10 per minute")
def replace_month():
    """Replace a specific month's data for a FY."""
    from services.csv_upload_service import (
        parse_zerodha_csv, ai_parse_csv, store_trades,
        create_upload_record, complete_upload_record, fail_upload_record,
    )

    conn = get_db()
    try:
        paywall = _check_subscription(conn)
        if paywall:
            return paywall

        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        fy = request.form.get("fy")
        month = request.form.get("month")
        if not fy:
            return jsonify({"error": "fy is required"}), 400

        f = request.files["file"]
        raw = f.read()

        parsed = parse_zerodha_csv(raw, f.filename)
        ai_tokens = 0

        if parsed.get("needs_ai") or (not parsed["trades"] and parsed["errors"]):
            ai_result = ai_parse_csv(raw, f.filename)
            if ai_result["trades"]:
                parsed = ai_result
                ai_tokens = ai_result.get("ai_tokens_used", 0)

        if not parsed["trades"]:
            return jsonify({"error": "No trades parsed", "details": parsed["errors"]}), 400

        cur = conn.cursor()

        # Get base capital
        cur.execute(
            "SELECT base_capital FROM user_fy_config WHERE user_id = %s AND fy = %s",
            (g.user_id, fy)
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": f"No base capital configured for FY {fy}"}), 400
        base_capital = float(row["base_capital"])

        # Delete existing data for this month
        target_month = int(month) if month else parsed.get("month")
        if target_month:
            cur.execute(
                "DELETE FROM user_uploaded_trades WHERE user_id = %s AND fy = %s AND month = %s",
                (g.user_id, fy, target_month)
            )
            conn.commit()

        # Store new data
        batch_id = uuid.uuid4()
        upload_id = create_upload_record(conn, g.user_id, batch_id, fy, f.filename)

        store_result = store_trades(conn, g.user_id, batch_id, parsed["trades"], fy, base_capital)
        complete_upload_record(
            conn, upload_id,
            rows_parsed=parsed["trade_count"],
            rows_inserted=store_result["inserted"],
            rows_skipped=store_result["skipped"],
            errors=parsed["warnings"],
            ai_tokens=ai_tokens,
        )

        return jsonify({
            "message": f"Replaced month {target_month} data for FY {fy}",
            "trades_imported": store_result["inserted"],
            "total_pl": parsed["total_pl"],
        })
    except Exception as e:
        print(f"[csv-upload] replace error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)
