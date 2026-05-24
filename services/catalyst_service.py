"""
Catalysts — Hybrid synthesis of "what's driving this stock right now".

Two specialized models, each doing what it's actually best at:

  1. INTERNAL bullets — DeepSeek synthesizes 2-3 catalysts from
     numerical / concall data we already have:
       • OPM expansion / contraction QoQ + YoY
       • Revenue / net-profit acceleration
       • Segment-mix shifts
       • Order-book mentions on the concall
       • Management guidance from the concall summary
     DeepSeek is cheap (~$0.0004 per call), fast, and these signals
     are fully present in our internal FACTS — no web search needed.

  2. NEWS bullets — Gemini 3.1 Pro (with native google_search
     grounding) hunts for 1-2 RECENT external catalysts:
       • Partnership announcements (e.g. Bloom Energy ↔ MTAR)
       • Major contract wins / order intake
       • Regulatory clearances
       • Capex / capacity announcements
     This is the catalyst class internal data fundamentally cannot
     surface. Each NEWS bullet carries a `source_url` (the citation
     that grounded it), and the global `citations` list includes
     every source the model consulted.

The two outputs are merged into a single ordered list capped at 4
items (news bullets first since they're time-sensitive and harder
to find, then internal bullets). Each call can fail independently —
if Gemini finds nothing or errors, the card still renders the
DeepSeek-only bullets, and vice versa.

Cache key: (security_id, concall_period, prompt_version). On read we
also enforce a 7-day TTL so news refreshes weekly even if the concall
hasn't changed. Writes UPSERT in place.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from database.database import get_db, close_db
from services.valvo_ai_v7.gateway import ModelGateway
from services.deep_research.gateway import GeminiProvider


# v3 → split the single Gemini call into a hybrid (DeepSeek for internal
# data + Gemini for web-search news). Same JSON output shape per bullet
# (title, detail, kind, source_url); the kind enum keeps the v2 additions
# (`partnership`, `news`) for the external-news class.
#
# v4 → harder-pushing news prompt (multi-search, JSON-with-url example,
# 0-result allowance only when search truly returned nothing) + a
# softer parser that falls back to result.citations when the model
# didn't inline source_url in JSON (Gemini grounding sometimes only
# attaches URLs in groundingMetadata, which was causing every news
# bullet to get dropped on strict require_source=True).
#
# v5 → Gemini 3.1 Pro thinking-budget fix. v4 silently failed because
# Gemini 3.x reasoning models spend `maxOutputTokens` on internal
# thinking BEFORE writing the response — a 900-token budget left ~50
# real tokens, which truncated the JSON to nothing and never let
# google_search fire. Now we pass `thinking_level="low"` (caps thinking
# at a small budget so output gets the lion's share) and bump
# `max_tokens` to 4000 to leave generous headroom for the JSON +
# citations on top.
PROMPT_VERSION = 5

# Stale-after window — news catalysts decay fast; even a stable stock
# can have a fresh partnership announcement land between concalls.
# 7 days balances freshness against Gemini search cost.
_TTL_DAYS = 7

# Per-call output caps. Internal slice asks DeepSeek for 2-3 bullets,
# news slice asks Gemini for 1-2 bullets. Total cap after merge is 4.
_MAX_INTERNAL = 3
_MAX_NEWS = 2
_MAX_TOTAL = 4

_DEEPSEEK_MODEL = "deepseek-chat"
_GEMINI_MODEL_ID = "gemini-3.1-pro-preview"
_GEMINI_LABEL = "gemini-3.1-pro"
# What we persist in `model_used` so admins can tell at a glance which
# slice produced this row's content (or whether one slice failed).
_MODEL_LABEL_BOTH    = "deepseek-chat+gemini-3.1-pro"
_MODEL_LABEL_DS_ONLY = "deepseek-chat"
_MODEL_LABEL_GM_ONLY = "gemini-3.1-pro"


# ── Prompt 1: INTERNAL bullets via DeepSeek ────────────────────────
PROMPT_SYSTEM_INTERNAL = (
    "You are a senior India-equity analyst. You synthesize 2-3 catalysts "
    "driving a listed stock right now FROM INTERNAL DATA ONLY — concall "
    "commentary, last quarter's print, segment growth, order-book mentions. "
    "You write tight, factual bullets — never marketing fluff, never "
    "speculation, never advice. Every bullet must cite a specific number, "
    "segment, or direct paraphrase from the FACTS block. You do NOT speculate "
    "about external news, partnerships, or contracts that aren't in the FACTS. "
    "If the FACTS are thin, output fewer bullets — never pad."
)


def _build_internal_prompt(facts: dict) -> str:
    quarters = facts.get("quarter_lines") or "  (no quarterly data)"
    segments = facts.get("segment_lines") or "  (no segment data)"
    return f"""Synthesize **2 to 3 internal-data catalysts** for **{facts['company_name']}** ({facts['symbol']}).

