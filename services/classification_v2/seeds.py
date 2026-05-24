"""Seed V2 taxonomy tables from the V2 config files.

Reads:
    config/custom_sectors_seed_v2.CUSTOM_SECTORS_SEED_V2
    config/themes_seed_v2.WAVES_SEED_V2
    config/themes_seed_v2.THEMES_SEED_V2

Writes:
    waves_v2, themes_v2, custom_sectors_v2

Idempotent — re-running upserts. Schema is initialised first if missing.
"""
from __future__ import annotations

import json

from database.database import get_db, close_db

from . import schema as v2_schema


def seed_taxonomy() -> dict:
    from config.custom_sectors_seed_v2 import CUSTOM_SECTORS_SEED_V2
    from config.themes_seed_v2 import WAVES_SEED_V2, THEMES_SEED_V2

    v2_schema.init_schema()

    waves_n = themes_n = sub_n = 0
    conn = get_db()
    try:
        cur = conn.cursor()

        # ── waves_v2 ──────────────────────────────────────────────
        for w in WAVES_SEED_V2:
            cur.execute(
                """
                INSERT INTO waves_v2 (slug, name, description, accent_color, sort_order)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                    SET name        = EXCLUDED.name,
                        description = EXCLUDED.description,
                        accent_color= EXCLUDED.accent_color,
                        sort_order  = EXCLUDED.sort_order,
                        updated_at  = NOW()
                """,
                (w["slug"], w["name"], w.get("description", ""),
                 w.get("accent_color"), w.get("sort_order", 99)),
            )
            waves_n += 1

        # ── themes_v2 ─────────────────────────────────────────────
        for t in THEMES_SEED_V2:
            parent_accent = next(
                (w.get("accent_color") for w in WAVES_SEED_V2 if w["slug"] == t["wave"]),
                None,
            )
            from config.themes_seed_v2 import THEME_PARENT_SECTOR_V2
            parent_sector = THEME_PARENT_SECTOR_V2.get(t["slug"])

            cur.execute(
                """
                INSERT INTO themes_v2
                    (slug, name, wave_slug, parent_sector, description,
                     keywords, name_overrides, segment_keywords,
                     accent_color, sort_order)
                VALUES (%s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb,
                        %s, %s)
                ON CONFLICT (slug) DO UPDATE
                    SET name             = EXCLUDED.name,
                        wave_slug        = EXCLUDED.wave_slug,
                        parent_sector    = EXCLUDED.parent_sector,
                        description      = EXCLUDED.description,
                        keywords         = EXCLUDED.keywords,
                        name_overrides   = EXCLUDED.name_overrides,
                        segment_keywords = EXCLUDED.segment_keywords,
                        accent_color     = EXCLUDED.accent_color,
                        sort_order       = EXCLUDED.sort_order,
                        updated_at       = NOW()
                """,
                (
                    t["slug"], t["name"], t["wave"], parent_sector,
                    t.get("description", ""),
                    json.dumps(t.get("keywords", [])),
                    json.dumps(t.get("name_overrides", [])),
                    json.dumps(t.get("segment_keywords", [])),
                    t.get("accent_color") or parent_accent,
                    t.get("sort_order", 99),
                ),
            )
            themes_n += 1

        # ── custom_sectors_v2 ─────────────────────────────────────
        for s in CUSTOM_SECTORS_SEED_V2:
            cur.execute(
                """
                INSERT INTO custom_sectors_v2
                    (slug, name, parent_sector, description,
                     keywords, segment_keywords, name_overrides)
                VALUES (%s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb)
                ON CONFLICT (slug) DO UPDATE
                    SET name             = EXCLUDED.name,
                        parent_sector    = EXCLUDED.parent_sector,
                        description      = EXCLUDED.description,
                        keywords         = EXCLUDED.keywords,
                        segment_keywords = EXCLUDED.segment_keywords,
                        name_overrides   = EXCLUDED.name_overrides,
                        updated_at       = NOW()
                """,
                (
                    s["slug"], s["name"], s["parent_sector"],
                    s.get("description", ""),
                    json.dumps(s.get("keywords", [])),
                    json.dumps(s.get("segment_keywords", [])),
                    json.dumps(s.get("name_overrides", [])),
                ),
            )
            sub_n += 1

        conn.commit()
    finally:
        close_db(conn)

    return {"waves": waves_n, "themes": themes_n, "sub_sectors": sub_n}
