"""
Theme Thesis — DeepSeek-generated "why is this stock in this theme"
SHORT (1 sentence) + LONG (3-4 sentence paragraph).

The Explore tab shows a THEME chip on every classified stock. By itself the
chip is just a label — there's no way for the user to tell *why* this
particular stock belongs to that theme, or *where in the theme's value chain*
it sits. This service generates two paired explanations:
  • SHORT — the crisp 1-sentence answer rendered in the "Why this Theme" card.
  • LONG  — the original 3-4 sentence paragraph kept for deeper-context use.

The answer to "why does this stock fit this theme" is a structural one
grounded in what the company actually does — segment mix, product lines,
classification signal — so we use DeepSeek (cheap, fast). Web-search
grounding lives only in `catalyst_service` where it's actually needed
(news + partnerships + project wins).

Generation is **lazy + cache-forever** per (stock, theme, prompt_version):
the first viewer pays the model round-trip (~3–6 s); every subsequent
viewer hits the `stock_theme_explanations` row in <100 ms.

Bumping `PROMPT_VERSION` invalidates all old rows without a destructive DROP —
old rows stay around as audit trail (including the legacy v1/v2 DeepSeek
rows and v3 Gemini rows from earlier iterations); lookups for the new
version miss and re-generate.
"""
from __future__ import annotations

import json
from typing import Optional

from database.database import get_db, close_db
from services.valvo_ai_v7.gateway import ModelGateway


# ════════════════════════════════════════════════════════════════════
#  Prompt — the thing that has to be world-class
# ════════════════════════════════════════════════════════════════════

# v4 → reverted from Gemini back to DeepSeek. The "why does this stock
# belong to this theme" answer is structural — grounded in what the
# company actually does (segment mix, product lines, classification
# signal) — and doesn't need real-time web search. Web grounding lives
# only in `catalyst_service` where the question is "what's driving this
# stock RIGHT NOW", which is news-time-sensitive.
# Same SHORT + LONG dual-output shape as v2/v3 so the parser is unchanged.
PROMPT_VERSION = 4

PROMPT_SYSTEM = (
    "You are a senior thematic equity analyst at a top India-focused fund. "
    "You write tight, grounded explanations of how individual companies fit "
    "into investment themes — the kind of sentence a portfolio manager skims "
    "in 5 seconds before a trade. You never speculate. You never use "
    "marketing language (\"well-positioned\", \"strong player\", \"key "
    "beneficiary\"). You name specific products, customers, and mechanisms "
    "wherever the source data supports it. You write about Indian listed "
    "companies in plain English with INR figures."
)


def _build_user_prompt(facts: dict) -> str:
    segment_lines = facts.get("segment_lines") or "  (no segment data on file)"
    return f"""Explain why **{facts['company_name']}** belongs in the **{facts['theme_name']}** theme.

You will produce TWO outputs in one response, delimited exactly as shown:

### SHORT
One sentence, max 25 words. The crispest possible answer to "why is this stock in this theme". Lead with what the company *does* that connects to the theme. Cite one concrete product/customer category if the FACTS support it. No filler.

### LONG
3 to 4 sentences, single paragraph, no bullets. Must answer in order:
1. **Where in the theme's value chain** does this company sit? (upstream raw material / midstream component / downstream integrator / enabling infrastructure / picks-and-shovels service). Pick exactly one.
2. **What specifically** does it provide that connects it to the theme? Be concrete — name the product, capability, or end-customer category. ("Industrial cooling and heat-recovery systems for hyperscale data centers" — not "infrastructure solutions".)
3. **Why does the theme's tailwind translate into demand for this stock?** Tie the macro driver of the theme to one observable fact about the company (segment share, recent order book, capex plan, management commentary).

Hard rules (apply to BOTH outputs):
- Every claim must be grounded in the FACTS block below. If a fact is missing, omit it — never invent product lines, customers, or numbers.
- Refer to the company by name (or short name), not "the company".
- No price/valuation/buy-sell language. No disclaimers.
- If the FACTS show only a weak / circumstantial connection to the theme, say so plainly ("exposure is indirect, via …") rather than overclaiming.

============================================================
THEME
------------------------------------------------------------
Name:        {facts['theme_name']}
Wave:        {facts['wave_name']}
Description: {facts['theme_description']}
Why this stock was tagged here:
  - Classifier source : {facts['classification_source']}
  - Matched signal    : "{facts['matched_term']}"
  - Exposure score    : {facts['exposure_score']}/100
  - Confidence        : {facts['confidence']}

============================================================
COMPANY FACTS - {facts['company_name']}
------------------------------------------------------------
Sector / Industry : {facts['sector']} / {facts['industry']}
Sub-sector (V2)   : {facts['sub_sector_name']}
Business descr.   : {facts['about_or_dash']}

Latest annual (FY{facts['fiscal_year']}):
  Revenue  : {facts['revenue_cr']} Cr
  Net prof : {facts['net_profit_cr']} Cr   OPM {facts['opm']}
  ROE {facts['roe']}   ROCE {facts['roce']}   D/E {facts['de']}

Top revenue segments (latest quarter, period {facts['segment_period']}):
{segment_lines}

Most-recent concall - management discussion ({facts['concall_period']}):
  Summary       : {facts['concall_summary_or_dash']}
  Themes flagged: {facts['concall_themes_or_dash']}

============================================================
Output exactly (no preamble, no quotes around the headers):

### SHORT
<one sentence>

### LONG
<3-4 sentence paragraph>"""


