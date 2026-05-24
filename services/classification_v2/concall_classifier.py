"""
Concall understanding pipeline (V2).

This is the "final understanding layer" — for stocks where the segment
schedule is too coarse (single 100% segment, geography-only segments,
or where the AI/DC pivot is forward-looking and not yet revenue-visible)
we read the most recent earnings-call transcript with Gemini and ask it
to estimate thematic exposure on the V2 taxonomy.

The result lands in `concall_understanding_v2`. The classifier (Layer C
in `classifier.py`) already consumes that table at priority 6 — between
manual / curated overrides and segment-revenue evidence — so once a row
is written, re-running `classify_all(symbols=[symbol])` lifts the new
evidence into themes / sub-sectors / sector decisions and writes a
matching audit row to `classification_evidence_v2`.

Cost note (Gemini Flash Lite, ~$0.10/M in, ~$0.40/M out): a 30-page
concall is ~25k input tokens + ~1.5k output → roughly $0.003 per stock.

Public API:
    ingest_symbol(symbol, *, max_filings=1, force=False)
        → ingest the latest concall(s) for one symbol, then re-run
          the V2 classifier and return a small report dict.

    ingest_symbols(symbols, *, max_filings=1, force=False)
        → batch wrapper.

CLI:
    python -m services.classification_v2.concall_classifier STLTECH HFCL
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import date, datetime
from typing import Any, Iterable, Optional

from database.database import get_db, close_db


# ─────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────
GEMINI_MODEL = os.getenv("V2_CONCALL_MODEL", "gemini-2.5-flash-lite")
GEMINI_TIMEOUT_MS = 180 * 1000        # google-genai HttpOptions.timeout is in ms

# Filing types we will read, in order of preference
PREFERRED_FILING_TYPES = ("CONCALL_TRANSCRIPT", "INVESTOR_PRESENTATION")

# Cap PDF size so a stray 50MB monster can't blow up tokens / RAM
MAX_PDF_BYTES = 12 * 1024 * 1024      # 12 MB

# Skip re-extraction unless force=True
RECENT_REGEN_GRACE_DAYS = 60

# A theme exposure below this is dropped from `themes_extracted` so the
# classifier doesn't act on noise. Matches CONCALL_MIN_EXPOSURE in
# classifier.py.
MIN_EXPOSURE_KEEP = 0.10


# ─────────────────────────────────────────────────────────────────────
# Taxonomy block — what we hand Gemini
# ─────────────────────────────────────────────────────────────────────
def _load_taxonomy_block(cur) -> tuple[str, set[str]]:
    """Returns (prompt_block, valid_slug_set). Used to constrain Gemini's
    output: we tell the model exactly which slugs are legal and reject
    anything else after parsing."""
    cur.execute("""
        SELECT t.slug, t.name, t.description, w.name AS wave_name
          FROM themes_v2 t
          LEFT JOIN waves_v2 w ON w.slug = t.wave_slug
         ORDER BY w.sort_order NULLS LAST, t.sort_order NULLS LAST, t.slug
    """)
    rows = cur.fetchall()
    valid = {r["slug"] for r in rows}
    lines: list[str] = []
    for r in rows:
        desc = (r["description"] or "").strip()
        if len(desc) > 140:
            desc = desc[:140] + "…"
        wave = (r["wave_name"] or "—")
        lines.append(f"  - {r['slug']:32s}  ({wave}) — {r['name']}: {desc}")
    return "\n".join(lines), valid


# ─────────────────────────────────────────────────────────────────────
# Stock + filing lookups
# ─────────────────────────────────────────────────────────────────────
def _resolve_stock(cur, symbol_or_sid: str) -> Optional[dict]:
    needle = (symbol_or_sid or "").strip()
    if not needle:
        return None
    cur.execute("""
        SELECT security_id, symbol, company_name, sector, industry
          FROM stock_universe
         WHERE security_id = %s
            OR upper(symbol) = upper(%s)
         LIMIT 1
    """, (needle, needle))
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_recent_filings(
    cur,
    security_id: str,
    *,
    limit: int = 1,
) -> list[dict]:
    """Most recent CONCALL_TRANSCRIPT (then INVESTOR_PRESENTATION as
    fallback) for the security. Limit applies to the combined ranked
    list — typical caller wants 1 row."""
    cur.execute("""
        WITH ranked AS (
            SELECT id, security_id, symbol, filing_type, period,
                   filing_date, pdf_url,
                   CASE filing_type
                       WHEN 'CONCALL_TRANSCRIPT' THEN 1
                       WHEN 'INVESTOR_PRESENTATION' THEN 2
                       ELSE 9
                   END AS pref
              FROM filings
             WHERE security_id = %s
               AND filing_type = ANY(%s)
               AND pdf_url IS NOT NULL AND pdf_url <> ''
        )
        SELECT *
          FROM ranked
         ORDER BY pref ASC, filing_date DESC NULLS LAST, id DESC
         LIMIT %s
    """, (security_id, list(PREFERRED_FILING_TYPES), limit))
    return [dict(r) for r in cur.fetchall()]


def _has_fresh_understanding(
    cur, security_id: str, period_end_date: date, *, grace_days: int
) -> bool:
    """True if we already have a concall_understanding_v2 row for the
    same period that's recent enough we shouldn't redo it."""
    cur.execute("""
        SELECT generated_at
          FROM concall_understanding_v2
         WHERE security_id = %s
           AND period_end_date = %s
         LIMIT 1
    """, (security_id, period_end_date))
    row = cur.fetchone()
    if not row:
        return False
    gen_at: datetime = row["generated_at"]
    age = (datetime.utcnow() - gen_at.replace(tzinfo=None)).days
    return age <= grace_days


# ─────────────────────────────────────────────────────────────────────
# PDF fetch — public BSE / NSE URLs
# ─────────────────────────────────────────────────────────────────────
def _bse_url_variants(url: str) -> list[str]:
    """BSE's AttachLive bucket sweeps files into AttachHis after a few
    days; the original URL we have stored often 404s while the same
    GUID under AttachHis still resolves. Try the recorded URL first,
    then the swap. Order also has the corollary that for *very* recent
    filings the live URL is the only one that works."""
    if "bseindia.com" not in url:
        return [url]
    variants = [url]
    if "AttachLive" in url:
        variants.append(url.replace("AttachLive", "AttachHis"))
    elif "AttachHis" in url:
        variants.append(url.replace("AttachHis", "AttachLive"))
    return variants


def _fetch_pdf_bytes(url: str, *, timeout: int = 30) -> bytes:
    """HTTP GET the PDF.

    BSE's CDN always returns HTTP 200 — even for missing files it serves
    an HTML 404 page. We detect non-PDF bodies and fall back across
    AttachLive ↔ AttachHis. NSE filings tend to be more permissive but
    we use the same headers for consistency."""
    import requests

    is_bse = "bseindia.com" in url
    is_nse = "nseindia.com" in url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-IN,en;q=0.9",
        "Connection": "keep-alive",
    }
    if is_bse:
        headers["Referer"] = "https://www.bseindia.com/"
        headers["Origin"] = "https://www.bseindia.com"
    elif is_nse:
        headers["Referer"] = "https://www.nseindia.com/"
        headers["Origin"] = "https://www.nseindia.com"

    last_err: Optional[str] = None
    for candidate in _bse_url_variants(url):
        try:
            resp = requests.get(
                candidate, headers=headers, timeout=timeout,
                allow_redirects=True,
            )
            resp.raise_for_status()
            blob = resp.content
            if not blob.startswith(b"%PDF"):
                last_err = (
                    f"non-PDF content at {candidate} "
                    f"(content_type={resp.headers.get('content-type')}, "
                    f"first_bytes={blob[:8]!r})"
                )
                continue
            if len(blob) > MAX_PDF_BYTES:
                raise ValueError(
                    f"PDF at {candidate} is {len(blob)//1024}KB "
                    f"> limit {MAX_PDF_BYTES//1024}KB"
                )
            return blob
        except requests.RequestException as exc:
            last_err = f"http error at {candidate}: {exc}"
            continue

    raise ValueError(last_err or f"could not fetch PDF from {url}")


# ─────────────────────────────────────────────────────────────────────
# Gemini call — structured JSON response
# ─────────────────────────────────────────────────────────────────────
# Gemini's structured-output schema is a subset of OpenAPI 3.0 — no
# `additionalProperties`, no `oneOf`, no recursion. We use an array of
# {theme_slug, score} objects instead of a free-form object so the
# schema stays valid; sanitiser flattens it back to a dict.
_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "exposure": {
            "type": "array",
            "description": (
                "Array of {theme_slug, score} entries — one entry per "
                "theme that the company has any meaningful exposure to. "
                "score is 0..1 representing the approximate share of "
                "business or forward-looking pipeline tied to the theme."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "theme_slug": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["theme_slug", "score"],
            },
        },
        "themes_extracted": {
            "type": "array",
            "description": (
                "Theme slugs with score ≥ 0.10. Subset of exposure entries, "
                "ordered by score descending."
            ),
            "items": {"type": "string"},
        },
        "summary": {
            "type": "string",
            "description": "2-3 sentence plain-English thematic summary.",
        },
        "evidence_quotes": {
            "type": "array",
            "description": (
                "Direct quotes from the transcript that support the "
                "exposure scores. Max 6 total, ≥1 per theme above 0.20."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "theme_slug": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["theme_slug", "quote"],
            },
        },
        "confidence": {
            "type": "number",
            "description": (
                "Self-reported confidence 0..1. Drop low if the document "
                "isn't actually a concall, is cut off, or themes are "
                "very unclear."
            ),
        },
    },
    "required": ["exposure", "themes_extracted", "summary", "confidence"],
}


def _build_prompt(company_name: str, symbol: str, taxonomy_block: str) -> str:
    return f"""You are reading a quarterly earnings-call transcript (or investor
presentation) for {company_name} ({symbol}), a company listed on Indian
exchanges.

