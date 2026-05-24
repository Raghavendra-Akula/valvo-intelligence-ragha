"""
Sector Thesis — DeepSeek-generated "why this stock fits its sector"
sentence.

The Explore strip used to lump sector identity into a chip + a long
narrative paragraph. The crisp Explore cards split that into a
dedicated "Why this Sector" tile that answers in one sentence: what
the company actually *does* that places it in the sector.

Like theme thesis, sector identity is structural — grounded in the
business description + segment mix — so we use DeepSeek (cheap, fast).
Web grounding lives only in `catalyst_service` for the news/projects
that web search is uniquely positioned to find.

Lazy generation, cache-forever per (security_id, sector, prompt_version).
"""
from __future__ import annotations

import json
from typing import Optional

from database.database import get_db, close_db
from services.valvo_ai_v7.gateway import ModelGateway


# ════════════════════════════════════════════════════════════════════
#  Prompt
# ════════════════════════════════════════════════════════════════════

# v3 → reverted from Gemini back to DeepSeek. Sector classification is
# structural; web search isn't the right tool. Same single-sentence
# shape as v1/v2 so the parser is unchanged.
PROMPT_VERSION = 3

PROMPT_SYSTEM = (
    "You are a senior India-equity analyst. You write tight, grounded "
    "single-sentence statements that explain why a listed company belongs "
    "to a particular sector — the kind of line a portfolio manager skims "
    "in 5 seconds. You never speculate, never use marketing language "
    "(\"well-positioned\", \"strong player\"), and always name the "
    "concrete product or end-customer that ties the company to the sector."
)


def _build_user_prompt(facts: dict) -> str:
    segment_lines = facts.get("segment_lines") or "  (no segment data on file)"
    return f"""Explain in ONE crisp sentence (max 30 words) why **{facts['company_name']}** is classified in the **{facts['sector']}** sector.

Hard rules:
- Lead with what the company DOES (the actual product or service), then connect that to the sector.
- Cite a concrete fact from the FACTS block: a product line, a customer category, or the dominant revenue segment with its share. ("MTAR makes precision components for Bloom Energy fuel-cell stacks and ISRO/Defence subsystems — ~52% of revenue.")
- No price/valuation/buy-sell language, no disclaimers, no marketing fluff.
- If the FACTS show only a tangential connection, say so plainly ("classified here mainly via …").
- Output the sentence ONLY — no preamble, no headers, no quotes.

============================================================
COMPANY FACTS - {facts['company_name']}
------------------------------------------------------------
Sector              : {facts['sector']}
Industry            : {facts['industry']}
Sub-sector (V2)     : {facts['sub_sector_name']}
Business descr.     : {facts['about_or_dash']}

Top revenue segments (latest quarter, period {facts['segment_period']}):
{segment_lines}

============================================================
Write the sentence now."""


# ════════════════════════════════════════════════════════════════════
#  Cache lookup
# ════════════════════════════════════════════════════════════════════

def _fetch_cached(cur, security_id: str, sector: str) -> Optional[dict]:
    cur.execute(
        """
        SELECT explanation, citations_json, model_used, generated_at,
               input_tokens, output_tokens
          FROM stock_sector_explanations
         WHERE security_id = %s
           AND sector      = %s
           AND prompt_version = %s
         LIMIT 1
        """,
        (security_id, sector, PROMPT_VERSION),
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
        "citations": citations,
        "model_used": row["model_used"],
        "generated_at": row["generated_at"].isoformat() if row.get("generated_at") else None,
        "prompt_version": PROMPT_VERSION,
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "cached": True,
    }


# ════════════════════════════════════════════════════════════════════
#  Grounding
# ════════════════════════════════════════════════════════════════════

def _dash(v):
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