# ════════════════════════════════════════════════════════════════════
#  Cache lookup
# ════════════════════════════════════════════════════════════════════

def _fetch_cached(cur, security_id: str, theme_slug: str) -> Optional[dict]:
    cur.execute(
        """
        SELECT explanation, short_explanation, citations_json,
               model_used, generated_at, input_tokens, output_tokens
          FROM stock_theme_explanations
         WHERE security_id = %s
           AND theme_slug  = %s
           AND prompt_version = %s
         LIMIT 1
        """,
        (security_id, theme_slug, PROMPT_VERSION),
    )
    row = cur.fetchone()
    if not row:
        return None
    raw_cites = row.get("citations_json")
    try:
        citations = (
            raw_cites if isinstance(raw_cites, list)
            else (json.loads(raw_cites) if raw_cites else [])
        )
    except Exception:
        citations = []
    return {
        "explanation": row["explanation"],
        "short_explanation": row.get("short_explanation"),
        "citations": citations,
        "model_used": row["model_used"],
        "generated_at": row["generated_at"].isoformat() if row.get("generated_at") else None,
        "prompt_version": PROMPT_VERSION,
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "cached": True,
    }


# ════════════════════════════════════════════════════════════════════
#  Grounding payload — pull every relevant fact for the prompt
# ════════════════════════════════════════════════════════════════════

def _dash(v):
    """Render any None/empty value as a dash for prompt readability."""
    if v is None:
        return "-"
    if isinstance(v, str) and not v.strip():
        return "-"
    return v


def _fmt_num(v, dec: int = 1, suffix: str = ""):
    if v is None:
        return "-"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "-"
    if dec == 0:
        return f"{int(round(n)):,}{suffix}"
    return f"{n:.{dec}f}{suffix}"