Output a JSON array of objects with this exact shape:
[
  {{
    "title":  "<3-6 word headline of the catalyst, title case>",
    "detail": "<one sentence with the supporting number, segment, or concall quote — max 28 words>",
    "kind":   "<one of: earnings | order_book | margin | segment | management_guidance | macro | risk>"
  }},
  ...
]

Hard rules:
- Lean on the FACTS block — every bullet MUST cite a number, segment name, or concall paraphrase that's actually present.
- {_MAX_INTERNAL} items max. Output 1 item if the FACTS are catastrophically thin.
- Order bullets by impact — most material catalyst first.
- `kind` MUST be one of the listed values exactly. (Do NOT use `partnership` or `news` — those are reserved for the news slice.)
- No buy/sell language, no price targets, no disclaimers, no markdown fence.
- Output the raw JSON array ONLY.

============================================================
COMPANY - {facts['company_name']}  ({facts['symbol']})
Sector / Industry : {facts['sector']} / {facts['industry']}
Business descr.   : {facts['about_or_dash']}

Last quarters (most recent first, period | revenue Cr | net profit Cr | OPM%):
{quarters}

Top revenue segments (latest period {facts['segment_period']}):
{segments}

Most-recent concall ({facts['concall_period']}):
  Themes flagged: {facts['concall_themes_or_dash']}
  Summary       : {facts['concall_summary_or_dash']}

============================================================
Output the JSON array now."""


# ── Prompt 2: NEWS bullets via Gemini 3.1 Pro (with google_search) ──
PROMPT_SYSTEM_NEWS = (
    "You are a senior India-equity analyst whose ONLY job is to surface "
    "EXTERNAL news catalysts a listed stock — partnership announcements, "
    "contract wins, regulatory clearances, capex / capacity moves, "
    "leadership changes, breaking news. These are the catalysts internal "
    "financial data alone fundamentally cannot surface, which is why "
    "you exist. You ALWAYS run multiple google_search queries before "
    "writing — never assume; always look. You write tight, factual "
    "bullets and embed the source URL inline in your JSON output. You "
    "NEVER invent news, but you also NEVER return an empty array if "
    "any plausibly-material event from the last 12 months exists in "
    "your search results — finding 1 bullet is better than 0."
)


def _build_news_prompt(facts: dict) -> str:
    return f"""Find **the 1 to 2 most material EXTERNAL news catalysts** for **{facts['company_name']}** ({facts['symbol']}), an India-listed stock in the {facts['sector']} sector.

Step 1 — RUN AT LEAST 2 google_search QUERIES. Suggested queries:
  • "{facts['company_name']} partnership 2025 OR 2026"
  • "{facts['company_name']} contract win"
  • "{facts['company_name']} order book"
  • "{facts['company_name']} capex new plant"
  • "{facts['symbol']} stock news 2026"
  • If the company has known anchor customers (e.g. Bloom Energy for MTAR, ISRO/DRDO for defence suppliers, hyperscalers for data-centre plays), include a query targeting that relationship explicitly.

