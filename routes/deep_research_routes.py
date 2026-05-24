"""
Deep Research routes — admin only.

Endpoints:
  POST /api/deep-research/run                run a fresh report (blocking)
  POST /api/deep-research/run-stream         SSE-streamed report generation
  GET  /api/deep-research/reports/<symbol>   list past reports for a symbol
  GET  /api/deep-research/report/<id>        fetch one report by id
  GET  /api/deep-research/report/<id>/pdf    download polished PDF artifact
  GET  /api/deep-research/models             list available frontier models
  GET  /api/deep-research/health             sanity probe

Every endpoint is gated by services.admin_service.is_admin(g.user_id).
The /run endpoint is rate-limited to keep frontier-model spend predictable.
"""
from __future__ import annotations

import io
import json
import re
import traceback
from datetime import datetime

from flask import (
    Blueprint, Response, g, jsonify, request, send_file, stream_with_context,
)

from extensions import limiter
from services.admin_service import is_admin, log_admin_action
from services.deep_research.engine import (
    run_research,
    stream_research,
    list_reports_for_symbol,
    get_report,
    get_pdf_bytes,
    store_pdf_bytes,
)
from services.deep_research.gateway import DeepResearchGateway, MODEL_REGISTRY

deep_research_bp = Blueprint("deep_research_bp", __name__)


def _require_admin():
    """Returns (error_response, status) or None if caller is admin."""
    user_id = getattr(g, "user_id", None)
    if not user_id or not is_admin(user_id):
        return jsonify({"error": "Admin only"}), 403
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Run a fresh report
# ─────────────────────────────────────────────────────────────────────────────

