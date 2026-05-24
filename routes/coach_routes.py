"""
/api/coach/* — Daily Coach endpoints.

Three endpoints:

  GET  /api/coach/daily?date=YYYY-MM-DD
       Latest persisted report for the user (today by default). If no
       report exists yet for `date`, returns 404 — the user (or cron)
       must POST /run-daily first.

  POST /api/coach/run-daily
       Body: { date?: 'YYYY-MM-DD' }  (defaults to today)
       Computes + upserts the report. Idempotent: re-running on the same
       date overwrites. The cron job will hit this once per weekday at
       16:30 IST after market close.

  GET  /api/coach/history?days=30
       Lightweight trend: list of {date, leak_score, high_severity_count}
       for the user's recent reports. Powers the trend chart.

  POST /api/coach/acknowledge/<id>
       Marks a report as 'read' (sets acknowledged_at). Used by the
       dashboard banner — once acknowledged, the banner stops nagging.

Auth: admin-gated for now (single-user beta), mirroring rationale_routes.
"""
from __future__ import annotations

from datetime import date as date_cls, datetime
from typing import Optional

from flask import Blueprint, request, jsonify, g

from extensions import limiter
from database.database import get_db, close_db
from services.admin_service import is_admin
from services import coach_qa_service, daily_coach_service

coach_bp = Blueprint("coach", __name__)


def _require_admin():
    if not getattr(g, "user_id", None):
        return jsonify({"error": "Login required"}), 401
    if not is_admin(g.user_id):
        return jsonify({"error": "Admin only"}), 403
    return None


def _parse_date(s: Optional[str]) -> date_cls:
    if not s:
        return date_cls.today()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return date_cls.today()


# ────────────────────────────────────────────────────────────────────
#  GET /api/coach/daily
# ────────────────────────────────────────────────────────────────────

@coach_bp.route("/api/coach/daily", methods=["GET"])
@limiter.limit("60 per minute")
def get_daily():
    err = _require_admin()
    if err:
        return err
    on = _parse_date(request.args.get("date"))
    conn = get_db()
    try:
        cur = conn.cursor()
        report = daily_coach_service.fetch_report(cur, g.user_id, on)
        if not report:
            return jsonify({
                "report": None,
                "message": "No report yet — POST /api/coach/run-daily to generate one.",
            }), 404
        return jsonify({"report": report})
    except Exception as exc:
        print(f"[coach] get_daily error: {exc}")
        return jsonify({"error": "Could not load report"}), 500
    finally:
        close_db(conn)


# ────────────────────────────────────────────────────────────────────
#  POST /api/coach/run-daily
# ────────────────────────────────────────────────────────────────────

@coach_bp.route("/api/coach/run-daily", methods=["POST"])
@limiter.limit("10 per minute")
def run_daily():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    on = _parse_date(body.get("date"))

    conn = get_db()
    try:
        cur = conn.cursor()
        report = daily_coach_service.build_report(cur, g.user_id, on)
        rid = daily_coach_service.upsert_report(cur, g.user_id, report)
        conn.commit()
        report["id"] = rid
        return jsonify({"report": report})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[coach] run_daily error: {exc}")
        return jsonify({"error": str(exc)}), 500
    finally:
        close_db(conn)


# ────────────────────────────────────────────────────────────────────
#  GET /api/coach/history
# ────────────────────────────────────────────────────────────────────

@coach_bp.route("/api/coach/history", methods=["GET"])
@limiter.limit("60 per minute")
def get_history():
    err = _require_admin()
    if err:
        return err
    try:
        days = int(request.args.get("days", "30"))
    except Exception:
        days = 30
    days = max(7, min(180, days))

    conn = get_db()
    try:
        cur = conn.cursor()
        history = daily_coach_service.fetch_history(cur, g.user_id, days)
        return jsonify({"history": history, "days": days})
    except Exception as exc:
        print(f"[coach] get_history error: {exc}")
        return jsonify({"error": "Could not load history"}), 500
    finally:
        close_db(conn)