Step 2 — PICK 1-2 most material results from the last ~12 months. Look for:
  • Partnership announcements / primary-supplier wins (named partners).
  • Major contract / order wins (named customers, contract sizes if reported).
  • Regulatory clearances (FDA / DGFT / RBI / SEBI / DGCA / EPCG / nuclear).
  • Capex / capacity / new-plant announcements.
  • Leadership changes that materially affect strategy.
  • Subsidy / incentive / scheme inclusions (PLI, government tenders).

Step 3 — OUTPUT a JSON array. Use this exact shape, one object per bullet:

```json
[
  {{
    "title":      "Bloom Energy Primary Supplier Win",
    "detail":     "MTAR confirmed as primary precision-component supplier for Bloom's 100MW data-centre fuel-cell rollout (announced Mar 2026).",
    "kind":       "partnership",
    "source_url": "https://www.bloomenergy.com/press/mtar-2026-supplier-announcement/"
  }}
]
```

Hard rules:
- Output 1 to {_MAX_NEWS} items. Output 0 ONLY if your searches truly returned nothing about this stock in the last 12 months — that should be rare for a listed company.
- `kind` MUST be `partnership` (named-customer / collab catalyst) or `news` (everything else external). NEVER `earnings`, `margin`, `segment`, `order_book`, etc — those are handled by a different model and would duplicate.
- `source_url` MUST be a real URL from your search results, embedded directly in the JSON object on the SAME line as the rest of the bullet. The URL field is non-optional. Never write `null` and never omit the field.
- Each `detail` must paraphrase a real search result. Never invent customers, contract sizes, or dates.
- Skip catalysts that are obvious from the company's basic business profile (e.g. "company makes X" is not news).
- Output the raw JSON array ONLY — no preamble text, no markdown fence around the JSON, no inline citation markers like [1].