def _gather_facts(cur, security_id: str, theme_slug: str) -> Optional[dict]:
    """Assemble every fact the prompt needs from the existing tables.

    Returns None if the stock or theme can't be resolved — the route turns
    that into a 404. We never call DeepSeek without a complete grounding
    block.
    """
    # ── Stock identity ──────────────────────────────────────────
    cur.execute(
        """
        SELECT security_id, symbol, company_name, sector, industry
          FROM stock_universe
         WHERE security_id = %s
         LIMIT 1
        """,
        (security_id,),
    )
    stock = cur.fetchone()
    if not stock:
        return None

    # ── Theme metadata ──────────────────────────────────────────
    cur.execute(
        """
        SELECT t.slug, t.name, t.description,
               t.parent_sector,
               w.slug AS wave_slug, w.name AS wave_name
          FROM themes_v2 t
          LEFT JOIN waves_v2 w ON w.slug = t.wave_slug
         WHERE t.slug = %s
         LIMIT 1
        """,
        (theme_slug,),
    )
    theme = cur.fetchone()
    if not theme:
        return None

    # ── Stock-theme link (matched_term, exposure, confidence, source) ─
    cur.execute(
        """
        SELECT exposure_score, confidence, source, matched_term, is_primary
          FROM stock_themes_v2
         WHERE security_id = %s AND theme_slug = %s
         LIMIT 1
        """,
        (security_id, theme_slug),
    )
    link = cur.fetchone() or {}

    # ── Sub-sector (primary, V2) ────────────────────────────────
    cur.execute(
        """
        SELECT cs.name
          FROM stock_custom_sector_v2 scs
          JOIN custom_sectors_v2 cs ON cs.id = scs.custom_sector_id
         WHERE scs.security_id = %s
         ORDER BY scs.is_primary DESC, scs.confidence DESC NULLS LAST
         LIMIT 1
        """,
        (security_id,),
    )
    sub = cur.fetchone()

    # ── Business description ("about") ──────────────────────────
    cur.execute(
        """
        SELECT about
          FROM fundamentals_overview
         WHERE security_id = %s
         LIMIT 1
        """,
        (security_id,),
    )
    fo = cur.fetchone() or {}

    # ── Latest annual financials (consolidated preferred) ───────
    annual = None
    fiscal_year = None
    for consolidated in (True, False):
        cur.execute(
            """
            SELECT period_end_date, fiscal_year, revenue_cr, net_profit_cr,
                   opm_percent AS opm_pct, roe, roce, debt_to_equity
              FROM financials_annual
             WHERE security_id = %s AND is_consolidated = %s
             ORDER BY period_end_date DESC
             LIMIT 1
            """,
            (security_id, consolidated),
        )
        row = cur.fetchone()
        if row:
            annual = row
            fy_raw = row.get("fiscal_year")
            if fy_raw:
                # Strip a leading "FY" so the prompt template's own "FY"
                # prefix doesn't double up ("FYFY2024-25").
                fy_str = str(fy_raw).strip()
                if fy_str.upper().startswith("FY"):
                    fy_str = fy_str[2:].strip()
                fiscal_year = fy_str or None
            else:
                try:
                    fiscal_year = str(row["period_end_date"].year)
                except (AttributeError, TypeError):
                    fiscal_year = None
            break
    annual = annual or {}

    # ── Top revenue segments (latest period, dedup by period+name) ─
    cur.execute(
        """
        WITH ranked AS (
            SELECT segment_name,
                   segment_revenue_cr,
                   segment_revenue_pct,
                   period_end_date,
                   is_consolidated,
                   DENSE_RANK() OVER (ORDER BY period_end_date DESC) AS period_rank
              FROM segments_quarterly
             WHERE security_id = %s
               AND segment_name IS NOT NULL
               AND segment_name <> ''
        )
        SELECT segment_name, segment_revenue_cr, segment_revenue_pct,
               period_end_date, is_consolidated
          FROM ranked
         WHERE period_rank = 1
         ORDER BY (is_consolidated)::int DESC,
                  segment_revenue_pct DESC NULLS LAST
         LIMIT 5
        """,
        (security_id,),
    )
    raw_segs = cur.fetchall() or []
    # Deduplicate consolidated vs standalone by segment_name — keep
    # consolidated when both exist for the same name.
    seen_names: dict[str, dict] = {}
    for s in raw_segs:
        s = dict(s)
        nm = s.get("segment_name") or ""
        prev = seen_names.get(nm)
        if prev is None:
            seen_names[nm] = s
            continue
        if s.get("is_consolidated") and not prev.get("is_consolidated"):
            seen_names[nm] = s
    top_segments = list(seen_names.values())[:5]

    seg_period = "-"
    if top_segments:
        try:
            d = top_segments[0]["period_end_date"]
            seg_period = d.strftime("%b %Y") if d else "-"
        except Exception:
            seg_period = "-"

    seg_lines = []
    for s in top_segments:
        rev = s.get("segment_revenue_cr")
        pct = s.get("segment_revenue_pct")
        rev_str = _fmt_num(rev, dec=0, suffix=" Cr")
        pct_str = _fmt_num(pct, dec=1, suffix="%")
        seg_lines.append(f"  - {s.get('segment_name')}: {rev_str} ({pct_str} of revenue)")
    segment_lines = "\n".join(seg_lines) if seg_lines else None

    # ── Concall context (most recent) ──────────────────────────
    cur.execute(
        """
        SELECT period, period_end_date, summary, themes_extracted, model_used
          FROM concall_understanding_v2
         WHERE security_id = %s
         ORDER BY period_end_date DESC NULLS LAST, generated_at DESC
         LIMIT 1
        """,
        (security_id,),
    )
    concall = cur.fetchone() or {}

    concall_period = "-"
    concall_summary = "-"
    concall_themes = "-"
    if concall:
        period_val = concall.get("period")
        end_val = concall.get("period_end_date")
        concall_period = period_val or (end_val.strftime("%b %Y") if end_val else "-")
        if concall.get("summary"):
            # Truncate ferociously — full concall summaries can be 1k+ tokens.
            # 700 chars ≈ 175 tokens, plenty for the prompt.
            s = str(concall["summary"]).strip()
            concall_summary = (s[:700] + "…") if len(s) > 700 else s
        themes_raw = concall.get("themes_extracted")
        if themes_raw:
            try:
                if isinstance(themes_raw, (dict, list)):
                    parsed = themes_raw
                else:
                    parsed = json.loads(themes_raw)
                if isinstance(parsed, dict):
                    items = list(parsed.items())[:6]
                    concall_themes = ", ".join(f"{k} ({v})" for k, v in items) or "-"
                elif isinstance(parsed, list):
                    concall_themes = ", ".join(str(x) for x in parsed[:6]) or "-"
            except Exception:
                concall_themes = "-"

    # ── Pack the facts ─────────────────────────────────────────
    exposure_pct = link.get("exposure_score")
    if exposure_pct is not None:
        try:
            # exposure is stored 0.0–1.0; show as 0–100
            exposure_pct = round(float(exposure_pct) * 100)
        except (TypeError, ValueError):
            exposure_pct = "-"
    else:
        exposure_pct = "-"

    confidence_pct = link.get("confidence")
    if confidence_pct is not None:
        try:
            confidence_pct = f"{round(float(confidence_pct) * 100)}%"
        except (TypeError, ValueError):
            confidence_pct = "-"
    else:
        confidence_pct = "-"

    return {
        "company_name": stock.get("company_name") or stock.get("symbol"),
        "symbol": stock.get("symbol"),
        "sector": _dash(stock.get("sector")),
        "industry": _dash(stock.get("industry")),
        "sub_sector_name": _dash(sub.get("name") if sub else None),
        "about_or_dash": _dash(fo.get("about")),

        "theme_name": theme.get("name"),
        "theme_description": _dash(theme.get("description")),
        "wave_name": _dash(theme.get("wave_name")),
        "classification_source": _dash(link.get("source")),
        "matched_term": _dash(link.get("matched_term")) or "-",
        "exposure_score": exposure_pct,
        "confidence": confidence_pct,

        "fiscal_year": fiscal_year if fiscal_year is not None else "-",
        "revenue_cr": _fmt_num(annual.get("revenue_cr"), dec=0, suffix=""),
        "net_profit_cr": _fmt_num(annual.get("net_profit_cr"), dec=0, suffix=""),
        "opm": _fmt_num(annual.get("opm_pct"), dec=1, suffix="%"),
        "roe": _fmt_num(annual.get("roe"), dec=1, suffix="%"),
        "roce": _fmt_num(annual.get("roce"), dec=1, suffix="%"),
        "de": _fmt_num(annual.get("debt_to_equity"), dec=2, suffix=""),

        "segment_period": seg_period,
        "segment_lines": segment_lines,

        "concall_period": concall_period,
        "concall_summary_or_dash": concall_summary,
        "concall_themes_or_dash": concall_themes,
    }


