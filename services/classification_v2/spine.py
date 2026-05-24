"""
V2 classification spine — the read-side counterpart of `classifier.py`.

`get_spine(symbol_or_sid)` returns one fully-assembled payload covering
everything the right-hand panel (`StockDetailDrawer`) needs to render:

    {
      'symbol', 'security_id', 'company_name', 'industry',
      'sector':    {'name', 'source', 'confidence'},
      'sub_sector':{'slug', 'name', 'parent_sector', 'confidence', 'source'},
      'primary_theme': {slug, name, wave_slug, wave_name, accent_color,
                        exposure, confidence, source, matched_term},
      'themes':       [{...}],         # up to 3, primary first
      'sub_sectors':  [{...}],         # up to 2, primary first
      'top_segments': [{name, pct, period, is_consolidated}, ...],
      'concall':      {period, summary, themes_extracted, exposure_json,
                       model_used, generated_at} | None,
      'evidence':     [{layer, value_slug, value_text, evidence_kind,
                        weight, confidence, matched_term, evidence_data,
                        applied_at}, ...],   # top-N for the "Why" panel
      'has_v2_classification': bool,
      'classified_at', 'updated_at'
    }

If a stock has not been classified by V2 yet, `has_v2_classification` is
False and `themes`, `sub_sectors`, etc. are empty — the API still returns
the raw V1 sector / industry so the panel can fall back gracefully.
"""
from __future__ import annotations

import json
from typing import Optional

from database.database import get_db, close_db
from services.segment_name_utils import clean_segment_name as _clean_segment_name


_TOP_SEGMENTS = 6      # most-recent-period top segments to surface
_TOP_EVIDENCE = 12     # rows from classification_evidence_v2 for the trail


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════
def _jsonb(val) -> dict | list:
    if val is None:
        return {}
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return {}


def _resolve_stock(cur, symbol_or_sid: str) -> Optional[dict]:
    """Look up the stock by symbol *or* security_id. Returns the full
    universe row or None."""
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


# ════════════════════════════════════════════════════════════════════
# Section loaders
# ════════════════════════════════════════════════════════════════════
def _load_classification(cur, sid: str) -> dict:
    """Pull the denormalised V2 row from `v_stock_classification_v2`."""
    cur.execute("""
        SELECT security_id, sector, sub_sector_slug, sub_sector_name,
               sub_sector_parent, primary_theme, primary_theme_name,
               primary_wave, primary_wave_name, primary_wave_accent,
               confidence, source, classified_at, updated_at
          FROM v_stock_classification_v2
         WHERE security_id = %s
    """, (sid,))
    row = cur.fetchone()
    return dict(row) if row else {}


def _load_themes(cur, sid: str) -> list[dict]:
    """All V2 themes for this stock with wave + parent-sector metadata,
    ordered so the primary theme is first."""
    cur.execute("""
        SELECT st.theme_slug                AS slug,
               t.name                       AS name,
               t.parent_sector              AS parent_sector,
               t.wave_slug                  AS wave_slug,
               w.name                       AS wave_name,
               w.accent_color               AS accent_color,
               st.exposure_score            AS exposure,
               st.confidence                AS confidence,
               st.source                    AS source,
               st.is_primary                AS is_primary,
               st.matched_term              AS matched_term,
               st.evidence_url              AS evidence_url,
               st.evidence_note             AS evidence_note,
               st.updated_at                AS updated_at
          FROM stock_themes_v2 st
          JOIN themes_v2 t ON t.slug = st.theme_slug
          LEFT JOIN waves_v2 w ON w.slug = t.wave_slug
         WHERE st.security_id = %s
         ORDER BY st.is_primary DESC,
                  st.exposure_score DESC NULLS LAST,
                  st.confidence DESC NULLS LAST,
                  t.slug
    """, (sid,))
    out = []
    for r in cur.fetchall():
        d = dict(r)
        if d.get("exposure") is not None:
            d["exposure"] = float(d["exposure"])
        if d.get("confidence") is not None:
            d["confidence"] = float(d["confidence"])
        out.append(d)
    return out


