"""
V2 classifier — segment-revenue-first, evidence-backed.

Per-stock pipeline (in priority order, highest wins):

    1. Manual override (admin / approved review queue)        prio 7
    2. Concall understanding (Gemini-extracted exposure %)    prio 6
    3. Segment revenue match (segments_quarterly ≥ 15% pct)   prio 5
    4. Name override (curated symbol → slug)                  prio 4
    5. Web-verified backfill                                  prio 3
    6. Peer inference (k-NN by sector / segments — TODO)      prio 2
    7. Keyword fallback (industry/name text — discouraged)    prio 1

For sub-sectors and themes flagged `requires_segment = True` we *do not*
fall through to layers 4 / 7 — segment evidence (or concall) is required.
This is what stops HFCL / TEJASNET from being keyword-mistagged as AI-DC
plays without the revenue mix proving it.

Outputs:
    * stock_themes_v2 rows
    * stock_custom_sector_v2 rows
    * stock_sector_v2 single denormalised row (primary sector / sub-sector
      / primary theme + wave)
    * classification_evidence_v2 rows (one per signal — audit trail)
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Iterable, Optional

from database.database import get_db, close_db


# ════════════════════════════════════════════════════════════════════
# Tunables
# ════════════════════════════════════════════════════════════════════
MAX_THEMES_PER_STOCK = 3
MAX_SUB_SECTORS_PER_STOCK = 2
QUARTERS_TO_FETCH = 4
SEGMENT_MIN_PCT_REQUIRED = 15.0      # segments under this don't drive classification
SEGMENT_HIGH_CONF_PCT = 30.0
SEGMENT_VERY_HIGH_PCT = 50.0
CONCALL_MIN_EXPOSURE = 0.15
CONCALL_HIGH_EXPOSURE = 0.30


_SOURCE_PRIORITY = {
    "keyword_fallback": 1,
    "peer_inference":   2,
    "web_verified":     3,
    "name_override":    4,
    "segment_revenue":  5,
    "concall_understanding": 6,
    "manual_override":  7,
}


# ════════════════════════════════════════════════════════════════════
# Text helpers
# ════════════════════════════════════════════════════════════════════
_WS = re.compile(r"\s+")


def _norm(s: Optional[str]) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def _match_keywords(text: str, keywords: list[str]) -> Optional[tuple[str, int]]:
    """Return (matched_keyword, length) for the longest keyword that hits."""
    if not text or not keywords:
        return None
    hay = f" {_norm(text)} "
    best: Optional[tuple[str, int]] = None
    for kw in keywords:
        kw_n = _norm(kw)
        if not kw_n:
            continue
        needle = f" {kw_n} " if len(kw_n) <= 4 else kw_n
        if needle in hay:
            if best is None or len(kw_n) > best[1]:
                best = (kw_n, len(kw_n))
    return best


# ════════════════════════════════════════════════════════════════════
# Rule loaders (V2 tables)
# ════════════════════════════════════════════════════════════════════
def _jsonb(val) -> list:
    if val is None:
        return []
    return val if isinstance(val, list) else json.loads(val)


def _load_v2_rules(cur) -> dict:
    """Load all V2 rules in one shot.

    Returns:
        {
          'themes': [{slug, name, wave_slug, parent_sector, keywords,
                      segment_keywords, name_overrides, requires_segment}],
          'sub_sectors': [{id, slug, name, parent_sector, keywords,
                           segment_keywords, name_overrides, requires_segment}],
          'theme_override_map': {SYMBOL: [theme_slug, ...]},
          'sub_override_map':   {SYMBOL: [(sub_id, sub_slug), ...]},
          'wave_meta':          {slug: {name, accent_color}},
          'theme_meta':         {slug: {name, parent_sector, wave_slug}},
          'sub_meta':           {slug: {id, name, parent_sector}},
        }
    """
    cur.execute("""
        SELECT slug, name, wave_slug, parent_sector,
               keywords, segment_keywords, name_overrides
          FROM themes_v2
         WHERE COALESCE(is_active, true) = true
         ORDER BY slug
    """)
    themes_rows = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT id, slug, name, parent_sector,
               keywords, segment_keywords, name_overrides
          FROM custom_sectors_v2
         WHERE COALESCE(is_active, true) = true
         ORDER BY slug
    """)
    sub_rows = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT slug, name, accent_color FROM waves_v2")
    wave_meta = {r["slug"]: dict(r) for r in cur.fetchall()}

    # `requires_segment` lives only in the seed (Python side); read from
    # config so the SQL row doesn't need an extra column. If config can't
    # be imported, default to False everywhere.
    requires_seg_themes: dict[str, bool] = {}
    requires_seg_subs:   dict[str, bool] = {}
    try:
        from config.themes_seed_v2 import THEMES_SEED_V2
        for t in THEMES_SEED_V2:
            requires_seg_themes[t["slug"]] = bool(t.get("requires_segment"))
    except Exception:
        pass
    try:
        from config.custom_sectors_seed_v2 import CUSTOM_SECTORS_SEED_V2
        for s in CUSTOM_SECTORS_SEED_V2:
            requires_seg_subs[s["slug"]] = bool(s.get("requires_segment"))
    except Exception:
        pass

    themes: list[dict] = []
    theme_override_map: dict[str, list[str]] = defaultdict(list)
    theme_meta: dict[str, dict] = {}
    for r in themes_rows:
        t = {
            "slug": r["slug"],
            "name": r["name"],
            "wave_slug": r["wave_slug"],
            "parent_sector": r.get("parent_sector"),
            "keywords": _jsonb(r["keywords"]),
            "segment_keywords": _jsonb(r["segment_keywords"]),
            "name_overrides": _jsonb(r["name_overrides"]),
            "requires_segment": requires_seg_themes.get(r["slug"], False),
        }
        themes.append(t)
        for sym in t["name_overrides"]:
            theme_override_map[sym.upper()].append(t["slug"])
        theme_meta[t["slug"]] = {
            "name": t["name"],
            "parent_sector": t["parent_sector"],
            "wave_slug": t["wave_slug"],
        }

    sub_sectors: list[dict] = []
    sub_override_map: dict[str, list[tuple[int, str]]] = defaultdict(list)
    sub_meta: dict[str, dict] = {}
    for r in sub_rows:
        s = {
            "id": r["id"],
            "slug": r["slug"],
            "name": r["name"],
            "parent_sector": r["parent_sector"],
            "keywords": _jsonb(r["keywords"]),
            "segment_keywords": _jsonb(r["segment_keywords"]),
            "name_overrides": _jsonb(r["name_overrides"]),
            "requires_segment": requires_seg_subs.get(r["slug"], False),
        }
        sub_sectors.append(s)
        for sym in s["name_overrides"]:
            sub_override_map[sym.upper()].append((s["id"], s["slug"]))
        sub_meta[s["slug"]] = {
            "id": s["id"],
            "name": s["name"],
            "parent_sector": s["parent_sector"],
        }

    return {
        "themes": themes,
        "sub_sectors": sub_sectors,
        "theme_override_map": theme_override_map,
        "sub_override_map": sub_override_map,
        "wave_meta": wave_meta,
        "theme_meta": theme_meta,
        "sub_meta": sub_meta,
    }