For context (do NOT re-state these as bullets — they're already on the card via a separate model):
  Sector: {facts['sector']}
  Industry: {facts['industry']}
  Business: {facts['about_or_dash']}
  Last concall: {facts['concall_period']}

Output the JSON array now."""


# ════════════════════════════════════════════════════════════════════
#  Cache lookup
# ════════════════════════════════════════════════════════════════════

def _fetch_cached(cur, security_id: str, concall_period) -> Optional[dict]:
    # Postgres treats NULL = NULL as unknown; use IS NOT DISTINCT FROM so
    # the unique key matches a NULL concall_period (= stock with no
    # concall yet).
    #
    # Also enforce a 7-day TTL — news evolves between concalls so even
    # if the concall hasn't changed we want a fresh synthesis weekly.
    # The age check happens in SQL so a stale row won't be returned.
    cur.execute(
        """
        SELECT catalysts_json, citations_json, model_used, generated_at,
               input_tokens, output_tokens, concall_period
          FROM stock_catalysts
         WHERE security_id = %s
           AND concall_period IS NOT DISTINCT FROM %s
           AND prompt_version = %s
           AND generated_at >= NOW() - (%s || ' days')::interval
         LIMIT 1
        """,
        (security_id, concall_period, PROMPT_VERSION, str(_TTL_DAYS)),
    )
    row = cur.fetchone()
    if not row:
        return None
    raw = row["catalysts_json"]
    try:
        catalysts = raw if isinstance(raw, list) else json.loads(raw)
    except Exception:
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
        "catalysts": catalysts,
        "citations": citations,
        "concall_period": row["concall_period"].isoformat() if row.get("concall_period") else None,
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


def _fmt_period(period, period_end_date) -> str:
    if period and not str(period).strip().lower() == "quarterly":
        return str(period).strip()
    if period_end_date:
        try:
            return period_end_date.strftime("%b %Y")
        except Exception:
            pass
    return "-"


def _gather_facts(cur, security_id: str) -> Optional[tuple[dict, object]]:
    """Pull company identity + last 4 quarterly prints + top segments + most
    recent concall. Returns (facts, concall_period_for_cache_key) or None.
    """
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
        SELECT about
          FROM fundamentals_overview
         WHERE security_id = %s
         LIMIT 1
        """,
        (security_id,),
    )
    fo = cur.fetchone() or {}

    # Last 4 quarters — show consolidated when present, else standalone.
    cur.execute(
        """
        SELECT period, period_end_date, revenue_cr, net_profit_cr,
               opm_percent, is_consolidated
          FROM financials_quarterly
         WHERE security_id = %s
           AND (revenue_cr IS NOT NULL OR net_profit_cr IS NOT NULL)
         ORDER BY period_end_date DESC NULLS LAST
         LIMIT 8
        """,
        (security_id,),
    )
    raw_q = cur.fetchall() or []
    # Dedup by period_end_date — prefer consolidated.
    by_period: dict[object, dict] = {}
    for r in raw_q:
        r = dict(r)
        key = r.get("period_end_date")
        prev = by_period.get(key)
        if prev is None:
            by_period[key] = r
            continue
        if r.get("is_consolidated") and not prev.get("is_consolidated"):
            by_period[key] = r
    quarters = sorted(by_period.values(), key=lambda x: x.get("period_end_date") or 0, reverse=True)[:4]

    q_lines = []
    for q in quarters:
        period = _fmt_period(q.get("period"), q.get("period_end_date"))
        rev = _fmt_num(q.get("revenue_cr"), dec=0)
        npr = _fmt_num(q.get("net_profit_cr"), dec=0)
        opm = _fmt_num(q.get("opm_percent"), dec=1, suffix="%")
        q_lines.append(f"  - {period} | rev {rev} | np {npr} | opm {opm}")
    quarter_lines = "\n".join(q_lines) if q_lines else None

    # Top segments — same dedup as theme thesis.
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

    # Most recent concall — including the full summary text (this is the
    # main signal feeding catalyst synthesis).
    cur.execute(
        """
        SELECT period, period_end_date, summary, themes_extracted
          FROM concall_understanding_v2
         WHERE security_id = %s
         ORDER BY period_end_date DESC NULLS LAST, generated_at DESC
         LIMIT 1
        """,
        (security_id,),
    )
    concall = cur.fetchone() or {}
    concall_period_label = "-"
    concall_summary = "-"
    concall_themes = "-"
    concall_period_key = None  # used as cache key — date or None
    if concall:
        concall_period_label = _fmt_period(concall.get("period"), concall.get("period_end_date"))
        concall_period_key = concall.get("period_end_date")
        if concall.get("summary"):
            s = str(concall["summary"]).strip()
            # Allow more headroom than theme thesis since this is the
            # primary signal.
            concall_summary = (s[:1800] + "…") if len(s) > 1800 else s
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

    facts = {
        "company_name": stock.get("company_name") or stock.get("symbol"),
        "symbol": stock.get("symbol"),
        "sector": _dash(stock.get("sector")),
        "industry": _dash(stock.get("industry")),
        "about_or_dash": _dash(fo.get("about")),

        "quarter_lines": quarter_lines,
        "segment_period": seg_period,
        "segment_lines": segment_lines,

        "concall_period": concall_period_label,
        "concall_summary_or_dash": concall_summary,
        "concall_themes_or_dash": concall_themes,
    }
    return facts, concall_period_key


# ════════════════════════════════════════════════════════════════════
#  Generation
# ════════════════════════════════════════════════════════════════════

# Bullets the DeepSeek (internal) slice may emit. `partnership` + `news`
# are reserved for the Gemini slice and stripped if DeepSeek tries them.
_INTERNAL_KINDS = {
    "earnings", "order_book", "margin", "segment",
    "management_guidance", "macro", "risk",
}
# Bullets the Gemini (news) slice may emit. Anything else is dropped —
# we don't want Gemini repeating the internal earnings/margin signals.
_NEWS_KINDS = {"partnership", "news"}