# ────────────────────────────────────────────────────────────────────
#  POST /api/coach/acknowledge/<id>
# ────────────────────────────────────────────────────────────────────

@coach_bp.route("/api/coach/acknowledge/<int:report_id>", methods=["POST"])
@limiter.limit("30 per minute")
def acknowledge(report_id: int):
    err = _require_admin()
    if err:
        return err
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE daily_coach_reports
            SET acknowledged_at = NOW()
            WHERE id = %s AND user_id = %s
            """,
            (report_id, g.user_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        return jsonify({"acknowledged": bool(ok)})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[coach] acknowledge error: {exc}")
        return jsonify({"error": "Could not acknowledge"}), 500
    finally:
        close_db(conn)


# ════════════════════════════════════════════════════════════════════
#  Manual AI-led Q&A check-in
#  /api/coach/qa/*
# ════════════════════════════════════════════════════════════════════

@coach_bp.route("/api/coach/qa/start", methods=["POST"])
@limiter.limit("20 per minute")
def qa_start():
    err = _require_admin()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    on = _parse_date(body.get("date"))

    conn = get_db()
    try:
        cur = conn.cursor()
        session = coach_qa_service.start_session(cur, g.user_id, on)
        conn.commit()
        return jsonify({"session": session})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[coach] qa_start error: {exc}")
        return jsonify({"error": str(exc)}), 500
    finally:
        close_db(conn)


@coach_bp.route("/api/coach/qa/<int:session_id>/answer", methods=["POST"])
@limiter.limit("60 per minute")
def qa_answer(session_id: int):
    err = _require_admin()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    answer = (body.get("answer") or "").strip()
    if not answer:
        return jsonify({"error": "answer is required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        result = coach_qa_service.submit_answer(cur, g.user_id, session_id, answer)
        conn.commit()
        return jsonify({"session": result})
    except ValueError as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[coach] qa_answer error: {exc}")
        return jsonify({"error": str(exc)}), 500
    finally:
        close_db(conn)


@coach_bp.route("/api/coach/qa/<int:session_id>/abandon", methods=["POST"])
@limiter.limit("30 per minute")
def qa_abandon(session_id: int):
    err = _require_admin()
    if err:
        return err
    conn = get_db()
    try:
        cur = conn.cursor()
        ok = coach_qa_service.abandon_session(cur, g.user_id, session_id)
        conn.commit()
        return jsonify({"abandoned": bool(ok)})
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[coach] qa_abandon error: {exc}")
        return jsonify({"error": "Could not abandon"}), 500
    finally:
        close_db(conn)


@coach_bp.route("/api/coach/qa/<int:session_id>", methods=["GET"])
@limiter.limit("60 per minute")
def qa_get(session_id: int):
    err = _require_admin()
    if err:
        return err
    conn = get_db()
    try:
        cur = conn.cursor()
        session = coach_qa_service.get_session(cur, g.user_id, session_id)
        if not session:
            return jsonify({"error": "Not found"}), 404
        return jsonify({"session": session})
    except Exception as exc:
        print(f"[coach] qa_get error: {exc}")
        return jsonify({"error": "Could not load session"}), 500
    finally:
        close_db(conn)


@coach_bp.route("/api/coach/qa/history", methods=["GET"])
@limiter.limit("60 per minute")
def qa_history():
    err = _require_admin()
    if err:
        return err
    try:
        days = int(request.args.get("days", "30"))
    except Exception:
        days = 30
    days = max(1, min(180, days))

    conn = get_db()
    try:
        cur = conn.cursor()
        sessions = coach_qa_service.list_sessions(cur, g.user_id, days)
        return jsonify({"sessions": sessions, "days": days})
    except Exception as exc:
        print(f"[coach] qa_history error: {exc}")
        return jsonify({"error": "Could not load history"}), 500
    finally:
        close_db(conn)