# ════════════════════════════════════════════════════════════════════
# Per-stock segment + concall fetching
# ════════════════════════════════════════════════════════════════════
def _fetch_segments(cur, security_ids: list[str]) -> dict[str, list[dict]]:
    """Latest-period segments for each stock. Returns {sid: [seg, seg, ...]}."""
    by_sid: dict[str, list[dict]] = defaultdict(list)
    if not security_ids:
        return by_sid
    cur.execute("""
        WITH ranked AS (
            SELECT s.security_id, s.period_end_date,
                   s.segment_name, s.segment_revenue_cr,
                   s.segment_revenue_pct, s.is_consolidated,
                   DENSE_RANK() OVER (
                       PARTITION BY s.security_id
                       ORDER BY s.period_end_date DESC
                   ) AS period_rank
              FROM segments_quarterly s
             WHERE s.security_id = ANY(%s)
               AND s.segment_name IS NOT NULL
               AND s.segment_name <> ''
        )
        SELECT security_id, period_end_date, segment_name,
               segment_revenue_cr, segment_revenue_pct, is_consolidated
          FROM ranked
         WHERE period_rank = 1
         ORDER BY security_id, segment_revenue_pct DESC NULLS LAST
    """, (security_ids,))
    for r in cur.fetchall():
        by_sid[r["security_id"]].append(dict(r))
    return by_sid


