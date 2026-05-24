"""
Classification V2 — segment-driven, evidence-backed taxonomy.

Public surface
    schema.init_schema()          — apply the V2 migration (idempotent)
    seeds.seed_taxonomy()         — upsert waves_v2, themes_v2, custom_sectors_v2
    classifier.classify_all(...)  — batch classify the universe
    classifier.classify_one(...)  — single-symbol classification
    spine.get_spine(symbol|sid)   — assemble the full spine for the API

V1 (custom_sectors / themes / stock_themes) is read-only from this package.
We only ever write to *_v2 tables.
"""
from . import schema, seeds, classifier, spine  # noqa: F401

__all__ = ["schema", "seeds", "classifier", "spine"]
