"""
Themes V2 — seed taxonomy.

Layers on top of V1 themes (`themes_seed.py`). Same delta-on-base pattern
as `custom_sectors_seed_v2.py`. Adds:

  * dc_connectivity_fiber  — optical fiber / OFC / DC interconnect
  * defence_naval_subsea   — split out from V1 defence_indigenization
  * (room for more as we curate)

V2 themes carry a `segment_keywords` field (matched against
`segments_quarterly.segment_name`) and a `requires_segment` flag (skip
keyword-only assignment for high-noise themes).

Composition is identical to V2 sub-sectors: V1 base → overrides →
additions. Exported as `WAVES_SEED_V2`, `THEMES_SEED_V2` and
`THEME_PARENT_SECTOR_V2`.
"""
from __future__ import annotations

from copy import deepcopy

from config.themes_seed import (
    WAVES_SEED as _V1_WAVES,
    THEMES_SEED as _V1_THEMES,
    THEME_PARENT_SECTOR as _V1_THEME_PARENT_SECTOR,
)


# ─────────────────────────────────────────────────────────────────────
# WAVES — V2 layers little here; V1 covers all 6 macro-narratives well.
# ─────────────────────────────────────────────────────────────────────
_WAVE_DELETIONS: set[str] = set()
_WAVE_OVERRIDES: dict[str, dict] = {}
_WAVE_ADDITIONS: list[dict] = []


# ─────────────────────────────────────────────────────────────────────
# THEMES — overrides and additions
# ─────────────────────────────────────────────────────────────────────
_THEME_DELETIONS: set[str] = set()

_THEME_OVERRIDES: dict[str, dict] = {
    # The V1 dc_power_infra had MTAR-adjacent capex names; we add MTAR
    # (precision components for DC-grade power gear) only after segments
    # / concall verify it. For now, just add segment_keywords for sharper
    # match — name_overrides are touched in the V2 priority curate pass.
    "dc_power_infra": {
        "add_segment_keywords": [
            "transformers", "switchgear", "ups",
            "uninterruptible power supply",
            "low voltage motors", "medium voltage motors",
            "power electronics",
            "data centre power", "data center power",
        ],
    },
    "dc_cooling_hvac": {
        "add_segment_keywords": [
            "heat exchanger", "heat exchangers",
            "air conditioning", "hvac", "chillers",
            "precision cooling", "cooling systems",
            "data centre cooling", "data center cooling",
        ],
    },
    "ai_compute_hardware": {
        "add_segment_keywords": [
            "computer servers", "computer and computer servers",
            "high performance computing", "ai server",
            "private cloud infrastructure", "supercomputing",
        ],
    },
    "ems_electronics": {
        "add_segment_keywords": [
            "electronics manufacturing services",
            "pcb", "printed circuit board",
            "box-build", "contract manufacturing",
        ],
    },
    "semiconductors_osat": {
        "add_segment_keywords": [
            "semiconductor", "osat", "atmp",
            "wafer", "ic packaging",
            "discrete semiconductor", "power semiconductor",
        ],
    },
}


_THEME_ADDITIONS: list[dict] = [
    # ── AI & DIGITAL INFRA — new theme ────────────────────────────────
    {
        "slug": "dc_connectivity_fiber",
        "wave": "ai_digital_infra",
        "name": "DC Connectivity & Fiber",
        "description": (
            "Optical fiber cable, DWDM optical transport and structured "
            "cabling for hyperscale data centres and the 5G fronthaul/"
            "backhaul that feeds them."
        ),
        "keywords": [
            "optical fiber cable to data center",
            "data center connectivity",
            "hyperscale fiber",
        ],
        "segment_keywords": [
            "optical fiber", "optical fibre", "ofc",
            "optical networking", "dwdm",
            "fiber to the home", "fttx",
            "structured cabling",
            "telecom products", "telecom equipment",
        ],
        # Empty by design. STLTECH / HFCL / TEJASNET / RAILTEL are
        # candidates but the V2 classifier will only assign this theme
        # if (a) segments_quarterly shows ≥15% revenue from a matching
        # segment, OR (b) concall_understanding flags AI/DC exposure.
        "name_overrides": [],
        "requires_segment": True,
    },

    # ── ENERGY TRANSITION — wires & cables sub-narrative ──────────────
    {
        "slug": "transmission_wires_cables",
        "wave": "energy_transition",
        "name": "Wires & Cables (T&D Capex)",
        "description": (
            "Industrial wires and cables benefiting from the T&D "
            "transmission build-out — power cable, EHV cable, "
            "winding wire."
        ),
        "keywords": [
            "wires and cables for transmission",
            "ehv cable", "ht cable", "power cable",
        ],
        "segment_keywords": [
            "wires and cables", "wires & cables",
            "cables", "wires", "winding wire",
        ],
        "name_overrides": [
            "POLYCAB", "KEI", "FINOLEXIND",
            "UNIVCABLES", "BIRLACABLE", "PRECWIRE",
            "RRKABEL",
        ],
        "requires_segment": False,
    },
]

