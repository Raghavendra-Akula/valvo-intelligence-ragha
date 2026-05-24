"""
Themes (Waves) — service layer.

Cross-cutting tailwind tagging that sits on top of the broad sector
reclassification. A stock has one sector but may carry multiple themes.

Responsibilities:
    * init_schema()         — run the DDL (idempotent)
    * seed_taxonomy()       — upsert the 6 waves + 22 themes from
                              config/themes_seed.py
    * _all_matches()        — multi-match keyword lookup (vs. custom_sectors
                              _best_match which returns only the longest hit)
    * classify_stock()      — decide themes for a single security
    * classify_all()        — batch-classify every active stock
    * get_matrix()          — wave × theme grid with counts + median return
                              for the matrix UI
    * get_constituents()    — stocks for a given theme (sorted by exposure)
    * get_stock_themes()    — themes for a given security

Mirrors the shape of services/custom_sectors_service.py.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Iterable, Optional

from database.database import get_db, close_db


_SCHEMA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "database",
    "create_themes_tables.sql",
)

# Source-priority when merging hits for the same (stock, theme) pair.
# Higher number wins.
_SOURCE_PRIORITY = {
    "fallback": 1,
    "segment_keyword": 2,
    "peer": 3,
    "name_override": 4,
    "web_verified": 5,
    "manual": 6,
}

# Max themes kept per stock after merging.
MAX_THEMES_PER_STOCK = 3


# ══════════════════════════════════════════════════════════════
# Schema + seeding
# ══════════════════════════════════════════════════════════════
def init_schema() -> None:
    """Create the themes tables if they don't exist. Idempotent."""
    with open(_SCHEMA_FILE, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
    finally:
        close_db(conn)


def seed_taxonomy() -> dict:
    """Upsert WAVES_SEED + THEMES_SEED from config/themes_seed.py.
    Returns {'waves': n, 'themes': n}."""
    from config.themes_seed import WAVES_SEED, THEMES_SEED

    init_schema()
    conn = get_db()
    waves_n = themes_n = 0
    try:
        cur = conn.cursor()
        for w in WAVES_SEED:
            cur.execute(
                """
                INSERT INTO waves_v2 (slug, name, description, accent_color, sort_order)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                    SET name = EXCLUDED.name,
                        description = EXCLUDED.description,
                        accent_color = EXCLUDED.accent_color,
                        sort_order = EXCLUDED.sort_order,
                        updated_at = NOW()
                """,
                (w["slug"], w["name"], w.get("description", ""),
                 w.get("accent_color"), w.get("sort_order", 99)),
            )
            waves_n += 1

        for t in THEMES_SEED:
            # Inherit accent from parent wave if theme doesn't define its own.
            parent_accent = next(
                (w.get("accent_color") for w in WAVES_SEED if w["slug"] == t["wave"]),
                None,
            )
            cur.execute(
                """
                INSERT INTO themes_v2 (slug, name, wave_slug, description,
                                    keywords, name_overrides, accent_color, sort_order)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                    SET name = EXCLUDED.name,
                        wave_slug = EXCLUDED.wave_slug,
                        description = EXCLUDED.description,
                        keywords = EXCLUDED.keywords,
                        name_overrides = EXCLUDED.name_overrides,
                        accent_color = EXCLUDED.accent_color,
                        sort_order = EXCLUDED.sort_order,
                        updated_at = NOW()
                """,
                (
                    t["slug"], t["name"], t["wave"], t.get("description", ""),
                    json.dumps(t.get("keywords", [])),
                    json.dumps(t.get("name_overrides", [])),
                    t.get("accent_color") or parent_accent,
                    t.get("sort_order", 99),
                ),
            )
            themes_n += 1
        conn.commit()
    finally:
        close_db(conn)
    return {"waves": waves_n, "themes": themes_n}


# ══════════════════════════════════════════════════════════════
# Classification helpers
# ══════════════════════════════════════════════════════════════
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def _all_matches(
    text: str,
    rules: list[tuple[str, list[str]]],
) -> list[tuple[str, str, int]]:
    """Return every (theme_slug, matched_kw, kw_len) where any keyword fires.
    Multi-match version of custom_sectors_service._best_match()."""
    hits: list[tuple[str, str, int]] = []
    if not text:
        return hits
    hay = f" {text} "
    for theme_slug, keywords in rules:
        seen_local: set[str] = set()
        for kw in keywords:
            kw_norm = _norm(kw)
            if not kw_norm or kw_norm in seen_local:
                continue
            needle = f" {kw_norm} " if len(kw_norm) <= 4 else kw_norm
            if needle in hay:
                hits.append((theme_slug, kw_norm, len(kw_norm)))
                seen_local.add(kw_norm)
                break  # one match per theme from this text
    return hits


def _load_rules(cur) -> tuple[list[tuple[str, list[str]]], dict[str, list[str]]]:
    """Load theme rules. Returns (keyword_rules, name_override_map).

    keyword_rules = [(theme_slug, [keywords]), ...]
    name_override_map = {upper_symbol: [theme_slug, ...]}
    """
    cur.execute(
        "SELECT slug, keywords, name_overrides FROM themes_v2 WHERE is_active = TRUE"
    )
    keyword_rules: list[tuple[str, list[str]]] = []
    override_map: dict[str, list[str]] = defaultdict(list)
    for r in cur.fetchall():
        kw_raw = r["keywords"]
        no_raw = r["name_overrides"]
        kw = kw_raw if isinstance(kw_raw, list) else json.loads(kw_raw or "[]")
        no = no_raw if isinstance(no_raw, list) else json.loads(no_raw or "[]")
        keyword_rules.append((r["slug"], kw))
        for sym in no:
            override_map[sym.upper()].append(r["slug"])
    return keyword_rules, override_map


# ══════════════════════════════════════════════════════════════
# Per-stock classification
# ══════════════════════════════════════════════════════════════
def classify_stock(
    security_id: str,
    symbol: str,
    company_name: str,
    sector: str,
    industry: str,
    segments: list[dict],
    rules: list[tuple[str, list[str]]],
    override_map: dict[str, list[str]],
) -> list[dict]:
    """Produce a list of theme-decisions for one stock. Shape:
        [{theme_slug, exposure_score, source, confidence, matched_term,
          is_primary}]

    Rules:
      1. Name-override list → hard-tag with source='name_override', exp=0.90
      2. For each segment with revenue_pct >= 20 that isn't a geo/placeholder,
         keyword-match → source='segment_keyword', exposure = pct/100 capped 1.0
      3. If no segment hits AND no overrides, fallback to
         name+industry+sector keyword match → source='fallback', exp≤0.5
      4. Merge duplicates keeping max exposure + highest-priority source.
      5. Cap at MAX_THEMES_PER_STOCK, sort by exposure desc.
      6. Highest-exposure theme marked is_primary=True.
    """
    hits: dict[str, dict] = {}

    # --- 1. Name overrides ------------------------------------------------
    for theme_slug in override_map.get(symbol.upper(), []):
        _merge_hit(hits, theme_slug, {
            "exposure_score": 0.90,
            "source": "name_override",
            "confidence": 0.85,
            "matched_term": symbol.upper(),
        })

    # --- 2. Segment-driven hits ------------------------------------------
    any_segment_used = False
    if segments:
        for seg in segments:
            pct = seg.get("segment_revenue_pct") or 0
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                pct = 0.0
            if pct < 20.0:
                continue
            seg_name = seg.get("segment_name") or ""
            if not seg_name.strip():
                continue
            text = _norm(seg_name)
            for theme_slug, matched_kw, _len in _all_matches(text, rules):
                any_segment_used = True
                exposure = min(1.0, pct / 100.0)
                _merge_hit(hits, theme_slug, {
                    "exposure_score": exposure,
                    "source": "segment_keyword",
                    "confidence": min(1.0, 0.4 + 0.05 * _len),
                    "matched_term": matched_kw,
                })

    # --- 3. Fallback on company name + industry + sector ------------------
    if not hits and not any_segment_used:
        fallback_text = _norm(f"{company_name} {industry} {sector}")
        for theme_slug, matched_kw, _len in _all_matches(fallback_text, rules):
            _merge_hit(hits, theme_slug, {
                "exposure_score": 0.40,
                "source": "fallback",
                "confidence": min(0.5, 0.3 + 0.02 * _len),
                "matched_term": matched_kw,
            })

    if not hits:
        return []

    # --- 4/5. Sort by exposure desc, then source priority, cap to 3 ------
    ranked = sorted(
        hits.values(),
        key=lambda h: (
            -float(h["exposure_score"] or 0),
            -_SOURCE_PRIORITY.get(h["source"], 0),
            h["theme_slug"],
        ),
    )[:MAX_THEMES_PER_STOCK]

    # --- 6. Primary flag -------------------------------------------------
    for idx, h in enumerate(ranked):
        h["is_primary"] = (idx == 0)
    return ranked


def _merge_hit(bucket: dict[str, dict], theme_slug: str, new: dict) -> None:
    """Merge a new hit into the bucket. Keep max exposure, highest-priority source."""
    cur = bucket.get(theme_slug)
    new["theme_slug"] = theme_slug
    if cur is None:
        bucket[theme_slug] = new
        return
    new_prio = _SOURCE_PRIORITY.get(new["source"], 0)
    cur_prio = _SOURCE_PRIORITY.get(cur["source"], 0)
    if new_prio > cur_prio:
        cur["source"] = new["source"]
    cur["exposure_score"] = max(
        float(cur.get("exposure_score") or 0),
        float(new.get("exposure_score") or 0),
    )
    cur["confidence"] = max(
        float(cur.get("confidence") or 0),
        float(new.get("confidence") or 0),
    )
    if not cur.get("matched_term"):
        cur["matched_term"] = new.get("matched_term")


# ══════════════════════════════════════════════════════════════
# Batch driver
# ══════════════════════════════════════════════════════════════
QUARTERS_TO_FETCH = 4


def classify_all(
    limit: Optional[int] = None,
    symbols: Optional[list[str]] = None,
    dry_run: bool = False,
    skip_manual: bool = True,
    skip_web_verified: bool = True,
    web_overrides: Optional[dict[str, list[dict]]] = None,
) -> dict:
    """Batch-classify every active stock.

    Parameters:
        limit            — stop after N stocks
        symbols          — only process the listed NSE symbols
        dry_run          — build decisions but don't write to DB
        skip_manual      — don't touch rows with source='manual'
        skip_web_verified — don't touch rows with source='web_verified'
        web_overrides    — {upper_symbol: [{theme_slug, exposure, confidence,
                                             evidence_url, note}, ...]}
                           hard-overrides the keyword pass

    Returns {'scanned': n, 'assigned': n, 'themes_written': n, 'stocks_no_theme': n,
             'decisions': [...]}  (decisions included only on dry_run)
    """
    conn = get_db()
    decisions: list[dict] = []
    scanned = 0
    assigned = 0
    themes_written = 0
    try:
        cur = conn.cursor()
        keyword_rules, override_map = _load_rules(cur)

        # Load stocks
        if symbols:
            syms = [s.strip().upper() for s in symbols if s.strip()]
            cur.execute(
                """SELECT security_id, symbol, company_name, sector, industry
                     FROM stock_universe
                    WHERE upper(symbol) = ANY(%s)
                 ORDER BY symbol""",
                (syms,),
            )
        else:
            cur.execute(
                """SELECT security_id, symbol, company_name, sector, industry
                     FROM stock_universe
                    WHERE COALESCE(is_active, true) = true
                 ORDER BY symbol"""
            )
        stocks = [dict(r) for r in cur.fetchall()]
        if limit:
            stocks = stocks[:limit]

        # Load protected rows (manual / web_verified)
        protected: dict[str, set[str]] = defaultdict(set)
        if skip_manual or skip_web_verified:
            src_list = []
            if skip_manual:
                src_list.append("manual")
            if skip_web_verified:
                src_list.append("web_verified")
            cur.execute(
                """SELECT security_id, theme_slug FROM stock_themes_v2
                    WHERE source = ANY(%s)""",
                (src_list,),
            )
            for r in cur.fetchall():
                protected[r["security_id"]].add(r["theme_slug"])

        # Pre-fetch the latest 4 quarters of segment rows (one query for all
        # target stocks — keeps latency low)
        sec_ids = [s["security_id"] for s in stocks]
        segments_by_sid: dict[str, list[dict]] = defaultdict(list)
        if sec_ids:
            cur.execute(
                """
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
                       segment_revenue_cr, segment_revenue_pct
                  FROM ranked
                 WHERE period_rank = 1
                 ORDER BY security_id, segment_revenue_cr DESC
                """,
                (sec_ids,),
            )
            for r in cur.fetchall():
                segments_by_sid[r["security_id"]].append(dict(r))

        # Classify each
        for stk in stocks:
            scanned += 1
            sid = stk["security_id"]
            sym = stk["symbol"]
            themes = classify_stock(
                security_id=sid,
                symbol=sym,
                company_name=stk.get("company_name") or "",
                sector=stk.get("sector") or "",
                industry=stk.get("industry") or "",
                segments=segments_by_sid.get(sid, []),
                rules=keyword_rules,
                override_map=override_map,
            )

            # Merge web_overrides on top, if provided
            if web_overrides and sym.upper() in web_overrides:
                wo_rows = web_overrides[sym.upper()]
                themes_by_slug = {t["theme_slug"]: t for t in themes}
                for wo in wo_rows:
                    slug = wo["theme_slug"]
                    themes_by_slug[slug] = {
                        "theme_slug": slug,
                        "exposure_score": float(wo.get("exposure") or 0.9),
                        "source": "web_verified",
                        "confidence": float(wo.get("confidence") or 0.9),
                        "matched_term": None,
                        "evidence_url": wo.get("evidence_url"),
                        "evidence_note": wo.get("note"),
                        "is_primary": False,
                    }
                ranked = sorted(
                    themes_by_slug.values(),
                    key=lambda h: (
                        -float(h["exposure_score"] or 0),
                        -_SOURCE_PRIORITY.get(h["source"], 0),
                    ),
                )[:MAX_THEMES_PER_STOCK]
                for idx, h in enumerate(ranked):
                    h["is_primary"] = (idx == 0)
                themes = ranked

            if not themes:
                decisions.append({"security_id": sid, "symbol": sym, "themes": []})
                continue

            assigned += 1
            decisions.append({"security_id": sid, "symbol": sym, "themes": themes})

            if dry_run:
                continue

            # Delete non-protected existing rows for this stock
            if protected[sid]:
                cur.execute(
                    """DELETE FROM stock_themes_v2
                        WHERE security_id = %s
                          AND theme_slug <> ALL(%s)""",
                    (sid, list(protected[sid])),
                )
            else:
                cur.execute("DELETE FROM stock_themes_v2 WHERE security_id = %s", (sid,))

            for t in themes:
                if t["theme_slug"] in protected[sid]:
                    continue
                cur.execute(
                    """
                    INSERT INTO stock_themes_v2
                      (security_id, theme_slug, exposure_score, source,
                       is_primary, confidence, matched_term,
                       evidence_url, evidence_note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (security_id, theme_slug) DO UPDATE
                      SET exposure_score = EXCLUDED.exposure_score,
                          source         = EXCLUDED.source,
                          is_primary     = EXCLUDED.is_primary,
                          confidence     = EXCLUDED.confidence,
                          matched_term   = EXCLUDED.matched_term,
                          evidence_url   = EXCLUDED.evidence_url,
                          evidence_note  = EXCLUDED.evidence_note,
                          updated_at     = NOW()
                    """,
                    (
                        sid, t["theme_slug"], t["exposure_score"], t["source"],
                        t["is_primary"], t.get("confidence"),
                        t.get("matched_term"),
                        t.get("evidence_url"),
                        t.get("evidence_note"),
                    ),
                )
                themes_written += 1

        if not dry_run:
            conn.commit()
    finally:
        close_db(conn)

    return {
        "scanned": scanned,
        "assigned": assigned,
        "stocks_no_theme": scanned - assigned,
        "themes_written": themes_written,
        "decisions": decisions if dry_run else [],
    }


# ══════════════════════════════════════════════════════════════
# Read helpers
# ══════════════════════════════════════════════════════════════
def list_taxonomy() -> dict:
    """Return {waves: [...], themes: [...]} for frontend hydration."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT slug, name, description, accent_color, sort_order
                 FROM waves_v2 WHERE is_active = TRUE
              ORDER BY sort_order, name"""
        )
        waves = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT slug, name, wave_slug, description, accent_color, sort_order
                 FROM themes_v2 WHERE is_active = TRUE
              ORDER BY sort_order, name"""
        )
        themes = [dict(r) for r in cur.fetchall()]
        return {"waves": waves, "themes": themes}
    finally:
        close_db(conn)


def get_matrix(days: int = 30) -> list[dict]:
    """Wave × Theme matrix with counts + median return over `days` trading days.
    Rows: one per (wave, theme). Used by the WavesMatrix UI."""
    days = max(1, min(int(days), 2000))
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            WITH per_stock AS (
                SELECT st.theme_slug, st.security_id,
                       COALESCE(sds.live_close, sds.prev_close) AS cur_close,
                       (SELECT close FROM candles_daily cd
                         WHERE cd.security_id = st.security_id
                           AND cd.date <= CURRENT_DATE - %s
                      ORDER BY cd.date DESC LIMIT 1) AS base_close
                  FROM stock_themes_v2 st
             LEFT JOIN stock_daily_summary sds ON sds.security_id = st.security_id
                 WHERE COALESCE(sds.is_etf, false) = false
            ),
            with_pct AS (
                SELECT theme_slug,
                       security_id,
                       100.0 * (cur_close - base_close) / NULLIF(base_close, 0) AS pct
                  FROM per_stock
                 WHERE base_close IS NOT NULL AND base_close > 0
            )
            SELECT t.slug   AS theme_slug,
                   t.name   AS theme_name,
                   w.slug   AS wave_slug,
                   w.name   AS wave_name,
                   w.accent_color AS wave_accent,
                   COUNT(DISTINCT st.security_id) AS constituents,
                   ROUND(AVG(wp.pct)::numeric, 2)  AS avg_pct,
                   ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY wp.pct)::numeric, 2)
                       AS median_pct
              FROM themes_v2 t
              JOIN waves_v2 w         ON w.slug = t.wave_slug
         LEFT JOIN stock_themes_v2 st ON st.theme_slug = t.slug
         LEFT JOIN with_pct wp        ON wp.theme_slug = t.slug
                                      AND wp.security_id = st.security_id
             WHERE t.is_active = TRUE
          GROUP BY t.slug, t.name, w.slug, w.name, w.accent_color, t.sort_order, w.sort_order
          ORDER BY w.sort_order, t.sort_order
            """,
            (days,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        close_db(conn)


_ENRICH_SQL = """
    , target_sids AS (
        SELECT DISTINCT security_id FROM base_rows
    ),
    latest_2 AS (
        SELECT cd.security_id, cd.close, cd.date,
               ROW_NUMBER() OVER (PARTITION BY cd.security_id ORDER BY cd.date DESC) AS rn
          FROM candles_daily cd
          JOIN target_sids t ON t.security_id = cd.security_id
         WHERE cd.date >= CURRENT_DATE - 10
    ),
    latest_prices AS (
        SELECT security_id,
               MAX(CASE WHEN rn = 1 THEN close END) AS cmp,
               ROUND(((MAX(CASE WHEN rn = 1 THEN close END) - MAX(CASE WHEN rn = 2 THEN close END))
                 / NULLIF(MAX(CASE WHEN rn = 2 THEN close END), 0) * 100)::numeric, 2) AS day_change
          FROM latest_2
         WHERE rn <= 2
         GROUP BY security_id
    ),
    week_ago AS (
        SELECT DISTINCT ON (cd.security_id)
               cd.security_id, cd.close AS week_ago_close
          FROM candles_daily cd
          JOIN target_sids t ON t.security_id = cd.security_id
         WHERE cd.date >= CURRENT_DATE - 14
           AND cd.date <= CURRENT_DATE - 4
      ORDER BY cd.security_id, cd.date DESC
    ),
    ma_data AS (
        SELECT security_id,
               AVG(close) FILTER (WHERE rn <= 5)  AS ma5,
               AVG(close) FILTER (WHERE rn <= 10) AS ma10,
               AVG(close) FILTER (WHERE rn <= 20) AS ma20,
               AVG(close) FILTER (WHERE rn <= 50) AS ma50
          FROM (
            SELECT cd.security_id, cd.close,
                   ROW_NUMBER() OVER (PARTITION BY cd.security_id ORDER BY cd.date DESC) AS rn
              FROM candles_daily cd
              JOIN target_sids t ON t.security_id = cd.security_id
             WHERE cd.date >= CURRENT_DATE - 90
          ) sub
         WHERE rn <= 50
         GROUP BY security_id
    )
"""


def _enrich_row(r: dict) -> dict:
    """Attach cmp/change/week_change/ma_trend/ma_above to a base row dict."""
    cmp = float(r["cmp"]) if r.get("cmp") is not None else None
    day_chg = float(r["day_change"]) if r.get("day_change") is not None else None
    wk_chg = float(r["week_change"]) if r.get("week_change") is not None else None
    ma5 = float(r["ma5"]) if r.get("ma5") is not None else None
    ma10 = float(r["ma10"]) if r.get("ma10") is not None else None
    ma20 = float(r["ma20"]) if r.get("ma20") is not None else None
    ma50 = float(r["ma50"]) if r.get("ma50") is not None else None
    ma_above = sum(1 for m in (ma5, ma10, ma20, ma50) if m and cmp and cmp > m)
    if cmp and ma5 and ma10 and ma20:
        if cmp > ma5 and cmp > ma10 and cmp > ma20:
            trend = "uptrend"
        elif cmp < ma5 and cmp < ma10 and cmp < ma20:
            trend = "downtrend"
        else:
            trend = "mixed"
    else:
        trend = "mixed"
    r["cmp"] = cmp
    r["change"] = day_chg
    r["pchange"] = day_chg
    r["week_change"] = wk_chg
    r["ma_trend"] = trend
    r["ma_above"] = ma_above
    # Strip raw MA columns from output.
    for k in ("ma5", "ma10", "ma20", "ma50", "day_change"):
        r.pop(k, None)
    return r


def get_constituents(theme_slug: str) -> list[dict]:
    """Stocks tagged to a theme, enriched with price + MA trend, sorted by exposure DESC."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            WITH base_rows AS (
                SELECT st.security_id, su.symbol, su.company_name,
                       COALESCE(su.valvo_sector, su.sector) AS sector,
                       st.exposure_score, st.source, st.confidence,
                       st.matched_term, st.evidence_url, st.evidence_note,
                       st.is_primary
                  FROM stock_themes_v2 st
                  JOIN stock_universe su ON su.security_id = st.security_id
                 WHERE st.theme_slug = %s
                   AND COALESCE(su.is_active, true) = true
            ){_ENRICH_SQL}
            SELECT b.*, lp.cmp, lp.day_change,
                   ROUND(((lp.cmp - wa.week_ago_close) / NULLIF(wa.week_ago_close, 0) * 100)::numeric, 2) AS week_change,
                   md.ma5, md.ma10, md.ma20, md.ma50
              FROM base_rows b
         LEFT JOIN latest_prices lp ON lp.security_id = b.security_id
         LEFT JOIN week_ago wa      ON wa.security_id = b.security_id
         LEFT JOIN ma_data md       ON md.security_id = b.security_id
          ORDER BY b.exposure_score DESC NULLS LAST, b.symbol
            """,
            (theme_slug,),
        )
        return [_enrich_row(dict(r)) for r in cur.fetchall()]
    finally:
        close_db(conn)