def _fetch_concall_understanding(cur, security_ids: list[str]) -> dict[str, dict]:
    """Latest concall understanding per stock. Returns {sid: row|None}."""
    by_sid: dict[str, dict] = {}
    if not security_ids:
        return by_sid
    cur.execute("""
        WITH ranked AS (
            SELECT cu.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY security_id
                       ORDER BY period_end_date DESC NULLS LAST,
                                generated_at DESC
                   ) AS rn
              FROM concall_understanding_v2 cu
             WHERE security_id = ANY(%s)
        )
        SELECT * FROM ranked WHERE rn = 1
    """, (security_ids,))
    for r in cur.fetchall():
        by_sid[r["security_id"]] = dict(r)
    return by_sid


# ════════════════════════════════════════════════════════════════════
# Decision merge utility
# ════════════════════════════════════════════════════════════════════
def _merge(bucket: dict[str, dict], slug: str, new: dict) -> None:
    new["slug"] = slug
    cur = bucket.get(slug)
    if cur is None:
        bucket[slug] = new
        return
    new_p = _SOURCE_PRIORITY.get(new["source"], 0)
    cur_p = _SOURCE_PRIORITY.get(cur["source"], 0)
    if new_p > cur_p:
        cur["source"] = new["source"]
        cur["matched_term"] = new.get("matched_term", cur.get("matched_term"))
    cur["exposure_score"] = max(
        float(cur.get("exposure_score") or 0),
        float(new.get("exposure_score") or 0),
    )
    cur["confidence"] = max(
        float(cur.get("confidence") or 0),
        float(new.get("confidence") or 0),
    )
    cur.setdefault("evidence", []).extend(new.get("evidence", []))


