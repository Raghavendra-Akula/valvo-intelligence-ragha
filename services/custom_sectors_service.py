"""
Custom Sectors — service layer.

Provides the primitives the (future) custom-sector analysis feature will
rely on. Pure backend/data-model for now; no frontend is wired yet.

Responsibilities:
    * init_schema()         — run the SQL migration (tables + indexes)
    * seed_taxonomy()       — upsert the default sub-sector list from
                              config/custom_sectors_seed.py
    * classify_universe()   — keyword-match every stock in
                              stock_universe against the seeded keywords
                              and populate stock_custom_sector
    * list_sectors()        — read-side helper (taxonomy only)
    * get_constituents()    — stocks belonging to a given custom sector
    * get_primary_sector()  — for a security_id
    * summary_performance() — avg/sum performance per custom sector over
                              a window (used by the future UI)

All DB work goes through database.database.get_db / close_db so we share
the existing pool.
"""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, Optional

from database.database import get_db, close_db


_SCHEMA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "database",
    "create_custom_sectors_tables.sql",
)


# ══════════════════════════════════════════════════════════════
# Schema + seeding
# ══════════════════════════════════════════════════════════════
def init_schema() -> None:
    """Create the custom-sector tables if they don't exist. Idempotent."""
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
    """Upsert the default taxonomy from custom_sectors_seed.CUSTOM_SECTORS_SEED.
    Returns a count summary { 'inserted': n, 'updated': n, 'total': n }."""
    from config.custom_sectors_seed import CUSTOM_SECTORS_SEED

    init_schema()  # make sure tables exist
    conn = get_db()
    inserted = updated = 0
    try:
        cur = conn.cursor()
        for entry in CUSTOM_SECTORS_SEED:
            cur.execute(
                """
                INSERT INTO custom_sectors (slug, name, parent_sector, description, keywords)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (slug) DO UPDATE
                    SET name = EXCLUDED.name,
                        parent_sector = EXCLUDED.parent_sector,
                        description = EXCLUDED.description,
                        keywords = EXCLUDED.keywords,
                        updated_at = NOW()
                RETURNING (xmax = 0) AS was_insert
                """,
                (
                    entry["slug"],
                    entry["name"],
                    entry["parent_sector"],
                    entry.get("description", ""),
                    json.dumps(entry.get("keywords", [])),
                ),
            )
            row = cur.fetchone()
            if row and row["was_insert"]:
                inserted += 1
            else:
                updated += 1
        conn.commit()
    finally:
        close_db(conn)
    return {"inserted": inserted, "updated": updated, "total": inserted + updated}


# ══════════════════════════════════════════════════════════════
# Classification
# ══════════════════════════════════════════════════════════════
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", (s or "").lower()).strip()


def _best_match(text: str, rules: list[tuple[int, str, list[str]]]) -> Optional[tuple[int, str]]:
    """Given normalised text and rules [(sector_id, slug, keywords)],
    return (sector_id, matched_keyword) for the longest matching keyword.
    Ties broken by first-seen order."""
    best = None
    best_len = 0
    for sid, _slug, keywords in rules:
        for kw in keywords:
            kw_norm = _norm(kw)
            if not kw_norm:
                continue
            # Pad with spaces to respect word-boundaries when kw is short
            hay = f" {text} "
            needle = f" {kw_norm} " if len(kw_norm) <= 4 else kw_norm
            if needle in hay:
                if len(kw_norm) > best_len:
                    best = (sid, kw_norm)
                    best_len = len(kw_norm)
    return best


def classify_universe(limit: Optional[int] = None, overwrite: bool = False) -> dict:
    """Run keyword classification across stock_universe.
    * limit — stop after N stocks (debug)
    * overwrite — if True, replace existing stock_custom_sector rows; else skip
                  securities that already have a primary custom sector.

    Returns { 'scanned': n, 'assigned': n, 'unassigned': n }.
    """
    conn = get_db()
    scanned = assigned = 0
    try:
        cur = conn.cursor()
        # Load active taxonomy
        cur.execute(
            "SELECT id, slug, keywords FROM custom_sectors WHERE is_active = TRUE"
        )
        rules: list[tuple[int, str, list[str]]] = []
        for r in cur.fetchall():
            kw = r["keywords"] if isinstance(r["keywords"], list) else json.loads(r["keywords"] or "[]")
            rules.append((r["id"], r["slug"], kw))

        # Pull candidate securities. Include name, sector, and industry if it exists.
        cur.execute(
            """
            SELECT su.security_id,
                   COALESCE(su.company_name, '') AS name,
                   COALESCE(su.sector, '')       AS sector,
                   COALESCE(su.industry, '')     AS industry
              FROM stock_universe su
             WHERE COALESCE(su.is_active, true) = true
          ORDER BY su.security_id
            """
        )
        rows = cur.fetchall()
        if limit:
            rows = rows[:limit]

        for row in rows:
            scanned += 1
            sid = row["security_id"]

            if not overwrite:
                cur.execute(
                    "SELECT 1 FROM stock_custom_sector WHERE security_id=%s AND is_primary=TRUE LIMIT 1",
                    (sid,),
                )
                if cur.fetchone():
                    continue

            text = _norm(f"{row['name']} {row['sector']} {row['industry']}")
            match = _best_match(text, rules)
            if not match:
                cur.execute(
                    """INSERT INTO custom_sector_classification_log
                        (security_id, custom_sector_id, source, confidence, matched_keyword, raw_input)
                       VALUES (%s, NULL, 'keyword', 0.000, NULL, %s)""",
                    (sid, text[:500]),
                )
                continue

            cs_id, matched_kw = match
            confidence = min(1.0, 0.4 + 0.05 * len(matched_kw))  # 0.4–1.0 based on keyword length

            if overwrite:
                cur.execute(
                    "DELETE FROM stock_custom_sector WHERE security_id=%s",
                    (sid,),
                )
            cur.execute(
                """
                INSERT INTO stock_custom_sector
                    (security_id, custom_sector_id, source, confidence, is_primary, note)
                VALUES (%s, %s, 'keyword', %s, TRUE, %s)
                ON CONFLICT (security_id, custom_sector_id) DO UPDATE
                    SET source     = EXCLUDED.source,
                        confidence = EXCLUDED.confidence,
                        is_primary = TRUE,
                        note       = EXCLUDED.note,
                        updated_at = NOW()
                """,
                (sid, cs_id, confidence, f"matched:{matched_kw}"),
            )
            cur.execute(
                """INSERT INTO custom_sector_classification_log
                    (security_id, custom_sector_id, source, confidence, matched_keyword, raw_input)
                   VALUES (%s, %s, 'keyword', %s, %s, %s)""",
                (sid, cs_id, confidence, matched_kw, text[:500]),
            )
            assigned += 1

        conn.commit()
    finally:
        close_db(conn)
    return {"scanned": scanned, "assigned": assigned, "unassigned": scanned - assigned}


