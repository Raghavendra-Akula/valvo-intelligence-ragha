"""
Classification V2 — HTTP routes.

All endpoints under /api/v2/classification:

    GET  /api/v2/classification/taxonomy
        Full V2 taxonomy (waves + themes + sub-sectors). Frontend
        hydration for the Magic "split by sub-sector" button.

    GET  /api/v2/classification/<symbol_or_sid>
        Single-stock spine — sector + sub-sector + themes/waves +
        top revenue segments + concall understanding + evidence trail.
        This powers the right-hand panel in StockDetailDrawer.

    GET  /api/v2/classification/by-theme/<slug>
        Stocks tagged to one V2 theme (with exposure / source).

    GET  /api/v2/classification/by-sub-sector/<slug>
        Stocks tagged to one V2 sub-sector.

    POST /api/v2/classification/init                       (admin)
        Apply migration + seed taxonomy. Idempotent.

    POST /api/v2/classification/classify                   (admin)
        Run the segment-driven classifier.
        Body: { dry_run?: bool, limit?: int, symbols?: [...] }

    POST /api/v2/classification/suggest-fix
        User-submitted correction. Goes into the review queue;
        approval applies it to the link tables.
        Body: {
            symbol|security_id: str,
            layer: 'sector' | 'sub_sector' | 'theme',
            current_value: str | null,
            suggested_value: str,
            reasoning: str | null
        }

    GET  /api/v2/classification/review-queue                (admin)
        Pending suggestions awaiting review.

    POST /api/v2/classification/review-queue/<id>/decide   (admin)
        Approve or reject a pending suggestion.
        Body: { decision: 'approved' | 'rejected', note?: str }

    POST /api/v2/classification/concall/<symbol>            (admin)
        [DISABLED by default — set V2_CONCALL_ENABLED=true to turn on]
        Read the most recent concall transcript for the symbol with
        Gemini, write to concall_understanding_v2, and re-run the V2
        classifier so the new evidence flows. Body (optional):
        { max_filings?: int, force?: bool, rerun_classifier?: bool }

    POST /api/v2/classification/concall/batch               (admin)
        [DISABLED by default — set V2_CONCALL_ENABLED=true to turn on]
        Same, but for an array of symbols (max 25 per call).
        Body: { symbols: [...], max_filings?: int, force?: bool,
                rerun_classifier?: bool }

V1 endpoints remain at /api/themes/* and /api/custom-sectors/* — V2 lives
on a parallel namespace until the frontend cuts over.
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request, g

from extensions import limiter
from database.database import get_db, close_db
from services.classification_v2 import classifier as v2_classifier
from services.classification_v2 import concall_classifier as v2_concall
from services.classification_v2 import schema as v2_schema
from services.classification_v2 import seeds as v2_seeds
from services.classification_v2 import spine as v2_spine


# ─────────────────────────────────────────────────────────────────────
# Feature flag: Gemini concall ingestion
# ─────────────────────────────────────────────────────────────────────
# Each call to /api/v2/classification/concall/* spends Gemini credits
# (~$0.003 / concall on Flash Lite). We don't have AI budget allocated
# yet, so the endpoints are registered but gated off — they 503 with a
# feature_disabled code until someone sets V2_CONCALL_ENABLED=true on
# the Cloud Run service. Flip via:
#
#     gcloud run services update valvo-backend \
#         --project=valvo-backend --region=asia-south1 \
#         --update-env-vars=V2_CONCALL_ENABLED=true
#
# Existing concall_understanding_v2 rows already in the DB remain in
# play — the classifier reads them at priority 6 with zero AI cost.
V2_CONCALL_ENABLED = os.getenv(
    "V2_CONCALL_ENABLED", "0",
).strip().lower() in ("1", "true", "yes", "on")


classification_v2_bp = Blueprint("classification_v2", __name__)


def _is_admin() -> bool:
    return bool(getattr(g, "is_admin", False)) or getattr(g, "user_email", "").endswith(
        "@valvointelligence.com"
    )


# ════════════════════════════════════════════════════════════════════
# Read endpoints
# ════════════════════════════════════════════════════════════════════
@classification_v2_bp.route("/api/v2/classification/taxonomy", methods=["GET"])
@limiter.limit("120 per minute")
def get_taxonomy():
    """All V2 waves, themes, and sub-sectors in one payload."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT slug, name, description, accent_color, sort_order
              FROM waves_v2
             WHERE COALESCE(is_active, true) = true
             ORDER BY sort_order, slug
        """)
        waves = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT slug, name, wave_slug, parent_sector,
                   description, accent_color, sort_order
              FROM themes_v2
             WHERE COALESCE(is_active, true) = true
             ORDER BY sort_order, slug
        """)
        themes = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT id, slug, name, parent_sector, description
              FROM custom_sectors_v2
             WHERE COALESCE(is_active, true) = true
             ORDER BY parent_sector, slug
        """)
        sub_sectors = [dict(r) for r in cur.fetchall()]

        return jsonify({
            "waves": waves,
            "themes": themes,
            "sub_sectors": sub_sectors,
            "counts": {
                "waves": len(waves),
                "themes": len(themes),
                "sub_sectors": len(sub_sectors),
            },
        })
    except Exception as e:
        print(f"[v2/taxonomy] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@classification_v2_bp.route("/api/v2/classification/<symbol_or_sid>", methods=["GET"])
@limiter.limit("120 per minute")
def get_spine_route(symbol_or_sid: str):
    """Single-stock spine for the right-hand panel."""
    try:
        spine = v2_spine.get_spine(symbol_or_sid)
        if "error" in spine:
            # Distinguish "not found" from internal errors
            code = 404 if "not found" in (spine.get("error") or "").lower() else 400
            return jsonify(spine), code
        return jsonify(spine)
    except Exception as e:
        print(f"[v2/spine] error for {symbol_or_sid}: {e}")
        return jsonify({"error": "Internal error"}), 500


@classification_v2_bp.route("/api/v2/classification/by-theme/<slug>", methods=["GET"])
@limiter.limit("60 per minute")
def stocks_by_theme(slug: str):
    """All stocks tagged with this V2 theme (joined to stock_universe)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT su.security_id, su.symbol, su.company_name, su.sector,
                   st.exposure_score, st.confidence, st.source,
                   st.is_primary, st.matched_term
              FROM stock_themes_v2 st
              JOIN stock_universe su ON su.security_id = st.security_id
             WHERE st.theme_slug = %s
             ORDER BY st.is_primary DESC,
                      st.exposure_score DESC NULLS LAST,
                      st.confidence DESC NULLS LAST,
                      su.symbol
        """, (slug,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            if d.get("exposure_score") is not None:
                d["exposure_score"] = float(d["exposure_score"])
            if d.get("confidence") is not None:
                d["confidence"] = float(d["confidence"])
            rows.append(d)
        return jsonify({"slug": slug, "stocks": rows, "count": len(rows)})
    except Exception as e:
        print(f"[v2/by-theme] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@classification_v2_bp.route("/api/v2/classification/by-sub-sector/<slug>", methods=["GET"])
@limiter.limit("60 per minute")
def stocks_by_sub_sector(slug: str):
    """All stocks tagged with this V2 sub-sector."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT su.security_id, su.symbol, su.company_name, su.sector,
                   cs.parent_sector, cs.name AS sub_sector_name,
                   scs.confidence, scs.source, scs.is_primary,
                   scs.matched_keyword
              FROM stock_custom_sector_v2 scs
              JOIN custom_sectors_v2 cs ON cs.id = scs.custom_sector_id
              JOIN stock_universe su ON su.security_id = scs.security_id
             WHERE cs.slug = %s
             ORDER BY scs.is_primary DESC,
                      scs.confidence DESC NULLS LAST,
                      su.symbol
        """, (slug,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            if d.get("confidence") is not None:
                d["confidence"] = float(d["confidence"])
            rows.append(d)
        return jsonify({"slug": slug, "stocks": rows, "count": len(rows)})
    except Exception as e:
        print(f"[v2/by-sub-sector] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ════════════════════════════════════════════════════════════════════
# Admin write endpoints
# ════════════════════════════════════════════════════════════════════
@classification_v2_bp.route("/api/v2/classification/init", methods=["POST"])
@limiter.limit("5 per minute")
def init_v2():
    """Apply the V2 migration + seed taxonomy. Idempotent — safe to re-run."""
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    try:
        applied = v2_schema.init_schema()
        seeded = v2_seeds.seed_taxonomy()
        return jsonify({"ok": True, "schema": applied, "seeded": seeded})
    except Exception as e:
        print(f"[v2/init] error: {e}")
        return jsonify({"error": str(e)}), 500


@classification_v2_bp.route("/api/v2/classification/classify", methods=["POST"])
@limiter.limit("3 per minute")
def classify_v2():
    """Run the V2 classifier — full universe or a symbol subset."""
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    body = request.get_json(silent=True) or {}
    try:
        summary = v2_classifier.classify_all(
            limit=body.get("limit"),
            symbols=body.get("symbols"),
            dry_run=bool(body.get("dry_run", False)),
        )
        # Trim the decisions array on big runs so the HTTP response stays small.
        if summary.get("decisions") and len(summary["decisions"]) > 200:
            summary["decisions_truncated"] = True
            summary["decisions"] = summary["decisions"][:200]
        return jsonify({"ok": True, **summary})
    except Exception as e:
        print(f"[v2/classify] error: {e}")
        return jsonify({"error": str(e)}), 500


# ════════════════════════════════════════════════════════════════════
# User feedback — Suggest fix
# ════════════════════════════════════════════════════════════════════
@classification_v2_bp.route("/api/v2/classification/suggest-fix", methods=["POST"])
@limiter.limit("30 per minute")
def suggest_fix():
    """Append a user correction to classification_review_queue_v2.

    We don't apply it directly — it sits as 'pending' until an admin
    decides via /review-queue/<id>/decide. This protects the V2 link
    tables from drive-by edits while still capturing the signal.
    """
    body = request.get_json(silent=True) or {}
    sym_or_sid = (body.get("symbol") or body.get("security_id") or "").strip()
    layer = (body.get("layer") or "").strip().lower()
    suggested = (body.get("suggested_value") or "").strip()
    current = (body.get("current_value") or "").strip() or None
    reasoning = (body.get("reasoning") or "").strip() or None

    if not sym_or_sid:
        return jsonify({"error": "symbol or security_id required"}), 400
    if layer not in ("sector", "sub_sector", "theme"):
        return jsonify({"error": "layer must be sector | sub_sector | theme"}), 400
    if not suggested:
        return jsonify({"error": "suggested_value required"}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT security_id, symbol FROM stock_universe
             WHERE security_id = %s OR upper(symbol) = upper(%s)
             LIMIT 1
        """, (sym_or_sid, sym_or_sid))
        stock = cur.fetchone()
        if not stock:
            return jsonify({"error": f"stock not found: {sym_or_sid}"}), 404

        sid = stock["security_id"]
        sym = stock["symbol"]
        submitter = getattr(g, "user_email", None) or "anonymous"

        cur.execute("""
            INSERT INTO classification_review_queue_v2
                (security_id, symbol, layer, current_value,
                 suggested_value, reasoning, submitted_by, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id
        """, (sid, sym, layer, current, suggested, reasoning, submitter))
        queue_id = cur.fetchone()["id"]
        conn.commit()
        return jsonify({
            "ok": True,
            "id": queue_id,
            "status": "pending",
            "security_id": sid,
            "symbol": sym,
        })
    except Exception as e:
        print(f"[v2/suggest-fix] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@classification_v2_bp.route("/api/v2/classification/review-queue", methods=["GET"])
@limiter.limit("60 per minute")
def list_review_queue():
    """Pending review queue — admin-gated."""
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    status_filter = (request.args.get("status") or "pending").strip().lower()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, security_id, symbol, layer, current_value,
                   suggested_value, reasoning, submitted_by, submitted_at,
                   status, reviewed_by, reviewed_at, applied_at, review_note
              FROM classification_review_queue_v2
             WHERE status = %s
             ORDER BY submitted_at DESC
             LIMIT 500
        """, (status_filter,))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for ts in ("submitted_at", "reviewed_at", "applied_at"):
                if d.get(ts) is not None:
                    d[ts] = d[ts].isoformat()
            rows.append(d)
        return jsonify({"status": status_filter, "rows": rows, "count": len(rows)})
    except Exception as e:
        print(f"[v2/review-queue] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


@classification_v2_bp.route("/api/v2/classification/review-queue/<int:queue_id>/decide", methods=["POST"])
@limiter.limit("30 per minute")
def decide_review_item(queue_id: int):
    """Approve or reject a review queue item.

    On approve we mark the row 'approved' and write a manual_override
    evidence entry. The actual link-table mutation is left to the
    classifier next time it runs (it skips manual_override rows on
    re-classification, so a one-shot insert of the new edge is enough
    — that's what the admin pipeline does today via reclassify scripts).
    Keeping this minimal avoids a large surgical patch of the link
    tables from a single approval.
    """
    if not _is_admin():
        return jsonify({"error": "Admin only"}), 403
    body = request.get_json(silent=True) or {}
    decision = (body.get("decision") or "").strip().lower()
    note = (body.get("note") or "").strip() or None
    if decision not in ("approved", "rejected"):
        return jsonify({"error": "decision must be approved or rejected"}), 400

    reviewer = getattr(g, "user_email", None) or "admin"
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, security_id, symbol, layer, current_value,
                   suggested_value, reasoning, status
              FROM classification_review_queue_v2
             WHERE id = %s
        """, (queue_id,))
        item = cur.fetchone()
        if not item:
            return jsonify({"error": "queue item not found"}), 404
        if item["status"] not in ("pending",):
            return jsonify({"error": f"item already {item['status']}"}), 400

        cur.execute("""
            UPDATE classification_review_queue_v2
               SET status = %s,
                   reviewed_by = %s,
                   reviewed_at = NOW(),
                   review_note = %s,
                   applied_at = CASE WHEN %s = 'approved' THEN NOW() ELSE applied_at END
             WHERE id = %s
        """, (decision, reviewer, note, decision, queue_id))

        if decision == "approved":
            cur.execute("""
                INSERT INTO classification_evidence_v2
                    (security_id, layer, value_slug, value_text,
                     evidence_kind, weight, confidence, matched_term,
                     evidence_data, source_ref, author)
                VALUES (%s, %s, %s, %s,
                        'manual_override', 1.0, 1.0, %s,
                        %s::jsonb, %s, %s)
            """, (
                item["security_id"], item["layer"],
                item["suggested_value"] if item["layer"] in ("sub_sector", "theme") else None,
                item["suggested_value"],
                item["suggested_value"],
                '{"queue_id": ' + str(queue_id) + '}',
                f"review_queue:{queue_id}",
                reviewer,
            ))

        conn.commit()
        return jsonify({"ok": True, "id": queue_id, "decision": decision})
    except Exception as e:
        print(f"[v2/review-decide] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        close_db(conn)


# ════════════════════════════════════════════════════════════════════
# Concall ingestion (Gemini understanding layer)
# ════════════════════════════════════════════════════════════════════
# These endpoints are slow — each symbol does a PDF download + Gemini
# call + classifier rerun. Typical latency 15–60s per symbol. Tight rate
# limit so a clicker can't fire 30 in a row.
_BATCH_MAX_SYMBOLS = 25


@classification_v2_bp.route(
    "/api/v2/classification/concall/<symbol>", methods=["POST"]
)
@limiter.limit("10 per minute")
def ingest_concall_one(symbol: str):
    """Admin: read latest concall PDF for one symbol with Gemini and
    update concall_understanding_v2 + re-run the V2 classifier."""
    if not V2_CONCALL_ENABLED:
        return jsonify({
            "error": "feature_disabled",
            "message": (
                "Concall AI ingestion is paused (no Gemini budget). "
                "Set V2_CONCALL_ENABLED=true on the Cloud Run service to enable."
            ),
        }), 503
    if not _is_admin():
        return jsonify({"error": "admin only"}), 403

    body = request.get_json(silent=True) or {}
    max_filings = max(1, min(int(body.get("max_filings", 1)), 4))
    force = bool(body.get("force", False))
    rerun = bool(body.get("rerun_classifier", True))

    try:
        report = v2_concall.ingest_symbol(
            symbol,
            max_filings=max_filings,
            force=force,
            rerun_classifier=rerun,
        )
        return jsonify(report)
    except Exception as e:
        print(f"[v2/concall/{symbol}] error: {e}")
        return jsonify({"error": "Internal error", "detail": str(e)[:300]}), 500


@classification_v2_bp.route(
    "/api/v2/classification/concall/batch", methods=["POST"]
)
@limiter.limit("4 per minute")
def ingest_concall_batch():
    """Admin: ingest concall for an array of symbols. Capped at 25."""
    if not V2_CONCALL_ENABLED:
        return jsonify({
            "error": "feature_disabled",
            "message": (
                "Concall AI ingestion is paused (no Gemini budget). "
                "Set V2_CONCALL_ENABLED=true on the Cloud Run service to enable."
            ),
        }), 503
    if not _is_admin():
        return jsonify({"error": "admin only"}), 403

    body = request.get_json(silent=True) or {}
    symbols = body.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        return jsonify({"error": "symbols (list) required"}), 400
    symbols = [s for s in symbols if isinstance(s, str) and s.strip()]
    if len(symbols) > _BATCH_MAX_SYMBOLS:
        return jsonify({
            "error": f"too many symbols (max {_BATCH_MAX_SYMBOLS})",
            "count": len(symbols),
        }), 400

    max_filings = max(1, min(int(body.get("max_filings", 1)), 4))
    force = bool(body.get("force", False))
    rerun = bool(body.get("rerun_classifier", True))
    sleep_between = max(0.0, min(float(body.get("sleep_between", 0.5)), 5.0))

    try:
        reports = v2_concall.ingest_symbols(
            symbols,
            max_filings=max_filings,
            force=force,
            rerun_classifier=rerun,
            sleep_between=sleep_between,
        )
        ok = sum(1 for r in reports if r.get("ingested"))
        skipped = sum(1 for r in reports if r.get("skipped") and not r.get("ingested"))
        errored = sum(1 for r in reports if r.get("errors") and not r.get("ingested"))
        tokens_in = sum(r.get("tokens_in", 0) for r in reports)
        tokens_out = sum(r.get("tokens_out", 0) for r in reports)
        return jsonify({
            "summary": {
                "total": len(reports),
                "ingested": ok,
                "skipped_only": skipped,
                "errored_only": errored,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
            "reports": reports,
        })
    except Exception as e:
        print(f"[v2/concall/batch] error: {e}")
        return jsonify({"error": "Internal error", "detail": str(e)[:300]}), 500