def get_wave_constituents(wave_slug: str) -> list[dict]:
    """Union of stocks across every theme of a wave, highest-exposure row per stock,
    enriched with price + MA trend."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            WITH base_rows AS (
                SELECT DISTINCT ON (st.security_id)
                       st.security_id, su.symbol, su.company_name,
                       COALESCE(su.valvo_sector, su.sector) AS sector,
                       st.theme_slug, t.name AS theme_name,
                       st.exposure_score, st.source, st.is_primary
                  FROM stock_themes_v2 st
                  JOIN themes_v2 t  ON t.slug = st.theme_slug
                  JOIN stock_universe su ON su.security_id = st.security_id
                 WHERE t.wave_slug = %s
                   AND COALESCE(su.is_active, true) = true
              ORDER BY st.security_id, st.exposure_score DESC NULLS LAST
            ){_ENRICH_SQL}
            SELECT b.*, lp.cmp, lp.day_change,
                   ROUND(((lp.cmp - wa.week_ago_close) / NULLIF(wa.week_ago_close, 0) * 100)::numeric, 2) AS week_change,
                   md.ma5, md.ma10, md.ma20, md.ma50
              FROM base_rows b
         LEFT JOIN latest_prices lp ON lp.security_id = b.security_id
         LEFT JOIN week_ago wa      ON wa.security_id = b.security_id
         LEFT JOIN ma_data md       ON md.security_id = b.security_id
          ORDER BY b.exposure_score DESC NULLS LAST, b.symbol
            """,
            (wave_slug,),
        )
        return [_enrich_row(dict(r)) for r in cur.fetchall()]
    finally:
        close_db(conn)