def _extract_json_array(raw: str) -> Optional[list]:
    """Pull a JSON array out of a model's response. Tolerates fenced
    ```json … ``` blocks and a sentence of preamble. Returns the list
    (possibly empty) or None on parse failure."""
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        candidate = fence.group(1)
    else:
        match = re.search(r"\[\s*(?:\{.*\}\s*,?\s*)*\]", text, re.DOTALL)
        candidate = match.group(0) if match else text
    try:
        arr = json.loads(candidate)
    except Exception:
        return None
    return arr if isinstance(arr, list) else None


def _normalize_bullet(item: object, allowed_kinds: set,
                       require_source: bool) -> Optional[dict]:
    """Validate + normalize one bullet from either slice. Returns the
    cleaned dict or None if the bullet should be dropped."""
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    detail = str(item.get("detail") or "").strip()
    kind = str(item.get("kind") or "").strip().lower()
    if not title or not detail:
        return None
    if kind not in allowed_kinds:
        return None  # caller-defined kind whitelist
    if len(title) > 80:
        title = title[:80].rstrip() + "…"
    if len(detail) > 280:
        detail = detail[:280].rstrip() + "…"

    source_url = None
    raw_src = item.get("source_url")
    if isinstance(raw_src, str):
        su = raw_src.strip()
        if su.startswith(("http://", "https://")):
            source_url = su[:500]
    if require_source and not source_url:
        # News bullets MUST have a source URL. Drop the bullet if missing.
        return None

    return {
        "title": title,
        "detail": detail,
        "kind": kind,
        "source_url": source_url,
    }


def _generate_internal_via_deepseek(facts: dict) -> Optional[tuple[list[dict], int, int]]:
    """DeepSeek synthesizes 2-3 internal-data bullets from concall +
    quarters + segments. Cheap call; no web search.
    Returns (bullets, input_tokens, output_tokens) or None on failure."""
    gateway = ModelGateway()
    if not gateway.available():
        print("[catalysts/internal] DEEPSEEK_API_KEY not set — skipping internal slice")
        return None
    try:
        resp = gateway.create_message(
            model=_DEEPSEEK_MODEL,
            max_tokens=600,
            system=PROMPT_SYSTEM_INTERNAL,
            messages=[{"role": "user", "content": _build_internal_prompt(facts)}],
            tools=[],
        )
    except Exception as exc:
        print(f"[catalysts/internal] DeepSeek call failed: {exc}")
        return None

    arr = _extract_json_array(resp.text or "")
    if arr is None:
        print(f"[catalysts/internal] could not parse JSON for {facts.get('symbol')}: {(resp.text or '')[:200]}")
        return None

    bullets: list[dict] = []
    for item in arr[:_MAX_INTERNAL]:
        nb = _normalize_bullet(item, _INTERNAL_KINDS, require_source=False)
        if nb:
            bullets.append(nb)
    return (
        bullets,
        int(getattr(resp, "input_tokens", 0) or 0),
        int(getattr(resp, "output_tokens", 0) or 0),
    )