# ════════════════════════════════════════════════════════════════════
#  DeepSeek call + persist
# ════════════════════════════════════════════════════════════════════

_LONG_MIN_LEN = 80
_LONG_MAX_LEN = 1400
_SHORT_MIN_LEN = 25
_SHORT_MAX_LEN = 280


def _collapse(s: str) -> str:
    """Trim quotes / preamble / collapse internal newlines into one line."""
    s = (s or "").strip()
    if len(s) >= 2 and s[0] in {'"', "'"} and s[-1] == s[0]:
        s = s[1:-1].strip()
    s = " ".join(line.strip() for line in s.splitlines() if line.strip())
    return s


def _split_short_long(raw: str) -> Optional[tuple[str, str]]:
    """Parse a `### SHORT … ### LONG …` response into the two blocks.

    DeepSeek occasionally varies the header (lowercase, missing #s). We
    accept any of `### SHORT`, `## SHORT`, `SHORT:`, `**SHORT**` as the
    delimiter on its own line. Returns (short, long) or None on parse
    failure (caller falls back to retry / give up).
    """
    import re

    if not raw:
        return None
    text = raw.strip()
    # Normalize header tokens to a canonical "<SHORT>" / "<LONG>" marker so
    # downstream split is trivial and tolerant of whitespace + casing.
    text = re.sub(
        r"(?im)^\s*(?:#{1,3}\s*|\*\*)?short(?:\*\*)?\s*[:\-]?\s*$",
        "<SHORT>",
        text,
    )
    text = re.sub(
        r"(?im)^\s*(?:#{1,3}\s*|\*\*)?long(?:\*\*)?\s*[:\-]?\s*$",
        "<LONG>",
        text,
    )

    if "<SHORT>" not in text or "<LONG>" not in text:
        return None
    after_short = text.split("<SHORT>", 1)[1]
    if "<LONG>" not in after_short:
        return None
    short_raw, long_raw = after_short.split("<LONG>", 1)

    short = _collapse(short_raw)
    long_ = _collapse(long_raw)
    if not short or not long_:
        return None
    return short, long_