Your job: estimate the share of the business — including forward-looking
order book and growth commentary — that maps to each thematic bucket
below. Be conservative. If a theme is mentioned only in passing or as a
customer category (not a product / service line / order book), score
it 0 or very low.

Themes (use these exact slugs, do NOT invent new ones):
{taxonomy_block}

Output rules (enforced by JSON schema):
1. `exposure` — array of {{theme_slug, score}} where score is 0..1. Include
   one entry per theme the company has any meaningful exposure to.
   Roughly: 0.50 means half of the business or forward growth comes
   from this theme; 0.10 means meaningful but minor. Skip themes
   entirely (don't include 0-score entries) where there is no
   exposure at all.
2. `themes_extracted` — array of theme_slugs with score ≥ 0.10. Subset
   of exposure entries, ordered by score desc.
3. `summary` — 2–3 plain-English sentences capturing the thematic profile.
4. `evidence_quotes` — direct quotes (verbatim) from the transcript that
   support the high-exposure themes. Max 6 entries. Each tagged with the
   theme_slug it supports.
5. `confidence` — 0..1, your own self-assessment. If the document isn't
   actually a concall transcript or is unreadable, return all-zeros and
   confidence ≤ 0.2.

Important:
- Total `exposure` does NOT have to sum to 1. A diversified company can
  legitimately score 0.4 on multiple themes.
- Use ONLY the slugs listed. Anything else will be discarded.
- Don't be optimistic on AI/DC for companies that just *mention* AI as a
  buzzword. Score it only when it's a product line, an order, or a
  capacity expansion specifically for AI/DC customers.
"""


def _call_gemini(
    *,
    company_name: str,
    symbol: str,
    pdf_bytes: bytes,
    taxonomy_block: str,
    model: str = GEMINI_MODEL,
) -> dict:
    """Send the PDF + prompt to Gemini and parse the structured JSON
    response. Raises on any provider failure — caller decides whether
    to fall back."""
    api_key = os.getenv("api_key", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "Gemini API key not set (looked for env: api_key, GEMINI_API_KEY)"
        )

    # Per-request client (fork-safety — see valvo_ai_v6/gateway.py for the
    # full explanation; bottom line is that gunicorn --preload + a long-
    # lived httpx pool blows up after fork).
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options={"timeout": GEMINI_TIMEOUT_MS},
    )

    pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
    prompt_part = types.Part.from_text(text=_build_prompt(
        company_name=company_name,
        symbol=symbol,
        taxonomy_block=taxonomy_block,
    ))

    config = types.GenerateContentConfig(
        temperature=0.15,
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=_RESPONSE_SCHEMA,
    )

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=[pdf_part, prompt_part])],
        config=config,
    )

    text = (response.text or "").strip()
    if not text:
        raise RuntimeError(f"Empty Gemini response for {symbol}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned non-JSON for {symbol}: {exc}; raw={text[:400]!r}") from exc

    # Token usage (best-effort)
    usage = getattr(response, "usage_metadata", None)
    parsed["__usage"] = {
        "input_tokens":  getattr(usage, "prompt_token_count", 0) or 0,
        "output_tokens": getattr(usage, "candidates_token_count", 0) or 0,
    }
    return parsed


# ─────────────────────────────────────────────────────────────────────
# Sanitisation — keep only known slugs, clamp values
# ─────────────────────────────────────────────────────────────────────
def _sanitise_response(parsed: dict, valid_slugs: set[str]) -> dict:
    exposure_in = parsed.get("exposure")
    themes_in = parsed.get("themes_extracted") or []
    quotes_in = parsed.get("evidence_quotes") or []

    # `exposure` comes back as an array of {theme_slug, score} objects
    # (Gemini schema constraint — see _RESPONSE_SCHEMA). Flatten to a
    # dict here. Tolerate the legacy dict shape too in case we ever
    # change provider.
    exposure: dict[str, float] = {}
    if isinstance(exposure_in, list):
        for entry in exposure_in:
            if not isinstance(entry, dict):
                continue
            slug = entry.get("theme_slug")
            if slug not in valid_slugs:
                continue
            try:
                v = float(entry.get("score") or 0)
            except (TypeError, ValueError):
                continue
            if v < 0:
                v = 0.0
            if v > 1:
                v = 1.0
            # Keep the higher score if a theme appears twice
            if v > exposure.get(slug, 0):
                exposure[slug] = round(v, 2)
    elif isinstance(exposure_in, dict):
        for slug, val in exposure_in.items():
            if slug not in valid_slugs:
                continue
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if v < 0:
                v = 0.0
            if v > 1:
                v = 1.0
            exposure[slug] = round(v, 2)

    # Themes must be (a) valid slugs, (b) above keep threshold
    themes: list[str] = []
    for slug in themes_in:
        if slug in valid_slugs and exposure.get(slug, 0) >= MIN_EXPOSURE_KEEP:
            if slug not in themes:
                themes.append(slug)
    # Belt-and-braces: if Gemini returned exposure ≥ 0.10 but didn't
    # repeat the slug in themes_extracted, include it.
    for slug, v in exposure.items():
        if v >= MIN_EXPOSURE_KEEP and slug not in themes:
            themes.append(slug)
    # Order by exposure desc
    themes.sort(key=lambda s: exposure.get(s, 0), reverse=True)

    quotes: list[dict] = []
    for q in quotes_in[:8]:
        if not isinstance(q, dict):
            continue
        slug = q.get("theme_slug")
        text = (q.get("quote") or "").strip()
        if slug in valid_slugs and text:
            quotes.append({"theme_slug": slug, "quote": text[:600]})
    quotes = quotes[:6]

    summary = (parsed.get("summary") or "").strip()
    summary = summary[:1200]

    try:
        confidence = float(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    confidence = round(confidence, 4)

    return {
        "exposure": exposure,
        "themes_extracted": themes,
        "summary": summary,
        "evidence_quotes": quotes,
        "confidence": confidence,
        "usage": parsed.get("__usage", {}),
    }


# ─────────────────────────────────────────────────────────────────────
# Period derivation from filing_date — period column on filings is null
# ─────────────────────────────────────────────────────────────────────
def _derive_period(filing_date: Optional[date]) -> tuple[Optional[str], Optional[date]]:
    """Quarterly filings on BSE typically come within 30-45 days of
    quarter end. Map a filing_date to the most plausible quarter end:

        filing in May–Jul → Q4 prior FY (Mar 31)
        filing in Aug–Oct → Q1 (Jun 30)
        filing in Nov–Jan → Q2 (Sep 30)
        filing in Feb–Apr → Q3 (Dec 31)

    Indian FY runs Apr→Mar; FYxx labels the year that ENDS in March.
    Returns (period_label, period_end_date) — either may be None if we
    can't derive."""
    if not filing_date:
        return (None, None)
    y = filing_date.year
    m = filing_date.month
    if 5 <= m <= 7:
        end = date(y, 3, 31)
        fy = y % 100  # FYxx ends in this March
        return (f"Q4FY{fy:02d}", end)
    if 8 <= m <= 10:
        end = date(y, 6, 30)
        fy = (y + 1) % 100
        return (f"Q1FY{fy:02d}", end)
    if 11 <= m <= 12:
        end = date(y, 9, 30)
        fy = (y + 1) % 100
        return (f"Q2FY{fy:02d}", end)
    if m == 1:
        end = date(y - 1, 9, 30)
        fy = y % 100
        return (f"Q2FY{fy:02d}", end)
    if 2 <= m <= 4:
        end = date(y - 1, 12, 31)
        fy = y % 100
        return (f"Q3FY{fy:02d}", end)
    return (None, None)


# ─────────────────────────────────────────────────────────────────────
# DB writes
# ─────────────────────────────────────────────────────────────────────
def _upsert_transcript(
    cur,
    *,
    security_id: str,
    symbol: str,
    period: Optional[str],
    period_end_date: Optional[date],
    filing_id: Optional[int],
    pdf_url: str,
    pdf_bytes: bytes,
) -> int:
    """Insert/upsert a concall_transcripts_v2 row. We don't extract text
    locally (Gemini handles the PDF directly); content_text stays NULL,
    content_chars=byte length so we have a sanity gauge.

    Returns the row id."""
    transcript_hash = hashlib.sha256(pdf_bytes).hexdigest()
    cur.execute("""
        INSERT INTO concall_transcripts_v2
            (security_id, symbol, period, period_end_date,
             filing_id, source_url, content_text, content_chars,
             transcript_hash)
        VALUES (%s, %s, %s, %s,
                %s, %s, NULL, %s,
                %s)
        ON CONFLICT (security_id, period, transcript_hash)
            DO UPDATE SET
                source_url      = EXCLUDED.source_url,
                period_end_date = COALESCE(EXCLUDED.period_end_date,
                                           concall_transcripts_v2.period_end_date),
                content_chars   = EXCLUDED.content_chars,
                fetched_at      = NOW()
        RETURNING id
    """, (security_id, symbol, period, period_end_date,
          filing_id, pdf_url, len(pdf_bytes),
          transcript_hash))
    return cur.fetchone()["id"]


def _upsert_understanding(
    cur,
    *,
    security_id: str,
    symbol: str,
    period: Optional[str],
    period_end_date: Optional[date],
    transcript_id: int,
    parsed: dict,
    raw_response: dict,
    model_used: str,
) -> None:
    """Upsert into concall_understanding_v2 keyed on (security_id, period).
    We store both the sanitised structured payload and the full raw
    response so we can re-derive things later without re-billing."""
    exposure_json = parsed["exposure"]
    themes_extracted = parsed["themes_extracted"]
    summary = parsed["summary"]
    confidence = parsed["confidence"]

    cur.execute("""
        INSERT INTO concall_understanding_v2
            (security_id, symbol, period, period_end_date, transcript_id,
             exposure_json, themes_extracted, summary,
             model_used, model_confidence, raw_response)
        VALUES (%s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s,
                %s, %s, %s::jsonb)
        ON CONFLICT (security_id, period)
            DO UPDATE SET
                period_end_date  = COALESCE(EXCLUDED.period_end_date,
                                            concall_understanding_v2.period_end_date),
                transcript_id    = EXCLUDED.transcript_id,
                exposure_json    = EXCLUDED.exposure_json,
                themes_extracted = EXCLUDED.themes_extracted,
                summary          = EXCLUDED.summary,
                model_used       = EXCLUDED.model_used,
                model_confidence = EXCLUDED.model_confidence,
                raw_response     = EXCLUDED.raw_response,
                generated_at     = NOW()
    """, (security_id, symbol, period, period_end_date, transcript_id,
          json.dumps(exposure_json), json.dumps(themes_extracted), summary,
          model_used, confidence, json.dumps(raw_response)))


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────
def ingest_symbol(
    symbol: str,
    *,
    max_filings: int = 1,
    force: bool = False,
    rerun_classifier: bool = True,
) -> dict:
    """Run the full pipeline for one symbol.

    Steps:
      1. Resolve symbol → (security_id, company_name).
      2. Fetch the latest CONCALL_TRANSCRIPT (or IP fallback). max_filings
         > 1 lets you capture the last N quarters in one go.
      3. For each filing: skip if a fresh row already exists (unless
         force), download the PDF, call Gemini, sanitise, upsert
         transcript + understanding.
      4. If anything new was written and rerun_classifier=True, kick off
         classify_all(symbols=[symbol]) so the new evidence flows.

    Returns a dict report:
        {
          symbol, security_id, ingested: [{period, filing_id,
                                            confidence, themes_extracted,
                                            primary_theme, exposure}],
          skipped: [...], errors: [...], reclassified: bool,
          tokens_in, tokens_out
        }
    """
    report: dict[str, Any] = {
        "symbol": symbol.upper(),
        "security_id": None,
        "ingested": [],
        "skipped": [],
        "errors": [],
        "reclassified": False,
        "tokens_in": 0,
        "tokens_out": 0,
    }

    conn = get_db()
    if not conn:
        report["errors"].append("db_unavailable")
        return report

    try:
        cur = conn.cursor()
        stock = _resolve_stock(cur, symbol)
        if not stock:
            report["errors"].append(f"symbol_not_found:{symbol}")
            return report
        report["security_id"] = stock["security_id"]

        taxonomy_block, valid_slugs = _load_taxonomy_block(cur)

        filings = _fetch_recent_filings(cur, stock["security_id"], limit=max_filings)
        if not filings:
            report["errors"].append("no_concall_filings")
            return report

        any_written = False
        for f in filings:
            period, period_end_date = _derive_period(f.get("filing_date"))

            if not force and period_end_date and _has_fresh_understanding(
                cur, stock["security_id"], period_end_date,
                grace_days=RECENT_REGEN_GRACE_DAYS,
            ):
                report["skipped"].append({
                    "filing_id": f["id"],
                    "period": period,
                    "reason": "fresh_row_exists",
                })
                continue

            try:
                pdf_bytes = _fetch_pdf_bytes(f["pdf_url"])
            except Exception as exc:
                report["errors"].append({
                    "filing_id": f["id"],
                    "stage": "fetch_pdf",
                    "detail": str(exc)[:300],
                })
                continue

            try:
                parsed_raw = _call_gemini(
                    company_name=stock.get("company_name") or stock["symbol"],
                    symbol=stock["symbol"],
                    pdf_bytes=pdf_bytes,
                    taxonomy_block=taxonomy_block,
                )
            except Exception as exc:
                report["errors"].append({
                    "filing_id": f["id"],
                    "stage": "gemini",
                    "detail": str(exc)[:300],
                })
                continue

            usage = parsed_raw.get("__usage") or {}
            report["tokens_in"] += int(usage.get("input_tokens") or 0)
            report["tokens_out"] += int(usage.get("output_tokens") or 0)

            sanitised = _sanitise_response(parsed_raw, valid_slugs)

            transcript_id = _upsert_transcript(
                cur,
                security_id=stock["security_id"],
                symbol=stock["symbol"],
                period=period,
                period_end_date=period_end_date,
                filing_id=f["id"],
                pdf_url=f["pdf_url"],
                pdf_bytes=pdf_bytes,
            )

            _upsert_understanding(
                cur,
                security_id=stock["security_id"],
                symbol=stock["symbol"],
                period=period,
                period_end_date=period_end_date,
                transcript_id=transcript_id,
                parsed=sanitised,
                raw_response={k: v for k, v in parsed_raw.items() if k != "__usage"},
                model_used=GEMINI_MODEL,
            )
            conn.commit()
            any_written = True

            report["ingested"].append({
                "filing_id": f["id"],
                "period": period,
                "period_end_date": str(period_end_date) if period_end_date else None,
                "confidence": sanitised["confidence"],
                "themes_extracted": sanitised["themes_extracted"],
                "primary_theme": (sanitised["themes_extracted"] or [None])[0],
                "exposure": sanitised["exposure"],
                "summary": sanitised["summary"],
            })

        # Re-classify so the new evidence flows
        if any_written and rerun_classifier:
            try:
                from services.classification_v2.classifier import classify_all
                classify_all(symbols=[stock["symbol"]])
                report["reclassified"] = True
            except Exception as exc:
                report["errors"].append({
                    "stage": "reclassify",
                    "detail": str(exc)[:300],
                })

        return report
    finally:
        close_db(conn)


def ingest_symbols(
    symbols: Iterable[str],
    *,
    max_filings: int = 1,
    force: bool = False,
    rerun_classifier: bool = True,
    sleep_between: float = 0.0,
) -> list[dict]:
    """Sequentially ingest a batch. Each symbol gets its own DB
    connection and Gemini call. We re-run the classifier per symbol so
    a partial batch failure still gives you partial wins."""
    out: list[dict] = []
    for s in symbols:
        if not s or not s.strip():
            continue
        rep = ingest_symbol(
            s.strip(),
            max_filings=max_filings,
            force=force,
            rerun_classifier=rerun_classifier,
        )
        out.append(rep)
        if sleep_between > 0:
            time.sleep(sleep_between)
    return out


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("usage: python -m services.classification_v2.concall_classifier "
              "<SYMBOL> [SYMBOL ...] [--force] [--no-rerun] [--n=N]")
        sys.exit(2)

    force = "--force" in args
    rerun = "--no-rerun" not in args
    max_n = 1
    syms: list[str] = []
    for a in args:
        if a == "--force" or a == "--no-rerun":
            continue
        if a.startswith("--n="):
            try:
                max_n = max(1, int(a.split("=", 1)[1]))
            except ValueError:
                pass
            continue
        syms.append(a.upper())

    if not syms:
        print("no symbols supplied")
        sys.exit(2)

    print(f"V2 concall ingestion: {len(syms)} symbols, max_filings={max_n}, "
          f"force={force}, rerun_classifier={rerun}, model={GEMINI_MODEL}")
    reports = ingest_symbols(
        syms,
        max_filings=max_n,
        force=force,
        rerun_classifier=rerun,
        sleep_between=0.5,
    )

    # Compact summary
    total_in = sum(r.get("tokens_in", 0) for r in reports)
    total_out = sum(r.get("tokens_out", 0) for r in reports)
    print(f"\n=== summary ===  tokens_in={total_in:,}  tokens_out={total_out:,}")
    for r in reports:
        sym = r.get("symbol")
        if r.get("errors"):
            print(f"  {sym:10s} ERROR  {r['errors']}")
            continue
        if not r.get("ingested"):
            print(f"  {sym:10s} skipped/no-op (skipped={len(r.get('skipped', []))})")
            continue
        for ing in r["ingested"]:
            themes = ", ".join(ing["themes_extracted"][:3]) or "—"
            print(f"  {sym:10s} {ing['period'] or '—':8s} conf={ing['confidence']:.2f}  "
                  f"themes=[{themes}]")
