"""
Custom Sectors V2 — seed taxonomy.

Layers carefully on top of V1 (`custom_sectors_seed.py`) so improvements to
the V1 baseline (e.g. someone adds Gail to gas-distribution) automatically
flow into V2. V2 owns the *deltas*: deletions, keyword overrides, brand-new
sub-sectors, and the new `segment_keywords` / `name_overrides` columns the
V2 classifier uses for higher-precision matching.

The composed list is exported as `CUSTOM_SECTORS_SEED_V2`. Each entry has
the same shape as V1 plus three new keys:

    segment_keywords  — substrings matched against `segments_quarterly.segment_name`.
                        These are the *high-precision* matchers — a hit
                        means the company explicitly reports this line of
                        business, not just talks about it.
    name_overrides    — symbols that ARE pure-play in this sub-sector
                        regardless of any keyword match. Used sparingly,
                        only when revenue mix already verifies it.
    requires_segment  — bool, default False. If True, the V2 classifier
                        will only assign this sub-sector if the stock has a
                        matching segment_quarterly row at >= 15% revenue.
                        Used for emerging buckets where keyword false
                        positives are common (wires-cables, optical-networking).

Seed loader: services/classification_v2/seeds.py.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Iterable

from config.custom_sectors_seed import CUSTOM_SECTORS_SEED as _V1_BASE


# ─────────────────────────────────────────────────────────────────────
# 1. DELETIONS — V1 slugs we drop in V2
# ─────────────────────────────────────────────────────────────────────
# Currently none. V1 entries are kept as a baseline.
_DELETIONS: set[str] = set()


# ─────────────────────────────────────────────────────────────────────
# 2. KEYWORD OVERRIDES — existing V1 slugs whose keyword/segment lists
#    change in V2. Matches by slug; partial update (other fields preserved).
# ─────────────────────────────────────────────────────────────────────
_OVERRIDES: dict[str, dict] = {
    # The "wires and cables" keyword in V1 routed Polycab/KEI/Finolex
    # Cables into Consumer Durables. That's wrong — they're industrial
    # capex plays selling into builders, T&D and DC connectivity. Remove
    # the misleading keyword here; the new `wires-cables` sub-sector
    # below picks them up.
    "consumer-durables": {
        "remove_keywords": ["wires and cables"],
    },

    # Power Transmission scope tightened: "distribution" alone is too
    # generic (FMCG also distribute). The new sub-sectors cover the
    # specific equipment / cable plays.
    "power-transmission": {
        "remove_keywords": ["distribution", "power distribution"],
        "add_segment_keywords": [
            "transmission and distribution",
            "power transmission",
            "transmission lines",
            "power grid services",
        ],
        "add_name_overrides": ["POWERGRID", "ADANIENSOL"],
    },
}


# ─────────────────────────────────────────────────────────────────────
# 3. ADDITIONS — brand-new V2 sub-sectors
# ─────────────────────────────────────────────────────────────────────
# Most of these are kept `requires_segment=True` — we'd rather miss a
# stock than mis-tag one. The V2 classifier checks segments_quarterly
# before assigning these.
_ADDITIONS: list[dict] = [
    # ── Power & Utilities ────────────────────────────────────────────
    {
        "slug": "wires-cables",
        "name": "Wires & Cables",
        "parent_sector": "Power & Utilities",
        "description": (
            "Industrial wires and cables manufacturers — power cable, "
            "winding wire, communication cable, building wire. Sells "
            "into infra, T&D, builders, hyperscalers."
        ),
        "keywords": [
            "wires and cables", "wires & cables", "wire and cable",
            "industrial cables", "power cable", "building wire",
            "winding wire", "ehv cable", "lt cable", "ht cable",
            "control cable", "instrumentation cable",
        ],
        "segment_keywords": [
            "wires and cables", "wires & cables",
            "cables", "wires", "winding wire",
            "power cables", "industrial cables",
        ],
        "name_overrides": [
            "POLYCAB", "KEI", "FINOLEXIND",
            "UNIVCABLES", "BIRLACABLE", "PRECWIRE",
            "RRKABEL", "VMARCIND", "PARACABLES",
            "DYNAMATECH", "CDS",
        ],
        "requires_segment": False,  # name_overrides are well-verified
    },

    # ── Telecom & Media ──────────────────────────────────────────────
    {
        "slug": "optical-networking",
        "name": "Optical Networking",
        "parent_sector": "Telecom & Media",
        "description": (
            "Optical fiber, optical fiber cable (OFC), optical "
            "transport / DWDM equipment. Sells into telcos for 5G, "
            "into hyperscalers for DC connectivity, and into defence."
        ),
        "keywords": [
            "optical fiber", "optical fibre", "ofc",
            "optical networking", "dwdm", "optical transport",
            "fiber to the home", "fttx", "fiber cable",
        ],
        "segment_keywords": [
            "optical networking", "optical fiber", "optical fibre",
            "ofc", "fiber cables", "telecom products",
            "telecom equipment", "communication systems",
        ],
        # Intentionally empty. STLTECH / HFCL / TEJASNET / RAILTEL are
        # plausible candidates but the V2 classifier should *prove* it
        # via segments_quarterly + concall, not assume.
        "name_overrides": [],
        "requires_segment": True,
    },

    # ── Engineering & Capital Goods ──────────────────────────────────
    {
        "slug": "precision-engineering",
        "name": "Precision Engineering",
        "parent_sector": "Engineering & Capital Goods",
        "description": (
            "Precision-machined components for aerospace, nuclear, "
            "defence, semiconductor capex and clean-energy. Common "
            "thread: tight tolerances, low-volume / high-mix."
        ),
        "keywords": [
            "precision engineering", "precision machining",
            "precision components", "precision parts",
            "high precision", "tight tolerance",
        ],
        "segment_keywords": [
            "precision engineering", "precision components",
            "precision products", "machined components",
        ],
        "name_overrides": ["MTARTECH", "HARSHA", "AZAD", "PARASDEFENCE"],
        "requires_segment": False,
    },
    {
        "slug": "industrial-hoses-fluid",
        "name": "Industrial Hoses & Fluid Systems",
        "parent_sector": "Engineering & Capital Goods",
        "description": (
            "Specialty hoses, fluid-handling assemblies and fittings "
            "for industrial, semiconductor, DC cooling, oil & gas."
        ),
        "keywords": [
            "industrial hose", "ptfe hose", "metal hose",
            "composite hose", "expansion joints",
        ],
        "segment_keywords": [
            "industrial hose", "ptfe", "stainless steel hose",
            "metal hoses", "composite hoses",
        ],
        # AEROFLEX reports a single 100% "manufacturing of product" segment
        # in segments_quarterly — useless for keyword/segment match — so we
        # curate it explicitly. requires_segment stays False so this curated
        # override is honoured.
        "name_overrides": ["AEROFLEX"],
        "requires_segment": False,
    },
    {
        "slug": "ems-electronics-mfg",
        "name": "EMS / Electronics Manufacturing",
        "parent_sector": "Engineering & Capital Goods",
        "description": (
            "Electronics manufacturing services, PCB assembly, "
            "box-build and contract manufacturing — separate from "
            "consumer durables because the buyer is industrial / OEM."
        ),
        "keywords": [
            "electronics manufacturing services", "ems",
            "pcb assembly", "box build", "contract manufacturing",
        ],
        "segment_keywords": [
            "electronics manufacturing services",
            "pcb", "printed circuit board", "ems",
            "electronic components", "box-build",
        ],
        "name_overrides": ["DIXON", "SYRMA", "AVALON", "KAYNES",
                           "CYIENTDLM", "PGEL", "ELIN"],
        "requires_segment": False,
    },

    # ── IT & Technology ──────────────────────────────────────────────
    {
        "slug": "ai-compute-hardware",
        "name": "AI Compute Hardware",
        "parent_sector": "IT & Technology",
        "description": (
            "Servers, HPC/GPU systems, AI-ready compute boxes, "
            "private-cloud infrastructure for AI workloads."
        ),
        "keywords": [
            "ai server", "gpu server", "hpc",
            "ai compute", "supercomputer", "private cloud infrastructure",
        ],
        "segment_keywords": [
            "computer servers", "computer and computer servers",
            "high performance computing", "ai cluster",
            "private cloud infrastructure",
        ],
        "name_overrides": ["NETWEB"],
        "requires_segment": False,
    },

    # ── Defence & Aerospace ──────────────────────────────────────────
    {
        "slug": "defence-electronics",
        "name": "Defence Electronics",
        "parent_sector": "Defence & Aerospace",
        "description": (
            "Radar, sonar, EW, avionics, secure communications and "
            "mission-critical electronics for defence."
        ),
        "keywords": [
            "defence electronics", "defense electronics",
            "radar", "sonar", "electronic warfare",
            "avionics", "secure communication",
        ],
        "segment_keywords": [
            "defence electronics", "radar systems", "sonar",
            "electronic warfare", "avionics",
        ],
        "name_overrides": ["BEL", "DATAPATTNS", "ASTRAMICRO",
                           "PARASDEFENCE", "SOLARINDS"],
        "requires_segment": False,
    },

    # ── Cement & Building Materials  ─────────────────────────────────
    # (none in V2 yet — V1 coverage adequate)
]


# ─────────────────────────────────────────────────────────────────────
# 4. COMPOSE the final V2 seed
# ─────────────────────────────────────────────────────────────────────
def _apply_overrides(entry: dict, override: dict) -> dict:
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
    if "parent_sector" in override:
        out["parent_sector"] = override["parent_sector"]
    return out


def _normalise(entry: dict) -> dict:
    """Ensure every entry has the V2 keys, even if V1 lacked them."""
    out = deepcopy(entry)
    out.setdefault("segment_keywords", [])
    out.setdefault("name_overrides", [])
    out.setdefault("requires_segment", False)
    return out


def _build() -> list[dict]:
    composed: list[dict] = []
    seen_slugs: set[str] = set()
    for entry in _V1_BASE:
        slug = entry["slug"]
        if slug in _DELETIONS:
            continue
        e = _normalise(entry)
        if slug in _OVERRIDES:
            e = _apply_overrides(e, _OVERRIDES[slug])
        composed.append(e)
        seen_slugs.add(slug)
    for entry in _ADDITIONS:
        if entry["slug"] in seen_slugs:
            # V2 addition collides with a V1 slug — V2 wins.
            composed = [c for c in composed if c["slug"] != entry["slug"]]
        composed.append(_normalise(entry))
    return composed


CUSTOM_SECTORS_SEED_V2: list[dict] = _build()


# Convenience for `python -m config.custom_sectors_seed_v2`
if __name__ == "__main__":
    print(f"V2 sub-sectors: {len(CUSTOM_SECTORS_SEED_V2)} "
          f"(V1: {len(_V1_BASE)}, additions: {len(_ADDITIONS)}, "
          f"deletions: {len(_DELETIONS)}, overrides: {len(_OVERRIDES)})")
    for e in CUSTOM_SECTORS_SEED_V2:
        if e.get("name_overrides"):
            print(f"  {e['slug']:32s} → {e['parent_sector']:32s}  "
                  f"overrides={len(e['name_overrides'])}")