@deep_research_bp.route("/api/deep-research/run", methods=["POST"])
@limiter.limit("10 per minute")
def run():
    err = _require_admin()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    mode = (body.get("mode") or "retrospective").strip().lower()
    model = (body.get("model") or "").strip() or None
    from_date = body.get("from_date") or None
    to_date = body.get("to_date") or None
    force_fresh = bool(body.get("force_fresh"))

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    if mode not in ("retrospective", "forward"):
        return jsonify({"error": "mode must be 'retrospective' or 'forward'"}), 400
    if model and model not in MODEL_REGISTRY:
        return jsonify({
            "error": f"Unknown model '{model}'. Allowed: {list(MODEL_REGISTRY.keys())}"
        }), 400

    try:
        result = run_research(
            symbol=symbol,
            mode=mode,
            model=model,
            from_date=from_date,
            to_date=to_date,
            user_id=g.user_id,
            force_fresh=force_fresh,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Deep Research run failed: {e}"}), 500

    try:
        log_admin_action(
            user_id=g.user_id,
            action="deep_research_run",
            details={
                "symbol": symbol,
                "mode": mode,
                "model": model,
                "from_date": from_date,
                "to_date": to_date,
                "force_fresh": force_fresh,
                "run_kind": result.get("run_kind"),
                "report_id": result.get("report_id"),
                "based_on_report_id": result.get("based_on_report_id"),
                "input_tokens": result.get("input_tokens"),
                "output_tokens": result.get("output_tokens"),
            },
        )
    except Exception:
        pass

    return jsonify(result), 200


# ─────────────────────────────────────────────────────────────────────────────
#  Run a fresh report — Server-Sent Events stream
# ─────────────────────────────────────────────────────────────────────────────
#
# Wire format: each event is `data: <json>\n\n`. The JSON carries a `type`:
#   meta       — dossier built / cached short-circuit (front-end can render
#                scoring trace + cohort tables immediately from `dossier`)
#   delta      — partial markdown chunk (append to live preview)
#   citation   — a web source surfaced mid-stream (render in sources panel)
#   web_query  — a search query the model issued (debug / breadcrumb panel)
#   done       — final payload with report_id, verdict, totals, content_md
#   error      — fatal error, stream ends
#
# The front-end consumes via fetch() + ReadableStream so it can carry the JWT
# Authorization header. EventSource doesn't support custom headers, so we
# don't use it — but the wire format stays standard SSE for inspection.
#
# Auth/admin and audit-log behavior matches /run.

@deep_research_bp.route("/api/deep-research/run-stream", methods=["POST"])
@limiter.limit("10 per minute")
def run_stream():
    err = _require_admin()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    mode = (body.get("mode") or "retrospective").strip().lower()
    model = (body.get("model") or "").strip() or None
    from_date = body.get("from_date") or None
    to_date = body.get("to_date") or None
    force_fresh = bool(body.get("force_fresh"))

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    if mode not in ("retrospective", "forward"):
        return jsonify({"error": "mode must be 'retrospective' or 'forward'"}), 400
    if model and model not in MODEL_REGISTRY:
        return jsonify({
            "error": f"Unknown model '{model}'. Allowed: {list(MODEL_REGISTRY.keys())}"
        }), 400

    user_id = g.user_id

    def _sse_frame(payload: dict) -> bytes:
        return f"data: {json.dumps(payload, default=str)}\n\n".encode("utf-8")

    def _audit(result: dict):
        try:
            log_admin_action(
                user_id=user_id,
                action="deep_research_run_stream",
                details={
                    "symbol": symbol,
                    "mode": mode,
                    "model": model,
                    "from_date": from_date,
                    "to_date": to_date,
                    "force_fresh": force_fresh,
                    "run_kind": result.get("run_kind"),
                    "report_id": result.get("report_id"),
                    "based_on_report_id": result.get("based_on_report_id"),
                    "input_tokens": result.get("input_tokens"),
                    "output_tokens": result.get("output_tokens"),
                },
            )
        except Exception:
            pass

    @stream_with_context
    def generator():
        # Initial keepalive comment so the browser doesn't buffer the first event.
        yield b": deep-research-stream\n\n"
        last_event = None
        try:
            for ev in stream_research(
                symbol=symbol,
                mode=mode,
                model=model,
                from_date=from_date,
                to_date=to_date,
                user_id=user_id,
                force_fresh=force_fresh,
            ):
                last_event = ev
                yield _sse_frame(ev)
                if ev.get("type") in ("done", "error"):
                    break
        except Exception as e:
            traceback.print_exc()
            yield _sse_frame({"type": "error", "error": f"Deep Research stream failed: {e}"})
            return

        if last_event and last_event.get("type") == "done":
            _audit(last_event)

    return Response(
        generator(),
        # Explicit utf-8 so the browser never falls back to Latin-1 for the
        # rupee glyph (₹) or emoji markers used in the timeline.
        mimetype="text/event-stream; charset=utf-8",
        headers={
            # Prevent buffering at proxies/Cloud Run.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
#  List past reports for a symbol
# ─────────────────────────────────────────────────────────────────────────────

@deep_research_bp.route("/api/deep-research/reports/<symbol>", methods=["GET"])
def list_for_symbol(symbol: str):
    err = _require_admin()
    if err:
        return err
    try:
        limit = int(request.args.get("limit", 20))
    except ValueError:
        limit = 20
    rows = list_reports_for_symbol(symbol.upper(), limit=limit)
    return jsonify({"symbol": symbol.upper(), "reports": rows}), 200


# ─────────────────────────────────────────────────────────────────────────────
#  Peek at the cache for (symbol, mode) — no model spend.
# ─────────────────────────────────────────────────────────────────────────────
# The UI calls this BEFORE clicking Generate so it can decide whether to
# (a) instantly show the cached report (age <= CACHE_HIT_MAX_AGE_DAYS),
# (b) prompt "this report is N days old — refresh or keep?" (age > window),
# or (c) just generate fresh (no prior report).
#
# Returns: { symbol, mode, has_cache: bool, age_days, fresh: bool,
#            latest_report_id, latest_created_at, cache_window_days }

@deep_research_bp.route("/api/deep-research/cache-status/<symbol>", methods=["GET"])
def cache_status(symbol: str):
    err = _require_admin()
    if err:
        return err
    from datetime import date, datetime
    from services.deep_research.engine import (
        _latest_report_for, CACHE_HIT_MAX_AGE_DAYS,
    )
    mode = (request.args.get("mode") or "retrospective").strip().lower()
    if mode not in ("retrospective", "forward"):
        return jsonify({"error": "mode must be 'retrospective' or 'forward'"}), 400
    prev = _latest_report_for(symbol.upper(), mode)
    if not prev:
        return jsonify({
            "symbol": symbol.upper(), "mode": mode,
            "has_cache": False, "fresh": False,
            "cache_window_days": CACHE_HIT_MAX_AGE_DAYS,
        }), 200
    created = prev.get("created_at")
    age_days = None
    if created:
        if isinstance(created, datetime):
            age_days = (date.today() - created.date()).days
        else:
            try:
                age_days = (date.today() - created).days
            except Exception:
                age_days = None
    fresh = age_days is not None and 0 <= age_days <= CACHE_HIT_MAX_AGE_DAYS
    return jsonify({
        "symbol": symbol.upper(),
        "mode": mode,
        "has_cache": True,
        "fresh": fresh,
        "age_days": age_days,
        "latest_report_id": prev.get("id"),
        "latest_created_at": str(created) if created else None,
        "model_used": prev.get("model_used"),
        "window_start": str(prev.get("window_start")) if prev.get("window_start") else None,
        "window_end": str(prev.get("window_end")) if prev.get("window_end") else None,
        "cache_window_days": CACHE_HIT_MAX_AGE_DAYS,
    }), 200


# ─────────────────────────────────────────────────────────────────────────────
#  Get one report by id
# ─────────────────────────────────────────────────────────────────────────────

@deep_research_bp.route("/api/deep-research/report/<int:report_id>", methods=["GET"])
def get_one(report_id: int):
    err = _require_admin()
    if err:
        return err
    row = get_report(report_id)
    if not row:
        return jsonify({"error": "Report not found"}), 404
    return jsonify(row), 200


# ─────────────────────────────────────────────────────────────────────────────
# Download a polished PDF artifact for one report
# ─────────────────────────────────────────────────────────────────────────────

_FNAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@deep_research_bp.route("/api/deep-research/report/<int:report_id>/pdf", methods=["GET"])
def download_pdf(report_id: int):
    err = _require_admin()
    if err:
        return err

    row = get_report(report_id)
    if not row:
        return jsonify({"error": "Report not found"}), 404

    # Frontend uses ?inline=1 to render the PDF in an <iframe>; the default
    # path remains an attachment download for the "Download PDF" button.
    inline = request.args.get("inline") in ("1", "true", "yes")

    # Prefer the stored bytes — rendered once at persist time so every download
    # and the frontend iframe view serve the exact same artefact.
    stored = get_pdf_bytes(report_id)
    if not stored:
        payload = row.get("report_json")
        if not payload:
            return jsonify({
                "error": "This report has no PDF payload yet. Re-run the report — the "
                         "model now emits a structured JSON block we can render.",
            }), 409
        if "window" not in payload:
            ws, we = row.get("window_start"), row.get("window_end")
            if ws and we:
                payload["window"] = f"{ws} → {we}"
        try:
            from services.deep_research.pdf_template import render_report
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": f"PDF renderer unavailable: {e}"}), 500
        buf = io.BytesIO()
        try:
            render_report(buf, payload)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"error": f"PDF render failed: {e}"}), 500
        stored = buf.getvalue()
        # Backfill so subsequent loads (and the iframe view) skip the render.
        try:
            store_pdf_bytes(report_id, stored)
        except Exception:
            traceback.print_exc()

    symbol = (row.get("symbol") or "REPORT").upper()
    company = (row.get("company_name") or symbol).split("(")[0].strip()
    safe = _FNAME_RE.sub("_", company)[:48].strip("_") or symbol
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
    filename = f"{symbol}_{safe}_{stamp}.pdf"

    if not inline:
        try:
            log_admin_action(
                user_id=g.user_id,
                action="deep_research_pdf_download",
                details={"symbol": symbol, "report_id": report_id, "filename": filename},
            )
        except Exception:
            pass

    return send_file(
        io.BytesIO(stored),
        mimetype="application/pdf",
        as_attachment=not inline,
        download_name=filename,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  List available frontier models (so the UI can populate the picker)
# ─────────────────────────────────────────────────────────────────────────────

@deep_research_bp.route("/api/deep-research/models", methods=["GET"])
def list_models():
    err = _require_admin()
    if err:
        return err
    return jsonify({"models": DeepResearchGateway().available_models()}), 200


# ─────────────────────────────────────────────────────────────────────────────
#  Health check (admin only — exposes which keys are wired)
# ─────────────────────────────────────────────────────────────────────────────

@deep_research_bp.route("/api/deep-research/health", methods=["GET"])
def health():
    err = _require_admin()
    if err:
        return err
    return jsonify(DeepResearchGateway().health()), 200


# ═════════════════════════════════════════════════════════════════════════════
#  CLAUDE-CODE WORKER QUEUE
#
#  Browser POSTs to /queue-claude-code → row in research_jobs (status=queued).
#  A long-running Claude Code session (`/research-worker` slash command) calls
#  /jobs/dequeue to atomically claim the next queued job, runs WebSearch +
#  synthesis + persists into deep_research_reports, then POSTs /jobs/<id>/done
#  with the new report_id. Browser polls /jobs/<id>/status and renders the
#  report inline once status flips to 'done'.
# ═════════════════════════════════════════════════════════════════════════════

from database.database import close_db, get_db


_VALID_MODES = {"retrospective", "forward"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _coerce_date(s):
    if not s:
        return None
    if not _DATE_RE.match(s):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


@deep_research_bp.route("/api/deep-research/queue-claude-code", methods=["POST"])
@limiter.limit("60 per minute")
def queue_claude_code():
    """Browser → enqueue a research job for the Claude Code worker."""
    err = _require_admin()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    mode = (body.get("mode") or "").strip().lower()
    from_date = _coerce_date(body.get("from_date"))
    to_date = _coerce_date(body.get("to_date"))

    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    if mode not in _VALID_MODES:
        return jsonify({"error": f"mode must be one of {sorted(_VALID_MODES)}"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO research_jobs
                (symbol, mode, from_date, to_date, requested_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, symbol, mode, from_date, to_date, status, requested_at
            """,
            (symbol, mode, from_date, to_date, g.user_id),
        )
        row = cur.fetchone()
        conn.commit()

        try:
            log_admin_action(
                user_id=g.user_id,
                action="deep_research_queue",
                details={"symbol": symbol, "mode": mode, "job_id": row["id"]},
            )
        except Exception:
            pass

        return jsonify({
            "job_id": row["id"],
            "symbol": row["symbol"],
            "mode": row["mode"],
            "status": row["status"],
            "requested_at": row["requested_at"].isoformat() if row.get("requested_at") else None,
        }), 201
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


@deep_research_bp.route("/api/deep-research/jobs/dequeue", methods=["POST"])
@limiter.limit("120 per minute")
def dequeue_job():
    """Worker → atomically claim the next queued job.

    Body (optional):
        worker_id (str) — host:pid identifier so the UI can show "Worker online"

    Returns 200 with {job: {...}} when a job is claimed,
    200 with {job: null} when the queue is empty.
    """
    err = _require_admin()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    worker_id = (body.get("worker_id") or "unknown").strip()[:64]

    conn = get_db()
    try:
        cur = conn.cursor()

        # Heartbeat: every dequeue counts as "I'm alive". The UI uses this
        # to show "Worker online" / "No worker running" in the header.
        cur.execute(
            """
            INSERT INTO research_workers (worker_id, user_id, last_polled_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (worker_id) DO UPDATE
              SET last_polled_at = NOW()
            """,
            (worker_id, g.user_id),
        )

        # Stale sweep — any job stuck 'running' for more than 10 minutes is
        # presumed dead (worker session crashed mid-job). Mark expired so the
        # next operator sees the failure rather than an indefinite spinner.
        cur.execute(
            """
            UPDATE research_jobs
               SET status = 'expired',
                   error  = COALESCE(error, 'stale (no completion in 10 min)'),
                   finished_at = NOW()
             WHERE status = 'running'
               AND started_at < NOW() - INTERVAL '10 minutes'
            """
        )

        # Atomic claim: SKIP LOCKED lets multiple workers run safely in
        # parallel without double-claiming the same job.
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                  FROM research_jobs
                 WHERE status = 'queued'
              ORDER BY requested_at ASC
                 LIMIT 1
                   FOR UPDATE SKIP LOCKED
            )
            UPDATE research_jobs
               SET status     = 'running',
                   claimed_by = %s,
                   started_at = NOW()
             WHERE id = (SELECT id FROM next_job)
         RETURNING id, symbol, mode, from_date, to_date, requested_by, requested_at
            """,
            (worker_id,),
        )
        job = cur.fetchone()
        conn.commit()

        if not job:
            return jsonify({"job": None}), 200

        return jsonify({"job": {
            "id":            job["id"],
            "symbol":        job["symbol"],
            "mode":          job["mode"],
            "from_date":     job["from_date"].isoformat() if job.get("from_date") else None,
            "to_date":       job["to_date"].isoformat() if job.get("to_date") else None,
            "requested_by":  job["requested_by"],
            "requested_at":  job["requested_at"].isoformat() if job.get("requested_at") else None,
        }}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


@deep_research_bp.route("/api/deep-research/jobs/<int:job_id>/done", methods=["POST"])
@limiter.limit("60 per minute")
def mark_job_done(job_id: int):
    """Worker → mark a claimed job as completed and link to the persisted report."""
    err = _require_admin()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    report_id = body.get("report_id")
    if not isinstance(report_id, int):
        return jsonify({"error": "report_id (int) required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE research_jobs
               SET status      = 'done',
                   report_id   = %s,
                   finished_at = NOW(),
                   error       = NULL
             WHERE id = %s
               AND status = 'running'
         RETURNING id, status, report_id
            """,
            (report_id, job_id),
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "job not in running state (or not found)"}), 409
        return jsonify({"job_id": row["id"], "status": row["status"], "report_id": row["report_id"]}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


@deep_research_bp.route("/api/deep-research/jobs/<int:job_id>/failed", methods=["POST"])
@limiter.limit("60 per minute")
def mark_job_failed(job_id: int):
    """Worker → mark a claimed job as failed with an error message."""
    err = _require_admin()
    if err:
        return err

    body = request.get_json(force=True, silent=True) or {}
    error_msg = (body.get("error") or "unspecified worker error").strip()[:1000]

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE research_jobs
               SET status      = 'failed',
                   error       = %s,
                   finished_at = NOW()
             WHERE id = %s
               AND status IN ('running', 'queued')
         RETURNING id, status
            """,
            (error_msg, job_id),
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "job not found or already finalized"}), 409
        return jsonify({"job_id": row["id"], "status": row["status"]}), 200
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


@deep_research_bp.route("/api/deep-research/jobs/<int:job_id>/status", methods=["GET"])
@limiter.limit("300 per minute")
def get_job_status(job_id: int):
    """Browser → poll the status of a queued/running job."""
    err = _require_admin()
    if err:
        return err

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, symbol, mode, from_date, to_date,
                   status, report_id, error,
                   requested_at, started_at, finished_at,
                   claimed_by
              FROM research_jobs
             WHERE id = %s
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "job not found"}), 404
        return jsonify({
            "id":           row["id"],
            "symbol":       row["symbol"],
            "mode":         row["mode"],
            "from_date":    row["from_date"].isoformat() if row.get("from_date") else None,
            "to_date":      row["to_date"].isoformat() if row.get("to_date") else None,
            "status":       row["status"],
            "report_id":    row["report_id"],
            "error":        row["error"],
            "requested_at": row["requested_at"].isoformat() if row.get("requested_at") else None,
            "started_at":   row["started_at"].isoformat() if row.get("started_at") else None,
            "finished_at":  row["finished_at"].isoformat() if row.get("finished_at") else None,
            "claimed_by":   row["claimed_by"],
        }), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


@deep_research_bp.route("/api/deep-research/worker-status", methods=["GET"])
@limiter.limit("120 per minute")
def worker_status():
    """Browser → check whether any worker has polled recently.

    Returns {online: bool, last_polled_at: iso|null, worker_count: int}.
    A worker is considered online if it polled within the last 30 seconds
    (worker polls every 5s normally, so 30s gives 6× the dequeue interval
    of slack for momentary lag).
    """
    err = _require_admin()
    if err:
        return err

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT worker_id, last_polled_at
              FROM research_workers
             WHERE last_polled_at > NOW() - INTERVAL '30 seconds'
          ORDER BY last_polled_at DESC
            """
        )
        rows = cur.fetchall()
        if not rows:
            cur.execute("SELECT MAX(last_polled_at) AS last FROM research_workers")
            last = cur.fetchone()
            return jsonify({
                "online": False,
                "worker_count": 0,
                "last_polled_at": last["last"].isoformat() if last and last.get("last") else None,
            }), 200
        return jsonify({
            "online": True,
            "worker_count": len(rows),
            "last_polled_at": rows[0]["last_polled_at"].isoformat(),
            "workers": [{"id": r["worker_id"], "polled_at": r["last_polled_at"].isoformat()} for r in rows],
        }), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        close_db(conn)


# ═════════════════════════════════════════════════════════════════════════════
#  CLAUDE-CODE WORKER SCRIPT ENDPOINTS — let scripts/research_*.py talk to
#  Supabase via HTTPS so the worker can run from any environment (web Claude
#  Code, codespace, cloud VM) using just an admin API key — no DB egress
#  required on the worker host.
# ═════════════════════════════════════════════════════════════════════════════

from services.deep_research.dossier import build_dossier
from services.deep_research.engine import _persist_report


@deep_research_bp.route("/api/deep-research/dossier", methods=["GET"])
@limiter.limit("120 per minute")
def get_dossier():
    """Return the structured dossier for a stock — no LLM, just SQL.

    Query params:
        symbol      (required)
        mode        retrospective | forward (default retrospective)
        from_date   YYYY-MM-DD (optional)
        to_date     YYYY-MM-DD (optional)

    Used by scripts/research_dossier.py over HTTPS.
    """
    err = _require_admin()
    if err:
        return err

    symbol = (request.args.get("symbol") or "").strip().upper()
    mode = (request.args.get("mode") or "retrospective").strip().lower()
    from_date = request.args.get("from_date") or None
    to_date = request.args.get("to_date") or None

    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    if mode not in {"retrospective", "forward"}:
        return jsonify({"error": "mode must be 'retrospective' or 'forward'"}), 400

    try:
        dossier = build_dossier(
            symbol=symbol, mode=mode,
            from_date=from_date, to_date=to_date,
        )
        return Response(
            json.dumps(dossier, default=str, ensure_ascii=False),
            mimetype="application/json",
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@deep_research_bp.route("/api/deep-research/save-from-claude", methods=["POST"])
@limiter.limit("60 per minute")
def save_from_claude():
    """Persist a Claude-Code-synthesised report into deep_research_reports.

    Body shape (see scripts/research_save.py docstring):
        symbol, mode, content_md, report_json   (required)
        company_name, window_start, window_end, dossier, citations,
        web_queries, verdict                    (optional)

    Persists with model_used='claude-code', input_tokens=0, output_tokens=0,
    latency_ms=0. Returns {report_id, symbol, mode, url}.
    """
    err = _require_admin()
    if err:
        return err

    payload = request.get_json(force=True, silent=True) or {}
    required = ("symbol", "mode", "content_md", "report_json")
    missing = [k for k in required if not payload.get(k)]
    if missing:
        return jsonify({"error": f"missing required keys: {missing}"}), 400

    mode = payload["mode"]
    if mode not in {"retrospective", "forward"}:
        return jsonify({"error": "mode must be 'retrospective' or 'forward'"}), 400

    try:
        new_id = _persist_report(
            symbol=payload["symbol"].upper(),
            company_name=payload.get("company_name"),
            mode=mode,
            model_used="claude-code",
            window_start=payload.get("window_start"),
            window_end=payload.get("window_end"),
            content_md=payload["content_md"],
            report_json=payload["report_json"],
            dossier=payload.get("dossier") or {},
            citations=payload.get("citations") or [],
            web_queries=payload.get("web_queries") or [],
            input_tokens=0,
            output_tokens=0,
            latency_ms=0,
            user_id=g.user_id,
            verdict=payload.get("verdict"),
        )
        if new_id is None:
            return jsonify({"error": "persist failed (see backend log)"}), 500

        try:
            log_admin_action(
                user_id=g.user_id,
                action="deep_research_save_from_claude",
                details={"symbol": payload["symbol"], "mode": mode, "report_id": new_id},
            )
        except Exception:
            pass

        return jsonify({
            "report_id": new_id,
            "symbol":    payload["symbol"].upper(),
            "mode":      mode,
        }), 201
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
