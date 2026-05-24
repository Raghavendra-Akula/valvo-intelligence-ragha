"""
Deep Research orchestrator.

Glues dossier → prompt → frontier model → persisted report row.
The route layer just calls run_research(...) with auth-checked inputs.

Re-run intelligence
───────────────────
Frontier-model runs are slow + expensive (~₹40-80 per dossier). When the
user re-runs research on a stock that already has a recent report we:

  1. Check the most recent stored report for (symbol, mode).
  2. Compute the freshness watermark — the latest filing or quarterly
     filing date for this stock.
  3. If the prior report is fresh AND no new filings have landed since
     it was written, short-circuit and return the cached report with
     `cached: True` (zero model cost).
  4. Otherwise pass the prior report's markdown into a smaller
     "incremental update" prompt that asks the model to refresh only the
     sections affected by the new evidence. This typically halves the
     output tokens versus a full rewrite.

Either path can be bypassed with `force_fresh=True`.
"""
from __future__ import annotations

import json
import re
import traceback
from datetime import date, datetime, timedelta
from typing import Any

import psycopg2

from database.database import get_db

from services.deep_research.dossier import build_dossier
from services.deep_research.gateway import DeepResearchGateway, DEFAULT_MODEL
from services.deep_research.prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
    build_incremental_user_prompt,
)
from services.deep_research.verdict import extract_verdict


# Within this window, ANY existing report for (symbol, mode) is treated as
# a cache hit and we short-circuit — no model spend, no incremental update,
# even if new candles have landed. The user explicitly opted into this with
# "if I generated the same stock 4 hours ago, just give me that report".
# To force-regenerate inside this window the caller must pass force_fresh.
CACHE_HIT_MAX_AGE_DAYS = 15

# Past the cache window, an existing report is still useful as an anchor for
# an incremental refresh up to this many days old. Outside it we just rebuild
# the dossier from scratch and ignore the prior report.
INCREMENTAL_MAX_AGE_DAYS = 90