def _load_sub_sectors(cur, sid: str) -> list[dict]:
    """All V2 sub-sectors for this stock — primary first."""
    cur.execute("""
        SELECT cs.id                        AS id,
               cs.slug                      AS slug,
               cs.name                      AS name,
               cs.parent_sector             AS parent_sector,
               scs.confidence               AS confidence,
               scs.source                   AS source,
               scs.is_primary               AS is_primary,
               scs.matched_keyword          AS matched_term,
               scs.note                     AS note,
               scs.updated_at               AS updated_at
          FROM stock_custom_sector_v2 scs
          JOIN custom_sectors_v2 cs ON cs.id = scs.custom_sector_id
         WHERE scs.security_id = %s
         ORDER BY scs.is_primary DESC,
                  scs.confidence DESC NULLS LAST,
                  cs.slug
    """, (sid,))
    out = []
    for r in cur.fetchall():
        d = dict(r)
        if d.get("confidence") is not None:
            d["confidence"] = float(d["confidence"])
        out.append(d)
    return out


def _load_top_segments(cur, sid: str, limit: int = _TOP_SEGMENTS) -> list[dict]:
    """Latest-period top revenue segments — used by the panel to show
    "what drove this classification" without asking the user to expand
    the evidence trail."""
    cur.execute("""
        WITH ranked AS (
            SELECT segment_name,
                   segment_revenue_cr,
                   segment_revenue_pct,
                   period_end_date,
                   is_consolidated,
                   DENSE_RANK() OVER (
                       ORDER BY period_end_date DESC
                   ) AS period_rank
              FROM segments_quarterly
             WHERE security_id = %s
               AND segment_name IS NOT NULL
               AND segment_name <> ''
        )
        SELECT segment_name, segment_revenue_cr, segment_revenue_pct,
               period_end_date, is_consolidated
          FROM ranked
         WHERE period_rank = 1
         ORDER BY segment_revenue_pct DESC NULLS LAST
         LIMIT %s
    """, (sid, limit))
    out = []
    for r in cur.fetchall():
        d = dict(r)
        d["segment_name"] = _clean_segment_name(d.get("segment_name"))
        if d.get("segment_revenue_pct") is not None:
            d["segment_revenue_pct"] = float(d["segment_revenue_pct"])
        if d.get("segment_revenue_cr") is not None:
            d["segment_revenue_cr"] = float(d["segment_revenue_cr"])
        if d.get("period_end_date") is not None:
            d["period_end_date"] = str(d["period_end_date"])
        out.append(d)
    return out