# Theme-to-parent-sector mapping for the V2 additions.
_THEME_PARENT_ADDITIONS: dict[str, str] = {
    "dc_connectivity_fiber": "Telecom & Media",
    "transmission_wires_cables": "Power & Utilities",
}


# ─────────────────────────────────────────────────────────────────────
# Compose
# ─────────────────────────────────────────────────────────────────────
def _normalise_theme(entry: dict) -> dict:
    out = deepcopy(entry)
    out.setdefault("segment_keywords", [])
    out.setdefault("name_overrides", out.get("name_overrides", []))
    out.setdefault("requires_segment", False)
    return out


def _apply_theme_override(entry: dict, override: dict) -> dict:
    out = deepcopy(entry)
    if "remove_keywords" in override:
        bad = {k.lower() for k in override["remove_keywords"]}
        out["keywords"] = [k for k in out.get("keywords", []) if k.lower() not in bad]
    if "add_keywords" in override:
        out["keywords"] = list(out.get("keywords", [])) + list(override["add_keywords"])
    if "add_segment_keywords" in override:
        out["segment_keywords"] = list(out.get("segment_keywords", [])) + list(override["add_segment_keywords"])
    if "add_name_overrides" in override:
        out["name_overrides"] = list(out.get("name_overrides", [])) + list(override["add_name_overrides"])
    return out


def _build_waves() -> list[dict]:
    composed: list[dict] = []
    for w in _V1_WAVES:
        if w["slug"] in _WAVE_DELETIONS:
            continue
        composed.append(deepcopy(w))
    composed.extend(deepcopy(_WAVE_ADDITIONS))
    return composed


def _build_themes() -> list[dict]:
    composed: list[dict] = []
    seen: set[str] = set()
    for t in _V1_THEMES:
        slug = t["slug"]
        if slug in _THEME_DELETIONS:
            continue
        e = _normalise_theme(t)
        if slug in _THEME_OVERRIDES:
            e = _apply_theme_override(e, _THEME_OVERRIDES[slug])
        composed.append(e)
        seen.add(slug)
    for t in _THEME_ADDITIONS:
        if t["slug"] in seen:
            composed = [c for c in composed if c["slug"] != t["slug"]]
        composed.append(_normalise_theme(t))
    return composed


def _build_theme_parent_sector() -> dict[str, str]:
    out = dict(_V1_THEME_PARENT_SECTOR)
    out.update(_THEME_PARENT_ADDITIONS)
    return out


WAVES_SEED_V2: list[dict] = _build_waves()
THEMES_SEED_V2: list[dict] = _build_themes()
THEME_PARENT_SECTOR_V2: dict[str, str] = _build_theme_parent_sector()


if __name__ == "__main__":
    print(f"V2 waves: {len(WAVES_SEED_V2)}")
    print(f"V2 themes: {len(THEMES_SEED_V2)} "
          f"(V1: {len(_V1_THEMES)}, additions: {len(_THEME_ADDITIONS)}, "
          f"overrides: {len(_THEME_OVERRIDES)})")
    for t in THEMES_SEED_V2:
        if t.get("requires_segment") or t["slug"] in _THEME_OVERRIDES or any(
                t["slug"] == a["slug"] for a in _THEME_ADDITIONS):
            print(f"  {t['slug']:32s} wave={t['wave']:24s} "
                  f"name_overrides={len(t.get('name_overrides', []))} "
                  f"requires_segment={t.get('requires_segment')}")