_MODEL_LABEL = "deepseek-chat"


def _generate_via_deepseek(facts: dict) -> Optional[tuple[str, str, int, int]]:
    """One DeepSeek round-trip. Returns (short, long, input_tokens, output_tokens) or None.

    Theme thesis is structural — grounded in segment mix + matched signal +
    concall context — so DeepSeek without web search is the right tool.
    Citations stay in the schema (column exists) but are persisted as an
    empty list for theme rows; the SourcesTray on the frontend renders
    nothing when the array is empty.
    """
    gateway = ModelGateway()
    if not gateway.available():
        print("[theme_thesis] DEEPSEEK_API_KEY not set — refusing to generate")
        return None

    user_prompt = _build_user_prompt(facts)
    try:
        resp = gateway.create_message(
            model="deepseek-chat",
            max_tokens=500,
            system=PROMPT_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[],
        )
    except Exception as exc:
        print(f"[theme_thesis] DeepSeek call failed: {exc}")
        return None

    parsed = _split_short_long(resp.text or "")
    if parsed is None:
        print(f"[theme_thesis] could not parse SHORT/LONG blocks for {facts.get('symbol')}")
        return None
    short, long_ = parsed
    if not (_SHORT_MIN_LEN <= len(short) <= _SHORT_MAX_LEN):
        print(
            f"[theme_thesis] short sanity check failed for {facts.get('symbol')}: "
            f"len={len(short)} (allowed {_SHORT_MIN_LEN}-{_SHORT_MAX_LEN})"
        )
        return None
    if not (_LONG_MIN_LEN <= len(long_) <= _LONG_MAX_LEN):
        print(
            f"[theme_thesis] long sanity check failed for {facts.get('symbol')}: "
            f"len={len(long_)} (allowed {_LONG_MIN_LEN}-{_LONG_MAX_LEN})"
        )
        return None

    return (
        short,
        long_,
        int(getattr(resp, "input_tokens", 0) or 0),
        int(getattr(resp, "output_tokens", 0) or 0),
    )


def _persist(cur, security_id: str, theme_slug: str, short: str, long_: str,
             input_tokens: int, output_tokens: int) -> None:
    """Write the row, race-safe under concurrent first-views.
    citations_json column stays in the schema but is set to an empty
    array — DeepSeek doesn't return citations, and the frontend's
    SourcesTray hides itself when the list is empty."""
    cur.execute(
        """
        INSERT INTO stock_theme_explanations
            (security_id, theme_slug, explanation, short_explanation,
             citations_json, prompt_version, model_used,
             input_tokens, output_tokens)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
        ON CONFLICT (security_id, theme_slug, prompt_version) DO NOTHING
        """,
        (
            security_id, theme_slug, long_, short,
            json.dumps([]), PROMPT_VERSION, _MODEL_LABEL,
            input_tokens, output_tokens,
        ),
    )


# ════════════════════════════════════════════════════════════════════
#  Public API
# ════════════════════════════════════════════════════════════════════

def get_or_generate(security_id: str, theme_slug: str) -> Optional[dict]:
    """Return the cached or freshly-generated theme thesis for one (stock, theme).

    Caller contract:
      • None  → could not generate (no such stock/theme, or LLM failure).
                Route turns this into HTTP 503 / 404; frontend hides the section.
      • dict  → { explanation, short_explanation, citations, model_used,
                  generated_at, prompt_version, input_tokens, output_tokens,
                  cached }
    """
    if not security_id or not theme_slug:
        return None

    conn = get_db()
    try:
        cur = conn.cursor()

        # 1) Cache hit?
        cached = _fetch_cached(cur, security_id, theme_slug)
        if cached:
            return cached

        # 2) Build the grounding payload.
        facts = _gather_facts(cur, security_id, theme_slug)
        if facts is None:
            return None

        # 3) Call DeepSeek.
        gen = _generate_via_deepseek(facts)
        if gen is None:
            return None
        short, long_, in_tok, out_tok = gen

        # 4) Persist (race-safe).
        try:
            _persist(cur, security_id, theme_slug, short, long_, in_tok, out_tok)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            print(f"[theme_thesis] insert failed for {security_id}/{theme_slug}: {exc}")
            # Fall through — we still have a valid response to return; just
            # not cached. Next viewer will retry.

        return {
            "explanation": long_,
            "short_explanation": short,
            "citations": [],
            "model_used": _MODEL_LABEL,
            "generated_at": None,  # row write may have raced; not authoritative
            "prompt_version": PROMPT_VERSION,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cached": False,
        }
    finally:
        close_db(conn)