def _gather_facts(cur, security_id: str, sector: str) -> Optional[dict]:
    """Pull the minimum facts the prompt needs — business description + top
    revenue segments. Returns None if the stock can't be resolved."""
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

    # Top segments — same pattern as theme thesis (latest period, dedup
    # consolidated/standalone, top 5 by % share).
    cur.execute(
        """
        WITH ranked AS (
            SELECT segment_name, segment_revenue_cr, segment_revenue_pct,
                   period_end_date, is_consolidated,
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
    seen: dict[str, dict] = {}
    for s in raw_segs:
        s = dict(s)
        nm = s.get("segment_name") or ""
        prev = seen.get(nm)
        if prev is None:
            seen[nm] = s
            continue
        if s.get("is_consolidated") and not prev.get("is_consolidated"):
            seen[nm] = s
    top_segments = list(seen.values())[:5]

    seg_period = "-"
    if top_segments:
        try:
            d = top_segments[0]["period_end_date"]
            seg_period = d.strftime("%b %Y") if d else "-"
        except Exception:
            seg_period = "-"

    seg_lines = []
    for s in top_segments:
        rev_str = _fmt_num(s.get("segment_revenue_cr"), dec=0, suffix=" Cr")
        pct_str = _fmt_num(s.get("segment_revenue_pct"), dec=1, suffix="%")
        seg_lines.append(f"  - {s.get('segment_name')}: {rev_str} ({pct_str} of revenue)")
    segment_lines = "\n".join(seg_lines) if seg_lines else None

    return {
        "company_name": stock.get("company_name") or stock.get("symbol"),
        "symbol": stock.get("symbol"),
        "sector": sector,
        "industry": _dash(stock.get("industry")),
        "sub_sector_name": _dash(sub.get("name") if sub else None),
        "about_or_dash": _dash(fo.get("about")),
        "segment_period": seg_period,
        "segment_lines": segment_lines,
    }


# ════════════════════════════════════════════════════════════════════
#  Generation
# ════════════════════════════════════════════════════════════════════

_MIN_LEN = 30
_MAX_LEN = 320

_MODEL_LABEL = "deepseek-chat"


def _strip_response(raw: str) -> str:
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] in {'"', "'"} and s[-1] == s[0]:
        s = s[1:-1].strip()
    s = " ".join(line.strip() for line in s.splitlines() if line.strip())
    return s


def _generate_via_deepseek(facts: dict) -> Optional[tuple[str, int, int]]:
    gateway = ModelGateway()
    if not gateway.available():
        print("[sector_thesis] DEEPSEEK_API_KEY not set — refusing to generate")
        return None
    try:
        resp = gateway.create_message(
            model="deepseek-chat",
            max_tokens=200,
            system=PROMPT_SYSTEM,
            messages=[{"role": "user", "content": _build_user_prompt(facts)}],
            tools=[],
        )
    except Exception as exc:
        print(f"[sector_thesis] DeepSeek call failed: {exc}")
        return None

    text = _strip_response(resp.text or "")
    if not (_MIN_LEN <= len(text) <= _MAX_LEN):
        print(
            f"[sector_thesis] sanity check failed for {facts.get('symbol')}: "
            f"len={len(text)} (allowed {_MIN_LEN}-{_MAX_LEN})"
        )
        return None
    return text, int(getattr(resp, "input_tokens", 0) or 0), int(getattr(resp, "output_tokens", 0) or 0)


def _persist(cur, security_id: str, sector: str, text: str,
             input_tokens: int, output_tokens: int) -> None:
    """citations_json is set to an empty array — DeepSeek doesn't return
    citations, and the frontend's SourcesTray hides itself when empty."""
    cur.execute(
        """
        INSERT INTO stock_sector_explanations
            (security_id, sector, explanation, citations_json,
             prompt_version, model_used, input_tokens, output_tokens)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
        ON CONFLICT (security_id, sector, prompt_version) DO NOTHING
        """,
        (
            security_id, sector, text, json.dumps([]),
            PROMPT_VERSION, _MODEL_LABEL,
            input_tokens, output_tokens,
        ),
    )


# ════════════════════════════════════════════════════════════════════
#  Public API
# ════════════════════════════════════════════════════════════════════

def get_or_generate(security_id: str, sector: str) -> Optional[dict]:
    if not security_id or not sector:
        return None

    conn = get_db()
    try:
        cur = conn.cursor()

        cached = _fetch_cached(cur, security_id, sector)
        if cached:
            return cached

        facts = _gather_facts(cur, security_id, sector)
        if facts is None:
            return None

        gen = _generate_via_deepseek(facts)
        if gen is None:
            return None
        text, in_tok, out_tok = gen

        try:
            _persist(cur, security_id, sector, text, in_tok, out_tok)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            print(f"[sector_thesis] insert failed for {security_id}/{sector}: {exc}")

        return {
            "explanation": text,
            "citations": [],
            "model_used": _MODEL_LABEL,
            "generated_at": None,
            "prompt_version": PROMPT_VERSION,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cached": False,
        }
    finally:
        close_db(conn)