def get_composite(scope: str, slug: str, days: int = 252) -> list[dict]:
    """Equal-weight rebased composite daily series for a theme or a wave.

    scope = 'theme' | 'wave'. Every constituent starts at 100 on its first
    available bar inside the window; the composite is the daily mean of all
    rebased levels. Returned list is [{date, value, n}, ...] sorted ascending.
    """
    if scope not in ("theme", "wave"):
        raise ValueError("scope must be 'theme' or 'wave'")
    days = max(7, min(int(days), 3650))

    if scope == "theme":
        filter_sql = "WHERE st.theme_slug = %s"
    else:
        filter_sql = "JOIN themes_v2 t ON t.slug = st.theme_slug WHERE t.wave_slug = %s"

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            WITH constituents AS (
                SELECT DISTINCT st.security_id
                  FROM stock_themes_v2 st
                  {filter_sql}
            ),
            candles AS (
                SELECT cd.security_id, cd.date, cd.close
                  FROM candles_daily cd
                  JOIN constituents c ON c.security_id = cd.security_id
                 WHERE cd.date >= CURRENT_DATE - %s
                   AND cd.close IS NOT NULL
                   AND cd.close > 0
            ),
            base AS (
                SELECT DISTINCT ON (security_id)
                       security_id, close AS base_close, date AS base_date
                  FROM candles
              ORDER BY security_id, date ASC
            ),
            rebased AS (
                SELECT c.date, c.security_id,
                       100.0 * c.close / NULLIF(b.base_close, 0) AS idx
                  FROM candles c
                  JOIN base b ON b.security_id = c.security_id
                 WHERE c.date >= b.base_date
            )
            SELECT date,
                   ROUND(AVG(idx)::numeric, 2) AS value,
                   COUNT(DISTINCT security_id)::int AS n
              FROM rebased
          GROUP BY date
          ORDER BY date ASC
            """,
            (slug, days),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        close_db(conn)


def get_stock_themes(security_id: str) -> list[dict]:
    """Themes attached to one stock, ordered by exposure."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT st.theme_slug, t.name AS theme_name,
                   t.wave_slug, w.name AS wave_name,
                   t.accent_color, w.accent_color AS wave_accent,
                   st.exposure_score, st.source, st.confidence,
                   st.matched_term, st.evidence_url, st.evidence_note,
                   st.is_primary
              FROM stock_themes_v2 st
              JOIN themes_v2 t ON t.slug = st.theme_slug
              JOIN waves_v2 w  ON w.slug = t.wave_slug
             WHERE st.security_id = %s
          ORDER BY st.exposure_score DESC NULLS LAST
            """,
            (security_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        close_db(conn)