# ════════════════════════════════════════════════════════════════════
# Per-stock classification
# ════════════════════════════════════════════════════════════════════
def classify_stock(
    *,
    security_id: str,
    symbol: str,
    company_name: str,
    sector: str,
    industry: str,
    segments: list[dict],
    concall: Optional[dict],
    rules: dict,
) -> dict:
    """Returns:
        {
          'themes':       [{slug, exposure, source, confidence, matched_term, evidence}],
          'sub_sectors':  [{id, slug, exposure, source, confidence, matched_term, evidence}],
          'primary_sector': str | None,
          'evidence_rows': [{layer, value_slug, value_text, evidence_kind, weight,
                             confidence, matched_term, evidence_data, source_ref, author}],
        }
    """
    sym_u = symbol.upper()
    theme_hits: dict[str, dict] = {}
    sub_hits: dict[str, dict] = {}
    evidence_rows: list[dict] = []

    # ── Layer A: name_override (themes) ─────────────────────────────
    for theme_slug in rules["theme_override_map"].get(sym_u, []):
        meta = rules["theme_meta"].get(theme_slug, {})
        # Respect requires_segment — name_override alone isn't enough
        # for high-noise themes.
        theme_def = next((t for t in rules["themes"] if t["slug"] == theme_slug), None)
        if theme_def and theme_def["requires_segment"]:
            continue
        _merge(theme_hits, theme_slug, {
            "exposure_score": 0.90,
            "source": "name_override",
            "confidence": 0.85,
            "matched_term": sym_u,
            "evidence": [{"kind": "name_override", "term": sym_u}],
        })
        evidence_rows.append({
            "layer": "theme", "value_slug": theme_slug,
            "value_text": meta.get("name"),
            "evidence_kind": "name_override",
            "weight": 0.90, "confidence": 0.85, "matched_term": sym_u,
            "evidence_data": {"symbol": sym_u},
            "source_ref": None, "author": "classifier",
        })

    # ── Layer A: name_override (sub-sectors) ────────────────────────
    for (sub_id, sub_slug) in rules["sub_override_map"].get(sym_u, []):
        sub_def = next((s for s in rules["sub_sectors"] if s["slug"] == sub_slug), None)
        if sub_def and sub_def["requires_segment"]:
            continue
        meta = rules["sub_meta"].get(sub_slug, {})
        _merge(sub_hits, sub_slug, {
            "id": sub_id,
            "exposure_score": 0.90,
            "source": "name_override",
            "confidence": 0.85,
            "matched_term": sym_u,
            "evidence": [{"kind": "name_override", "term": sym_u}],
        })
        evidence_rows.append({
            "layer": "sub_sector", "value_slug": sub_slug,
            "value_text": meta.get("name"),
            "evidence_kind": "name_override",
            "weight": 0.90, "confidence": 0.85, "matched_term": sym_u,
            "evidence_data": {"symbol": sym_u},
            "source_ref": None, "author": "classifier",
        })

    # ── Layer B: segment_revenue (themes + sub-sectors) ─────────────
    for seg in segments or []:
        try:
            pct = float(seg.get("segment_revenue_pct") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct < SEGMENT_MIN_PCT_REQUIRED:
            continue
        seg_name = seg.get("segment_name") or ""
        if not seg_name.strip():
            continue
        # Geo-only segments are noisy ("India", "Outside India") — skip
        if _norm(seg_name) in {"india", "outside india", "domestic", "international",
                                "rest of world", "row", "abroad", "exports"}:
            continue
        text = seg_name

        if pct >= SEGMENT_VERY_HIGH_PCT:
            conf = 0.95
        elif pct >= SEGMENT_HIGH_CONF_PCT:
            conf = 0.85
        else:
            conf = 0.70
        exposure = min(1.0, pct / 100.0)

        for t in rules["themes"]:
            keys = list(t["segment_keywords"]) + list(t["keywords"])
            m = _match_keywords(text, keys)
            if not m:
                continue
            _merge(theme_hits, t["slug"], {
                "exposure_score": exposure,
                "source": "segment_revenue",
                "confidence": conf,
                "matched_term": m[0],
                "evidence": [{"kind": "segment_revenue",
                               "segment": seg_name, "pct": pct, "term": m[0]}],
            })
            evidence_rows.append({
                "layer": "theme", "value_slug": t["slug"],
                "value_text": t["name"],
                "evidence_kind": "segment_revenue",
                "weight": exposure, "confidence": conf,
                "matched_term": m[0],
                "evidence_data": {
                    "segment_name": seg_name,
                    "segment_revenue_pct": pct,
                    "period_end_date": str(seg.get("period_end_date") or ""),
                },
                "source_ref": None, "author": "classifier",
            })

        for s in rules["sub_sectors"]:
            keys = list(s["segment_keywords"]) + list(s["keywords"])
            m = _match_keywords(text, keys)
            if not m:
                continue
            _merge(sub_hits, s["slug"], {
                "id": s["id"],
                "exposure_score": exposure,
                "source": "segment_revenue",
                "confidence": conf,
                "matched_term": m[0],
                "evidence": [{"kind": "segment_revenue",
                               "segment": seg_name, "pct": pct, "term": m[0]}],
            })
            evidence_rows.append({
                "layer": "sub_sector", "value_slug": s["slug"],
                "value_text": s["name"],
                "evidence_kind": "segment_revenue",
                "weight": exposure, "confidence": conf,
                "matched_term": m[0],
                "evidence_data": {
                    "segment_name": seg_name,
                    "segment_revenue_pct": pct,
                    "period_end_date": str(seg.get("period_end_date") or ""),
                },
                "source_ref": None, "author": "classifier",
            })

    # ── Layer C: concall_understanding ──────────────────────────────
    if concall and concall.get("themes_extracted"):
        themes_extracted = concall["themes_extracted"]
        if isinstance(themes_extracted, str):
            try:
                themes_extracted = json.loads(themes_extracted)
            except Exception:
                themes_extracted = []
        exposure_json = concall.get("exposure_json") or {}
        if isinstance(exposure_json, str):
            try:
                exposure_json = json.loads(exposure_json)
            except Exception:
                exposure_json = {}

        for theme_slug in themes_extracted or []:
            # Pull a numeric exposure if the model returned one
            ex = float(exposure_json.get(theme_slug, 0) or 0)
            if ex == 0:
                # Fall back to a constant for "mentioned without %"
                ex = 0.30
            if ex < CONCALL_MIN_EXPOSURE:
                continue
            conf = 0.90 if ex >= CONCALL_HIGH_EXPOSURE else 0.75
            meta = rules["theme_meta"].get(theme_slug)
            if not meta:
                continue
            _merge(theme_hits, theme_slug, {
                "exposure_score": min(1.0, ex),
                "source": "concall_understanding",
                "confidence": conf,
                "matched_term": "concall",
                "evidence": [{"kind": "concall_understanding",
                               "exposure": ex,
                               "period": concall.get("period")}],
            })
            evidence_rows.append({
                "layer": "theme", "value_slug": theme_slug,
                "value_text": meta.get("name"),
                "evidence_kind": "concall_understanding",
                "weight": min(1.0, ex), "confidence": conf,
                "matched_term": "concall",
                "evidence_data": {
                    "period": concall.get("period"),
                    "exposure": ex,
                    "model": concall.get("model_used"),
                },
                "source_ref": str(concall.get("transcript_id") or ""),
                "author": "gemini",
            })

    # ── Layer D: keyword fallback (only for non-requires_segment, no hits) ─
    if not theme_hits:
        fallback_text = " ".join([_norm(company_name), _norm(industry), _norm(sector)])
        for t in rules["themes"]:
            if t["requires_segment"]:
                continue
            m = _match_keywords(fallback_text, t["keywords"])
            if not m:
                continue
            _merge(theme_hits, t["slug"], {
                "exposure_score": 0.40,
                "source": "keyword_fallback",
                "confidence": 0.40,
                "matched_term": m[0],
                "evidence": [{"kind": "keyword_match", "term": m[0]}],
            })
            evidence_rows.append({
                "layer": "theme", "value_slug": t["slug"],
                "value_text": t["name"],
                "evidence_kind": "keyword_match",
                "weight": 0.40, "confidence": 0.40, "matched_term": m[0],
                "evidence_data": {"text": fallback_text[:200]},
                "source_ref": None, "author": "classifier",
            })

    if not sub_hits:
        fallback_text = " ".join([_norm(company_name), _norm(industry), _norm(sector)])
        for s in rules["sub_sectors"]:
            if s["requires_segment"]:
                continue
            m = _match_keywords(fallback_text, s["keywords"])
            if not m:
                continue
            _merge(sub_hits, s["slug"], {
                "id": s["id"],
                "exposure_score": 0.40,
                "source": "keyword_fallback",
                "confidence": 0.40,
                "matched_term": m[0],
                "evidence": [{"kind": "keyword_match", "term": m[0]}],
            })
            evidence_rows.append({
                "layer": "sub_sector", "value_slug": s["slug"],
                "value_text": s["name"],
                "evidence_kind": "keyword_match",
                "weight": 0.40, "confidence": 0.40, "matched_term": m[0],
                "evidence_data": {"text": fallback_text[:200]},
                "source_ref": None, "author": "classifier",
            })

    # ── Rank + cap ─────────────────────────────────────────────────
    ranked_themes = sorted(
        theme_hits.values(),
        key=lambda h: (
            -float(h.get("exposure_score") or 0),
            -_SOURCE_PRIORITY.get(h["source"], 0),
            h["slug"],
        ),
    )[:MAX_THEMES_PER_STOCK]
    for i, h in enumerate(ranked_themes):
        h["is_primary"] = (i == 0)

    ranked_subs = sorted(
        sub_hits.values(),
        key=lambda h: (
            -float(h.get("exposure_score") or 0),
            -_SOURCE_PRIORITY.get(h["source"], 0),
            h["slug"],
        ),
    )[:MAX_SUB_SECTORS_PER_STOCK]
    for i, h in enumerate(ranked_subs):
        h["is_primary"] = (i == 0)

    # ── Derive primary_sector ──────────────────────────────────────
    primary_sector: Optional[str] = None
    primary_theme = ranked_themes[0]["slug"] if ranked_themes else None
    primary_sub_slug = ranked_subs[0]["slug"] if ranked_subs else None
    if primary_theme:
        primary_sector = rules["theme_meta"].get(primary_theme, {}).get("parent_sector")
    if not primary_sector and primary_sub_slug:
        primary_sector = rules["sub_meta"].get(primary_sub_slug, {}).get("parent_sector")
    # Last resort: use the existing V1 / raw sector field
    if not primary_sector and sector:
        primary_sector = sector

    # Evidence row for the sector decision itself
    if primary_sector:
        evidence_rows.append({
            "layer": "sector", "value_slug": None,
            "value_text": primary_sector,
            "evidence_kind": (
                "concall_understanding" if (ranked_themes and ranked_themes[0]["source"] == "concall_understanding")
                else "segment_revenue" if (ranked_themes and ranked_themes[0]["source"] == "segment_revenue")
                else "name_override" if (ranked_themes and ranked_themes[0]["source"] == "name_override")
                else "raw_industry"
            ),
            "weight": 1.0,
            "confidence": float(ranked_themes[0]["confidence"]) if ranked_themes else 0.30,
            "matched_term": primary_theme or primary_sub_slug or sector,
            "evidence_data": {
                "derived_from_theme": primary_theme,
                "derived_from_sub_sector": primary_sub_slug,
            },
            "source_ref": None, "author": "classifier",
        })

    return {
        "themes": ranked_themes,
        "sub_sectors": ranked_subs,
        "primary_sector": primary_sector,
        "primary_theme": primary_theme,
        "primary_sub_slug": primary_sub_slug,
        "evidence_rows": evidence_rows,
    }


# ════════════════════════════════════════════════════════════════════
# DB writers
# ════════════════════════════════════════════════════════════════════
def _write_decision(cur, security_id: str, decision: dict, rules: dict) -> None:
    # Wipe non-manual rows (manual rows are protected — applied via review queue)
    cur.execute("""
        DELETE FROM stock_themes_v2
         WHERE security_id = %s AND COALESCE(source, '') <> 'manual_override'
    """, (security_id,))
    cur.execute("""
        DELETE FROM stock_custom_sector_v2
         WHERE security_id = %s AND COALESCE(source, '') <> 'manual_override'
    """, (security_id,))

    for t in decision["themes"]:
        cur.execute("""
            INSERT INTO stock_themes_v2
                (security_id, theme_slug, exposure_score, source,
                 is_primary, confidence, matched_term)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (security_id, theme_slug) DO UPDATE
                SET exposure_score = EXCLUDED.exposure_score,
                    source         = EXCLUDED.source,
                    is_primary     = EXCLUDED.is_primary,
                    confidence     = EXCLUDED.confidence,
                    matched_term   = EXCLUDED.matched_term,
                    updated_at     = NOW()
        """, (security_id, t["slug"], t["exposure_score"], t["source"],
              t["is_primary"], t["confidence"], t.get("matched_term")))

    for s in decision["sub_sectors"]:
        cur.execute("""
            INSERT INTO stock_custom_sector_v2
                (security_id, custom_sector_id, confidence, is_primary,
                 source, matched_keyword)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (security_id, custom_sector_id) DO UPDATE
                SET confidence       = EXCLUDED.confidence,
                    is_primary       = EXCLUDED.is_primary,
                    source           = EXCLUDED.source,
                    matched_keyword  = EXCLUDED.matched_keyword,
                    updated_at       = NOW()
        """, (security_id, s["id"], s["confidence"], s["is_primary"],
              s["source"], s.get("matched_term")))

    primary_theme = decision["primary_theme"]
    primary_wave = (rules["theme_meta"].get(primary_theme) or {}).get("wave_slug") if primary_theme else None
    primary_sub_slug = decision["primary_sub_slug"]
    primary_sector = decision["primary_sector"]
    overall_conf = (
        decision["themes"][0]["confidence"] if decision["themes"]
        else (decision["sub_sectors"][0]["confidence"] if decision["sub_sectors"] else 0.30)
    )
    overall_source = (
        decision["themes"][0]["source"] if decision["themes"]
        else (decision["sub_sectors"][0]["source"] if decision["sub_sectors"] else "raw_industry")
    )
    cur.execute("""
        INSERT INTO stock_sector_v2
            (security_id, sector, sub_sector_slug, primary_theme,
             primary_wave, confidence, source, classified_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (security_id) DO UPDATE
            SET sector          = EXCLUDED.sector,
                sub_sector_slug = EXCLUDED.sub_sector_slug,
                primary_theme   = EXCLUDED.primary_theme,
                primary_wave    = EXCLUDED.primary_wave,
                confidence      = EXCLUDED.confidence,
                source          = EXCLUDED.source,
                classified_at   = NOW(),
                updated_at      = NOW()
    """, (security_id, primary_sector or "Uncategorized", primary_sub_slug,
          primary_theme, primary_wave, overall_conf, overall_source))

    # Evidence rows — append-only audit trail. We don't dedupe on insert
    # because re-running classification produces a fresh trail per run.
    for ev in decision["evidence_rows"]:
        cur.execute("""
            INSERT INTO classification_evidence_v2
                (security_id, layer, value_slug, value_text,
                 evidence_kind, weight, confidence, matched_term,
                 evidence_data, source_ref, author)
            VALUES (%s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s::jsonb, %s, %s)
        """, (security_id, ev["layer"], ev.get("value_slug"), ev.get("value_text"),
              ev["evidence_kind"], ev.get("weight", 0), ev.get("confidence", 0),
              ev.get("matched_term"), json.dumps(ev.get("evidence_data") or {}),
              ev.get("source_ref"), ev.get("author")))


# ════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════
def classify_all(
    *,
    limit: Optional[int] = None,
    symbols: Optional[Iterable[str]] = None,
    dry_run: bool = False,
) -> dict:
    """Batch-classify the universe (or a symbol subset).

    Returns:
        {scanned, assigned, themes_written, sub_sectors_written,
         decisions: [...]}  — decisions populated only when dry_run.
    """
    conn = get_db()
    decisions_out: list[dict] = []
    scanned = assigned = themes_written = subs_written = 0
    try:
        cur = conn.cursor()
        rules = _load_v2_rules(cur)

        if symbols:
            syms = [s.strip().upper() for s in symbols if s and s.strip()]
            cur.execute("""
                SELECT security_id, symbol, company_name, sector, industry
                  FROM stock_universe
                 WHERE upper(symbol) = ANY(%s)
                 ORDER BY symbol
            """, (syms,))
        else:
            cur.execute("""
                SELECT security_id, symbol, company_name, sector, industry
                  FROM stock_universe
                 WHERE COALESCE(is_active, true) = true
                 ORDER BY symbol
            """)
        stocks = [dict(r) for r in cur.fetchall()]
        if limit:
            stocks = stocks[:limit]

        sids = [s["security_id"] for s in stocks]
        segments_by_sid = _fetch_segments(cur, sids)
        concall_by_sid = _fetch_concall_understanding(cur, sids)

        for stk in stocks:
            scanned += 1
            sid = stk["security_id"]
            sym = stk["symbol"]

            decision = classify_stock(
                security_id=sid,
                symbol=sym,
                company_name=stk.get("company_name") or "",
                sector=stk.get("sector") or "",
                industry=stk.get("industry") or "",
                segments=segments_by_sid.get(sid, []),
                concall=concall_by_sid.get(sid),
                rules=rules,
            )

            if decision["themes"] or decision["sub_sectors"]:
                assigned += 1
            themes_written += len(decision["themes"])
            subs_written += len(decision["sub_sectors"])

            if dry_run:
                decisions_out.append({
                    "security_id": sid,
                    "symbol": sym,
                    "primary_sector": decision["primary_sector"],
                    "primary_theme": decision["primary_theme"],
                    "primary_sub_slug": decision["primary_sub_slug"],
                    "themes": [{
                        "slug": t["slug"],
                        "exposure": t["exposure_score"],
                        "source": t["source"],
                        "confidence": t["confidence"],
                        "matched_term": t.get("matched_term"),
                        "is_primary": t["is_primary"],
                    } for t in decision["themes"]],
                    "sub_sectors": [{
                        "slug": s["slug"],
                        "exposure": s["exposure_score"],
                        "source": s["source"],
                        "confidence": s["confidence"],
                        "matched_term": s.get("matched_term"),
                        "is_primary": s["is_primary"],
                    } for s in decision["sub_sectors"]],
                })
                continue

            _write_decision(cur, sid, decision, rules)

        if not dry_run:
            conn.commit()
    finally:
        close_db(conn)

    return {
        "scanned": scanned,
        "assigned": assigned,
        "themes_written": themes_written,
        "sub_sectors_written": subs_written,
        "decisions": decisions_out if dry_run else None,
    }


def classify_one(symbol: str, dry_run: bool = False) -> dict:
    """Classify a single symbol — convenience wrapper."""
    return classify_all(symbols=[symbol], dry_run=dry_run)