# ══════════════════════════════════════════════════════════════
# Read helpers
# ══════════════════════════════════════════════════════════════
def list_sectors(parent: Optional[str] = None) -> list[dict]:
    """Return taxonomy rows. Optional filter by broad parent sector."""
    conn = get_db()
    try:
        cur = conn.cursor()
        if parent:
            cur.execute(
                """SELECT id, slug, name, parent_sector, description, keywords
                     FROM custom_sectors
                    WHERE is_active = TRUE AND parent_sector = %s
                 ORDER BY name""",
                (parent,),
            )
        else:
            cur.execute(
                """SELECT id, slug, name, parent_sector, description, keywords
                     FROM custom_sectors
                    WHERE is_active = TRUE
                 ORDER BY parent_sector, name"""
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        close_db(conn)


def get_constituents(slug: str) -> list[dict]:
    """Stocks assigned to a given custom sector (by slug)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT su.security_id, su.company_name AS name, su.sector,
                   scs.confidence, scs.source, scs.is_primary
              FROM custom_sectors cs
              JOIN stock_custom_sector scs ON scs.custom_sector_id = cs.id
              JOIN stock_universe su       ON su.security_id       = scs.security_id
             WHERE cs.slug = %s
          ORDER BY scs.is_primary DESC, su.company_name
            """,
            (slug,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        close_db(conn)


def get_primary_sector(security_id: int) -> Optional[dict]:
    """Fetch the primary custom sector for a stock (or None)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT cs.id, cs.slug, cs.name, cs.parent_sector, scs.confidence, scs.source
                 FROM stock_custom_sector scs
                 JOIN custom_sectors cs ON cs.id = scs.custom_sector_id
                WHERE scs.security_id = %s AND scs.is_primary = TRUE
                LIMIT 1""",
            (security_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        close_db(conn)


def summary_performance(days: int = 30) -> list[dict]:
    """Aggregate per-custom-sector average close-to-close return over `days`.
    Used by the future comparison UI. Returns one row per sector with:
        { slug, name, parent_sector, constituents, avg_pct, median_pct,
          pct_above_avg, pct_below_avg }
    """
    days = max(1, min(int(days), 2000))
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            WITH per_stock AS (
                SELECT scs.custom_sector_id,
                       scs.security_id,
                       -- current close: prefer live_close, else last row from candles_daily
                       COALESCE(s.live_close, s.prev_close)            AS cur_close,
                       -- base close: candles_daily close from `days` days back
                       (SELECT close FROM candles_daily cd
                         WHERE cd.security_id = scs.security_id
                           AND cd.date <= CURRENT_DATE - %s
                      ORDER BY cd.date DESC LIMIT 1)                    AS base_close
                  FROM stock_custom_sector scs
                  JOIN stock_daily_summary s ON s.security_id = scs.security_id
                 WHERE scs.is_primary = TRUE
                   AND COALESCE(s.is_etf, false) = false
            ),
            with_pct AS (
                SELECT custom_sector_id,
                       security_id,
                       100.0 * (cur_close - base_close) / NULLIF(base_close, 0) AS pct
                  FROM per_stock
                 WHERE base_close IS NOT NULL AND base_close > 0
            )
            SELECT cs.id, cs.slug, cs.name, cs.parent_sector,
                   COUNT(wp.security_id)                                             AS constituents,
                   ROUND(AVG(wp.pct)::numeric, 2)                                     AS avg_pct,
                   ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY wp.pct)::numeric, 2) AS median_pct,
                   ROUND(100.0 * COUNT(*) FILTER (WHERE wp.pct > 0) / NULLIF(COUNT(*),0), 1) AS pct_above_avg,
                   ROUND(100.0 * COUNT(*) FILTER (WHERE wp.pct < 0) / NULLIF(COUNT(*),0), 1) AS pct_below_avg
              FROM custom_sectors cs
         LEFT JOIN with_pct wp ON wp.custom_sector_id = cs.id
             WHERE cs.is_active = TRUE
          GROUP BY cs.id, cs.slug, cs.name, cs.parent_sector
          ORDER BY avg_pct DESC NULLS LAST
            """,
            (days,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        close_db(conn)