def _load_concall(cur, sid: str) -> Optional[dict]:
    """Latest concall_understanding_v2 row, if Gemini has been run yet."""
    cur.execute("""
        SELECT period, period_end_date, summary,
               themes_extracted, exposure_json,
               model_used, model_confidence, generated_at
          FROM concall_understanding_v2
         WHERE security_id = %s
         ORDER BY period_end_date DESC NULLS LAST,
                  generated_at DESC
         LIMIT 1
    """, (sid,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d["themes_extracted"] = _jsonb(d.get("themes_extracted"))
    d["exposure_json"] = _jsonb(d.get("exposure_json"))
    if d.get("period_end_date") is not None:
        d["period_end_date"] = str(d["period_end_date"])
    if d.get("generated_at") is not None:
        d["generated_at"] = d["generated_at"].isoformat()
    if d.get("model_confidence") is not None:
        d["model_confidence"] = float(d["model_confidence"])
    return d


def _load_evidence(cur, sid: str, limit: int = _TOP_EVIDENCE) -> list[dict]:
    """Most-recent-and-highest-weighted evidence rows for the "Why" panel.

    We sort by (applied_at DESC, weight DESC, confidence DESC) so the
    panel shows the most recent classifier run first; older runs of the
    same decision live in the table but stay collapsed unless the user
    asks for the full history.
    """
    cur.execute("""
        SELECT layer, value_slug, value_text, evidence_kind,
               weight, confidence, matched_term,
               evidence_data, source_ref, author, applied_at
          FROM classification_evidence_v2
         WHERE security_id = %s
         ORDER BY applied_at DESC,
                  weight DESC NULLS LAST,
                  confidence DESC NULLS LAST
         LIMIT %s
    """, (sid, limit))
    out = []
    for r in cur.fetchall():
        d = dict(r)
        d["evidence_data"] = _jsonb(d.get("evidence_data"))
        if d.get("weight") is not None:
            d["weight"] = float(d["weight"])
        if d.get("confidence") is not None:
            d["confidence"] = float(d["confidence"])
        if d.get("applied_at") is not None:
            d["applied_at"] = d["applied_at"].isoformat()
        out.append(d)
    return out


# ════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════
def get_spine(symbol_or_sid: str) -> dict:
    """Assemble the full V2 classification spine for one stock.

    Accepts either a `symbol` (e.g. "STLTECH") or a `security_id` —
    whichever the caller has at hand. Returns a JSON-serialisable dict.
    """
    if not symbol_or_sid:
        return {"error": "symbol_or_sid is required"}

    conn = get_db()
    try:
        cur = conn.cursor()

        stock = _resolve_stock(cur, symbol_or_sid)
        if not stock:
            return {"error": f"stock not found: {symbol_or_sid}"}

        sid = stock["security_id"]
        sym = stock["symbol"]

        classification = _load_classification(cur, sid)
        themes = _load_themes(cur, sid)
        sub_sectors = _load_sub_sectors(cur, sid)
        segments = _load_top_segments(cur, sid)
        concall = _load_concall(cur, sid)
        evidence = _load_evidence(cur, sid)

        primary_theme = themes[0] if themes else None
        primary_sub = sub_sectors[0] if sub_sectors else None

        # Normalise the sector block — prefer V2, but always carry a
        # value so the panel never renders empty.
        sector_name = (
            classification.get("sector")
            or (primary_sub.get("parent_sector") if primary_sub else None)
            or (primary_theme.get("parent_sector") if primary_theme else None)
            or stock.get("sector")
            or "Uncategorized"
        )

        spine = {
            "symbol": sym,
            "security_id": sid,
            "company_name": stock.get("company_name"),
            "industry": stock.get("industry"),
            "v1_sector": stock.get("sector"),

            "has_v2_classification": bool(classification),

            "sector": {
                "name": sector_name,
                "source": classification.get("source"),
                "confidence": (
                    float(classification["confidence"])
                    if classification.get("confidence") is not None
                    else None
                ),
            },

            "sub_sector": (
                {
                    "slug": primary_sub["slug"],
                    "name": primary_sub["name"],
                    "parent_sector": primary_sub["parent_sector"],
                    "confidence": primary_sub.get("confidence"),
                    "source": primary_sub.get("source"),
                }
                if primary_sub else None
            ),

            "primary_theme": (
                {
                    "slug": primary_theme["slug"],
                    "name": primary_theme["name"],
                    "wave_slug": primary_theme.get("wave_slug"),
                    "wave_name": primary_theme.get("wave_name"),
                    "accent_color": primary_theme.get("accent_color"),
                    "exposure": primary_theme.get("exposure"),
                    "confidence": primary_theme.get("confidence"),
                    "source": primary_theme.get("source"),
                    "matched_term": primary_theme.get("matched_term"),
                }
                if primary_theme else None
            ),

            "themes": themes,
            "sub_sectors": sub_sectors,
            "top_segments": segments,
            "concall": concall,
            "evidence": evidence,

            "classified_at": (
                classification["classified_at"].isoformat()
                if classification.get("classified_at") else None
            ),
            "updated_at": (
                classification["updated_at"].isoformat()
                if classification.get("updated_at") else None
            ),
        }
        return spine
    finally:
        close_db(conn)


def get_spine_bulk(symbols: list[str]) -> dict[str, dict]:
    """Spines for many symbols at once (used by group-by-theme renderers
    that need the full evidence trail for several stocks). One round-trip
    per stock — cheap enough at watchlist scale (≤200) and avoids a
    multi-table monster SELECT."""
    out: dict[str, dict] = {}
    for s in symbols or []:
        try:
            out[s] = get_spine(s)
        except Exception as e:
            out[s] = {"error": str(e)}
    return out