# The model is asked to emit a fenced JSON block at the end of the markdown,
# fenced as ```json:report-data ... ```. The fence may also drop the
# `:report-data` suffix; tolerate both.
_JSON_FENCE_RE = re.compile(
    r"```json(?::report-data)?\s*\n(?P<body>\{.*?\})\s*\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)


def extract_pdf_payload(content_md: str) -> tuple[str, dict | None]:
    """Pull the trailing JSON block out of the markdown response.

    Returns (markdown_without_block, parsed_json_or_None). The markdown is
    returned with the fence stripped so the UI canvas doesn't render raw
    JSON below section 8. Falls back to a permissive last-`{...}` scan if
    the fenced regex misses (e.g. the model forgot the language tag).
    """
    if not content_md:
        return content_md or "", None

    text = content_md.rstrip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        body = m.group("body")
        try:
            data = json.loads(body)
            cleaned = text[: m.start()].rstrip()
            return cleaned, data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass

    # Permissive fallback: find the last balanced { ... } chunk and try.
    last_open = text.rfind("\n{")
    if last_open != -1:
        candidate = text[last_open + 1:].strip()
        if candidate.endswith("```"):
            candidate = candidate[:-3].rstrip()
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and "header" in data:
                cleaned = text[: last_open].rstrip()
                if cleaned.endswith("```json:report-data") or cleaned.endswith("```json"):
                    cleaned = cleaned.rsplit("```", 1)[0].rstrip()
                return cleaned, data
        except json.JSONDecodeError:
            pass

    return text, None


_gateway: DeepResearchGateway | None = None


def _get_gateway() -> DeepResearchGateway:
    global _gateway
    if _gateway is None:
        _gateway = DeepResearchGateway()
    return _gateway


def run_research(
    *,
    symbol: str,
    mode: str = "retrospective",
    model: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    user_id: str | None = None,
    max_tokens: int = 16000,
    force_fresh: bool = False,
) -> dict[str, Any]:
    """End-to-end: build dossier, call frontier model, persist, return row.

    Returns: {report_id, symbol, company_name, mode, model_used, content_md,
              dossier, input_tokens, output_tokens, latency_ms, run_kind, ...}

    `run_kind` describes what the orchestrator actually did:
      - "fresh"        — full new dossier + full prompt (default path)
      - "cached"       — short-circuited, returned existing report row
      - "incremental"  — prior report passed in as anchor, model only
                         updated sections affected by new evidence

    Raises ValueError on bad symbol / dates, RuntimeError on model failure.
    """
    if mode not in ("retrospective", "forward"):
        raise ValueError(f"mode must be 'retrospective' or 'forward', got '{mode}'")

    dossier = build_dossier(
        symbol=symbol,
        from_date=from_date,
        to_date=to_date,
        mode=mode,
    )
    identity = dossier.get("identity") or {}
    window = dossier.get("window") or {}
    canonical_symbol = identity.get("symbol") or symbol
    sid = identity.get("security_id")

    # ── Re-run intelligence ─────────────────────────────────────────────
    prev_report = None
    watermark = None
    if not force_fresh:
        prev_report = _latest_report_for(canonical_symbol, mode)
        watermark = _freshness_watermark(sid) if sid else None

        if prev_report and _is_cache_hit(prev_report, watermark):
            return _format_cached_response(prev_report, watermark)

    # Build prompt — incremental if a prior report is recent enough to anchor
    use_incremental = (
        not force_fresh
        and prev_report is not None
        and _is_incremental_eligible(prev_report)
    )
    if use_incremental:
        prev_created = _coerce_date(prev_report.get("created_at"))
        user_prompt = build_incremental_user_prompt(
            dossier=dossier,
            prev_content_md=prev_report.get("content_md") or "",
            prev_created_at=str(prev_created) if prev_created else None,
            mode=mode,
            watermark=str(watermark) if watermark else None,
        )
    else:
        user_prompt = build_user_prompt(dossier, mode)

    gateway = _get_gateway()
    chosen_model = model or DEFAULT_MODEL
    result = gateway.complete(
        model=chosen_model,
        system=SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=max_tokens,
    )

    verdict = extract_verdict(markdown=result.text or "", mode=mode, dossier=dossier)

    # Strip the trailing JSON block out of the markdown so the UI canvas
    # doesn't render raw JSON, and persist the parsed dict separately so
    # the PDF endpoint can render directly from it.
    cleaned_md, report_json = extract_pdf_payload(result.text)

    row_id = _persist_report(
        symbol=canonical_symbol,
        company_name=identity.get("company_name"),
        mode=mode,
        model_used=chosen_model,
        window_start=window.get("from"),
        window_end=window.get("to"),
        content_md=cleaned_md,
        report_json=report_json,
        dossier=dossier,
        citations=result.citations,
        web_queries=result.web_queries,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
        user_id=user_id,
        verdict=verdict,
    )

    return {
        "report_id": row_id,
        "symbol": canonical_symbol,
        "company_name": identity.get("company_name"),
        "mode": mode,
        "model_used": chosen_model,
        "model_id": result.model_used,
        "content_md": cleaned_md,
        "report_json": report_json,
        "has_pdf_payload": report_json is not None,
        "dossier": dossier,
        "citations": result.citations,
        "web_queries": result.web_queries,
        "verdict": verdict,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "latency_ms": result.latency_ms,
        "run_kind": "incremental" if use_incremental else "fresh",
        "based_on_report_id": prev_report.get("id") if use_incremental and prev_report else None,
        "watermark": str(watermark) if watermark else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Re-run helpers
# ─────────────────────────────────────────────────────────────────────────────

def stream_research(
    *,
    symbol: str,
    mode: str = "retrospective",
    model: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    user_id: str | None = None,
    max_tokens: int = 16000,
    force_fresh: bool = False,
):
    """Streaming variant of run_research.

    Yields a sequence of typed dicts:
      {"type": "meta", "phase": "dossier_built", "dossier": {...}, "model": "...",
       "company_name": "...", "symbol": "...", "mode": "..."}
      {"type": "delta", "text": "..."}
      {"type": "citation", "title": "...", "url": "..."}
      {"type": "web_query", "query": "..."}
      {"type": "done", "report_id": int, "verdict": {...},
       "input_tokens": int, "output_tokens": int, "latency_ms": int,
       "content_md": "...", "citations": [...], "web_queries": [...]}
      {"type": "error", "error": "..."}

    Cached responses short-circuit: if a fresh report already exists, we yield
    a single `meta` event with phase=cached + the prior content_md + done.
    """
    if mode not in ("retrospective", "forward"):
        yield {"type": "error", "error": f"mode must be 'retrospective' or 'forward', got '{mode}'"}
        return

    try:
        dossier = build_dossier(
            symbol=symbol,
            from_date=from_date,
            to_date=to_date,
            mode=mode,
        )
    except ValueError as e:
        yield {"type": "error", "error": str(e)}
        return
    except Exception as e:
        traceback.print_exc()
        yield {"type": "error", "error": f"Dossier build failed: {e}"}
        return

    identity = dossier.get("identity") or {}
    window = dossier.get("window") or {}
    canonical_symbol = identity.get("symbol") or symbol
    sid = identity.get("security_id")
    chosen_model = model or DEFAULT_MODEL

    # Check cache.
    prev_report = None
    watermark = None
    if not force_fresh:
        prev_report = _latest_report_for(canonical_symbol, mode)
        watermark = _freshness_watermark(sid) if sid else None

        if prev_report and _is_cache_hit(prev_report, watermark):
            cached = _format_cached_response(prev_report, watermark)
            yield {
                "type": "meta",
                "phase": "cached",
                "symbol": canonical_symbol,
                "company_name": identity.get("company_name"),
                "mode": mode,
                "model": chosen_model,
                "dossier": dossier,
                "report_id": cached.get("report_id"),
            }
            # Replay the existing markdown as one big delta so the front-end can
            # render the cached report identically to a streamed one.
            content = cached.get("content_md") or ""
            if content:
                yield {"type": "delta", "text": content}
            yield {
                "type": "done",
                "report_id": cached.get("report_id"),
                "verdict": cached.get("verdict"),
                "content_md": content,
                "report_json": cached.get("report_json"),
                "has_pdf_payload": cached.get("has_pdf_payload", False),
                "citations": cached.get("citations") or [],
                "web_queries": cached.get("web_queries") or [],
                "input_tokens": cached.get("input_tokens") or 0,
                "output_tokens": cached.get("output_tokens") or 0,
                "latency_ms": 0,
                "run_kind": "cached",
                "cached": True,
                "watermark": str(watermark) if watermark else None,
            }
            return

    use_incremental = (
        not force_fresh
        and prev_report is not None
        and _is_incremental_eligible(prev_report)
    )
    if use_incremental:
        prev_created = _coerce_date(prev_report.get("created_at"))
        user_prompt = build_incremental_user_prompt(
            dossier=dossier,
            prev_content_md=prev_report.get("content_md") or "",
            prev_created_at=str(prev_created) if prev_created else None,
            mode=mode,
            watermark=str(watermark) if watermark else None,
        )
    else:
        user_prompt = build_user_prompt(dossier, mode)

    # Send the dossier up front so the UI can render the scoring trace and
    # cohort tables while the model is still drafting prose.
    yield {
        "type": "meta",
        "phase": "dossier_built",
        "symbol": canonical_symbol,
        "company_name": identity.get("company_name"),
        "mode": mode,
        "model": chosen_model,
        "dossier": dossier,
        "run_kind": "incremental" if use_incremental else "fresh",
        "based_on_report_id": prev_report.get("id") if use_incremental and prev_report else None,
    }

    gateway = _get_gateway()
    final_event: dict | None = None
    try:
        for ev in gateway.stream_complete(
            model=chosen_model,
            system=SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=max_tokens,
        ):
            if ev.get("type") == "done":
                final_event = ev
                # Don't forward yet; we still need to persist + verdict-extract.
                continue
            yield ev
    except Exception as e:
        traceback.print_exc()
        yield {"type": "error", "error": f"Model stream failed: {e}"}
        return

    if not final_event:
        yield {"type": "error", "error": "Model stream ended without a `done` event"}
        return

    content_md = final_event.get("full_text") or ""
    citations = final_event.get("citations") or []
    web_queries = final_event.get("web_queries") or []
    # Strip the trailing PDF JSON block before extracting verdict / persisting
    # / yielding back, so neither the verdict regex nor the UI canvas sees raw
    # JSON below section 8.
    cleaned_md, report_json = extract_pdf_payload(content_md)
    verdict = extract_verdict(markdown=cleaned_md, mode=mode, dossier=dossier)

    row_id = _persist_report(
        symbol=canonical_symbol,
        company_name=identity.get("company_name"),
        mode=mode,
        model_used=chosen_model,
        window_start=window.get("from"),
        window_end=window.get("to"),
        content_md=cleaned_md,
        report_json=report_json,
        dossier=dossier,
        citations=citations,
        web_queries=web_queries,
        input_tokens=int(final_event.get("input_tokens") or 0),
        output_tokens=int(final_event.get("output_tokens") or 0),
        latency_ms=int(final_event.get("latency_ms") or 0),
        user_id=user_id,
        verdict=verdict,
    )

    yield {
        "type": "done",
        "report_id": row_id,
        "symbol": canonical_symbol,
        "company_name": identity.get("company_name"),
        "mode": mode,
        "model_used": chosen_model,
        "model_id": final_event.get("model_used"),
        "content_md": cleaned_md,
        "report_json": report_json,
        "has_pdf_payload": report_json is not None,
        "verdict": verdict,
        "citations": citations,
        "web_queries": web_queries,
        "input_tokens": int(final_event.get("input_tokens") or 0),
        "output_tokens": int(final_event.get("output_tokens") or 0),
        "latency_ms": int(final_event.get("latency_ms") or 0),
        "run_kind": "incremental" if use_incremental else "fresh",
        "based_on_report_id": prev_report.get("id") if use_incremental and prev_report else None,
        "watermark": str(watermark) if watermark else None,
    }


def _latest_report_for(symbol: str, mode: str) -> dict | None:
    """Return the most recent stored report for (symbol, mode), or None."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, symbol, company_name, mode, model_used,
                   window_start, window_end, content_md,
                   report_json,
                   dossier_json, citations_json, web_queries_json,
                   verdict_json,
                   input_tokens, output_tokens, latency_ms,
                   created_by, created_at
              FROM deep_research_reports
             WHERE upper(symbol) = upper(%s) AND mode = %s
          ORDER BY created_at DESC
             LIMIT 1
            """,
            (symbol, mode),
        )
        r = cur.fetchone()
        return dict(r) if r else None
    except Exception as e:
        print(f"[deep_research/engine] _latest_report_for error: {e}")
        return None
    finally:
        conn.close()


def _freshness_watermark(security_id: int | None) -> date | None:
    """Latest `something happened` date for this stock — max of:
       filings.filing_date, quarterly_results.filing_date, candles_daily.date.
       This is what we compare against the prior report's `created_at` to
       decide whether anything material has landed since then.
    """
    if not security_id:
        return None
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT GREATEST(
                COALESCE((SELECT MAX(filing_date)
                            FROM filings
                           WHERE security_id = %s), '1900-01-01'::date),
                COALESCE((SELECT MAX(filing_date)
                            FROM quarterly_results
                           WHERE security_id = %s), '1900-01-01'::date),
                COALESCE((SELECT MAX(date)
                            FROM candles_daily
                           WHERE security_id = %s), '1900-01-01'::date)
            ) AS watermark
            """,
            (security_id, security_id, security_id),
        )
        r = cur.fetchone()
        if not r or not r.get("watermark"):
            return None
        wm = r["watermark"]
        if isinstance(wm, datetime):
            return wm.date()
        if isinstance(wm, date):
            return wm
        try:
            return datetime.strptime(str(wm), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    except Exception as e:
        print(f"[deep_research/engine] _freshness_watermark error: {e}")
        return None
    finally:
        conn.close()


def _coerce_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")).date()
    except (ValueError, TypeError):
        return None


def _is_cache_hit(prev_report: dict, watermark: date | None) -> bool:
    """Cache hit = prior report is younger than CACHE_HIT_MAX_AGE_DAYS.

    `watermark` is intentionally ignored. The original watermark guard
    invalidated the cache whenever a new daily candle landed for the
    stock — which is every weekday — so re-running the same dossier
    twice on the same day burned tokens both times. The user wants a
    pure age-based cache: within 15 days, give them the existing
    report; pass force_fresh to bypass.
    """
    prev_created = _coerce_date(prev_report.get("created_at"))
    if not prev_created:
        return False
    age = (date.today() - prev_created).days
    return 0 <= age <= CACHE_HIT_MAX_AGE_DAYS


def _is_incremental_eligible(prev_report: dict) -> bool:
    prev_created = _coerce_date(prev_report.get("created_at"))
    if not prev_created:
        return False
    age = (date.today() - prev_created).days
    return 0 <= age <= INCREMENTAL_MAX_AGE_DAYS


def _format_cached_response(prev_report: dict, watermark: date | None) -> dict:
    """Re-shape a stored row into the same envelope a fresh run returns."""
    out = {
        "report_id": prev_report.get("id"),
        "symbol": prev_report.get("symbol"),
        "company_name": prev_report.get("company_name"),
        "mode": prev_report.get("mode"),
        "model_used": prev_report.get("model_used"),
        "model_id": prev_report.get("model_used"),
        "content_md": prev_report.get("content_md") or "",
        "dossier": prev_report.get("dossier_json"),
        "citations": prev_report.get("citations_json") or [],
        "web_queries": prev_report.get("web_queries_json") or [],
        "verdict": prev_report.get("verdict_json"),
        "report_json": prev_report.get("report_json"),
        "has_pdf_payload": prev_report.get("report_json") is not None,
        "input_tokens": prev_report.get("input_tokens") or 0,
        "output_tokens": prev_report.get("output_tokens") or 0,
        "latency_ms": 0,
        "run_kind": "cached",
        "cached": True,
        "watermark": str(watermark) if watermark else None,
    }
    for k in ("window_start", "window_end", "created_at"):
        v = prev_report.get(k)
        if v is not None:
            out[k] = str(v)
    return out


def _render_pdf_bytes(report_json: dict | None, window: str | None) -> bytes | None:
    """Render the polished PDF in-memory. Returns None if no payload or render fails.

    Kept off the request path: the model already paid the cost of producing
    `report_json`; rendering it once at persist time means every download +
    the frontend iframe view serve the exact same bytes.
    """
    if not report_json:
        return None
    try:
        import io as _io
        from services.deep_research.pdf_template import render_report
        payload = dict(report_json)
        if window and "window" not in payload:
            payload["window"] = window
        buf = _io.BytesIO()
        render_report(buf, payload)
        return buf.getvalue()
    except Exception as e:
        print(f"[deep_research/engine] pdf render error: {e}")
        traceback.print_exc()
        return None


def _persist_report(
    *,
    symbol: str,
    company_name: str | None,
    mode: str,
    model_used: str,
    window_start: str | None,
    window_end: str | None,
    content_md: str,
    report_json: dict | None,
    dossier: dict,
    citations: list,
    web_queries: list,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    user_id: str | None,
    verdict: dict | None = None,
) -> int | None:
    window = None
    if window_start and window_end:
        window = f"{window_start} → {window_end}"
    pdf_bytes = _render_pdf_bytes(report_json, window)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO deep_research_reports
                (symbol, company_name, mode, model_used,
                 window_start, window_end, content_md,
                 report_json, dossier_json,
                 citations_json, web_queries_json, verdict_json,
                 input_tokens, output_tokens, latency_ms, created_by,
                 pdf_bytes, pdf_generated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    %s, %s, %s, %s,
                    %s, CASE WHEN %s::bytea IS NOT NULL THEN NOW() ELSE NULL END)
            RETURNING id
            """,
            (
                symbol, company_name, mode, model_used,
                window_start, window_end, content_md,
                json.dumps(report_json, default=str) if report_json else None,
                json.dumps(dossier, default=str),
                json.dumps(citations or [], default=str),
                json.dumps(web_queries or [], default=str),
                json.dumps(verdict, default=str) if verdict else None,
                input_tokens, output_tokens, latency_ms, user_id,
                psycopg2.Binary(pdf_bytes) if pdf_bytes else None,
                psycopg2.Binary(pdf_bytes) if pdf_bytes else None,
            ),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        return new_id
    except Exception as e:
        print(f"[deep_research/engine] persist error: {e}")
        traceback.print_exc()
        conn.rollback()
        return None
    finally:
        conn.close()


def get_pdf_bytes(report_id: int) -> bytes | None:
    """Fetch the stored PDF bytes for a report (NULL if not yet rendered)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pdf_bytes FROM deep_research_reports WHERE id = %s",
            (report_id,),
        )
        r = cur.fetchone()
        if not r:
            return None
        b = r.get("pdf_bytes")
        return bytes(b) if b is not None else None
    finally:
        conn.close()


def store_pdf_bytes(report_id: int, pdf_bytes: bytes) -> bool:
    """Persist a lazily-rendered PDF onto an existing report row."""
    if not pdf_bytes:
        return False
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE deep_research_reports SET pdf_bytes = %s, pdf_generated_at = NOW() WHERE id = %s",
            (psycopg2.Binary(pdf_bytes), report_id),
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"[deep_research/engine] store_pdf_bytes error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def list_reports_for_symbol(symbol: str, limit: int = 20) -> list[dict]:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, symbol, company_name, mode, model_used,
                   window_start, window_end,
                   verdict_json,
                   (report_json IS NOT NULL) AS has_pdf_payload,
                   input_tokens, output_tokens, latency_ms,
                   created_by, created_at
              FROM deep_research_reports
             WHERE upper(symbol) = upper(%s)
          ORDER BY created_at DESC
             LIMIT %s
            """,
            (symbol, limit),
        )
        rows = []
        for r in cur.fetchall():
            row = dict(r)
            for k in ("window_start", "window_end", "created_at"):
                if row.get(k) is not None:
                    row[k] = str(row[k])
            rows.append(row)
        return rows
    finally:
        conn.close()


def get_report(report_id: int) -> dict | None:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, symbol, company_name, mode, model_used,
                   window_start, window_end, content_md,
                   report_json, dossier_json,
                   citations_json, web_queries_json, verdict_json,
                   input_tokens, output_tokens, latency_ms,
                   created_by, created_at
              FROM deep_research_reports
             WHERE id = %s
            """,
            (report_id,),
        )
        r = cur.fetchone()
        if not r:
            return None
        row = dict(r)
        for k in ("window_start", "window_end", "created_at"):
            if row.get(k) is not None:
                row[k] = str(row[k])
        row["has_pdf_payload"] = row.get("report_json") is not None
        return row
    finally:
        conn.close()