def _generate_news_via_gemini(facts: dict) -> Optional[tuple[list[dict], list[dict], int, int]]:
    """Gemini 3.1 Pro hunts for 1-2 external-news bullets via google_search.
    Returns (bullets, citations, input_tokens, output_tokens). Empty
    bullets list is a valid result (no material news found); None means
    the call failed."""
    provider = GeminiProvider()
    if not provider.available():
        print("[catalysts/news] GEMINI_API_KEY not set — skipping news slice")
        return None
    try:
        # max_tokens=4000 is generous on purpose — Gemini 3.1 Pro's
        # `thinkingLevel="low"` still consumes some hidden budget for
        # search planning + grounding metadata before the JSON gets
        # written. Anything below ~2000 risks truncation. The actual
        # billed output is whatever the model writes, not the cap.
        result = provider.complete(
            model_id=_GEMINI_MODEL_ID,
            system=PROMPT_SYSTEM_NEWS,
            user=_build_news_prompt(facts),
            max_tokens=4000,
            thinking_level="low",
        )
    except Exception as exc:
        print(f"[catalysts/news] Gemini call failed: {exc}")
        return None

    # Surface every grounding URL — useful both as per-bullet source
    # fallbacks below (when the model didn't inline them) and as the
    # global "Sources" tray rendered under the card.
    citations: list[dict] = []
    seen_urls: set[str] = set()
    for c in (result.citations or []):
        url = (c.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        citations.append({
            "title": (c.get("title") or "").strip()[:200],
            "url": url,
        })
        if len(citations) >= 8:
            break

    arr = _extract_json_array(result.text or "")
    if arr is None:
        print(
            f"[catalysts/news] could not parse JSON for {facts.get('symbol')}: "
            f"{(result.text or '')[:200]}"
        )
        return ([], citations,
                int(getattr(result, "input_tokens", 0) or 0),
                int(getattr(result, "output_tokens", 0) or 0))

    # Two-pass parse on the news slice:
    #   Pass 1 — accept bullets that already inline a source_url.
    #   Pass 2 — for bullets without a source_url, fall back to attaching
    #            an unused URL from result.citations (Gemini's grounding
    #            tool surfaces URLs in groundingMetadata even when the
    #            model didn't embed them in JSON; rather than drop the
    #            bullet entirely, we stamp on a citation so the user
    #            still sees a verifiable source).
    bullets: list[dict] = []
    used_urls: set[str] = set()
    pending_no_source: list[dict] = []
    for item in arr[:_MAX_NEWS]:
        nb_with = _normalize_bullet(item, _NEWS_KINDS, require_source=True)
        if nb_with:
            bullets.append(nb_with)
            if nb_with.get("source_url"):
                used_urls.add(nb_with["source_url"])
            continue
        nb_without = _normalize_bullet(item, _NEWS_KINDS, require_source=False)
        if nb_without:
            pending_no_source.append(nb_without)

    fallback_pool = [c["url"] for c in citations if c["url"] not in used_urls]
    for nb in pending_no_source:
        if not fallback_pool:
            # Truly no URL we can attach — drop the bullet rather than
            # ship a "news" item with no provenance.
            print(
                f"[catalysts/news] dropping un-sourced bullet for "
                f"{facts.get('symbol')}: {nb.get('title')!r}"
            )
            continue
        nb["source_url"] = fallback_pool.pop(0)
        bullets.append(nb)

    if not bullets and citations:
        # Prompt-parse worked but every bullet got dropped — log the
        # raw response head so we can tune the prompt next iteration.
        print(
            f"[catalysts/news] all bullets dropped for {facts.get('symbol')} "
            f"despite {len(citations)} citations; response head: "
            f"{(result.text or '')[:300]}"
        )

    return (
        bullets,
        citations,
        int(getattr(result, "input_tokens", 0) or 0),
        int(getattr(result, "output_tokens", 0) or 0),
    )


def _persist(cur, security_id: str, concall_period, catalysts: list[dict],
             citations: list[dict], model_label: str,
             input_tokens: int, output_tokens: int) -> None:
    """Race-safe write — uses UPSERT so a weekly refresh updates the row
    in place rather than colliding with the existing one. Without
    DO UPDATE the new synthesis would be discarded since the unique key
    on (security_id, concall_period, prompt_version) hasn't changed.

    `model_label` records which slice(s) actually fired — useful for
    debugging "why are there only DeepSeek bullets" investigations.
    `input_tokens` / `output_tokens` are summed across both slices."""
    cur.execute(
        """
        INSERT INTO stock_catalysts
            (security_id, concall_period, catalysts_json, citations_json,
             prompt_version, model_used, input_tokens, output_tokens,
             generated_at)
        VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, NOW())
        ON CONFLICT (security_id, concall_period, prompt_version)
        DO UPDATE SET
            catalysts_json = EXCLUDED.catalysts_json,
            citations_json = EXCLUDED.citations_json,
            model_used     = EXCLUDED.model_used,
            input_tokens   = EXCLUDED.input_tokens,
            output_tokens  = EXCLUDED.output_tokens,
            generated_at   = EXCLUDED.generated_at
        """,
        (
            security_id, concall_period,
            json.dumps(catalysts), json.dumps(citations or []),
            PROMPT_VERSION, model_label, input_tokens, output_tokens,
        ),
    )


# ════════════════════════════════════════════════════════════════════
#  Public API
# ════════════════════════════════════════════════════════════════════

def get_or_generate(security_id: str) -> Optional[dict]:
    """Return cached or freshly-synthesized catalyst list.

    Hybrid synthesis: DeepSeek for internal-data bullets (numerical
    signals from concall + last quarters + segments) + Gemini 3.1 Pro
    with google_search for 1-2 external news bullets. The two slices
    fail independently — if Gemini's web search returns nothing or
    errors, the card still renders the DeepSeek-only bullets, and vice
    versa. Returns None only when BOTH slices failed (or no such stock).
    """
    if not security_id:
        return None

    conn = get_db()
    try:
        cur = conn.cursor()

        # First: get the concall_period that grounds this synthesis. We
        # need it before the cache lookup so the unique key matches.
        gathered = _gather_facts(cur, security_id)
        if gathered is None:
            return None
        facts, concall_period = gathered

        cached = _fetch_cached(cur, security_id, concall_period)
        if cached:
            return cached

        # ── Slice 1: DeepSeek for internal-data bullets ──────────
        ds_gen = _generate_internal_via_deepseek(facts)
        internal_bullets: list[dict] = []
        ds_in = ds_out = 0
        if ds_gen is not None:
            internal_bullets, ds_in, ds_out = ds_gen

        # ── Slice 2: Gemini for external news bullets ────────────
        gm_gen = _generate_news_via_gemini(facts)
        news_bullets: list[dict] = []
        citations: list[dict] = []
        gm_in = gm_out = 0
        if gm_gen is not None:
            news_bullets, citations, gm_in, gm_out = gm_gen

        # ── Merge: news first (time-sensitive + harder to find), then
        # internal. Cap at 4 total. If both slices failed or both came
        # back empty, hide the card by returning None.
        catalysts = (news_bullets + internal_bullets)[:_MAX_TOTAL]
        if not catalysts:
            return None

        # Decide model_used label based on which slice contributed
        # actual content — useful for ops when investigating "why is
        # this card thin".
        had_ds = ds_gen is not None and len(internal_bullets) > 0
        had_gm = gm_gen is not None and len(news_bullets) > 0
        if had_ds and had_gm:
            model_label = _MODEL_LABEL_BOTH
        elif had_gm:
            model_label = _MODEL_LABEL_GM_ONLY
        else:
            # had_ds OR (gemini fired but found nothing — still ds-only
            # for content-attribution purposes).
            model_label = _MODEL_LABEL_DS_ONLY

        in_tok = ds_in + gm_in
        out_tok = ds_out + gm_out

        try:
            _persist(cur, security_id, concall_period, catalysts, citations,
                     model_label, in_tok, out_tok)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            print(f"[catalysts] insert failed for {security_id}: {exc}")

        return {
            "catalysts": catalysts,
            "citations": citations,
            "concall_period": concall_period.isoformat() if concall_period else None,
            "model_used": model_label,
            "generated_at": None,
            "prompt_version": PROMPT_VERSION,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cached": False,
        }
    finally:
        close_db(conn)
