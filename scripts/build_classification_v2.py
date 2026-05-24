"""
Unified classification v2 — derive sector from primary theme.

Goal
----
Resolve the long-standing drift between two parallel classification systems:
  * stock_universe.valvo_sector       (20 canonical parent sectors, revenue-keyword path)
  * stock_themes.theme_slug           (22 themes under 6 waves, theme-keyword path)

They share input data (segments_quarterly) but use independent keyword maps,
which is why NETWEB's theme is "ai_compute_hardware" while its sector is blank,
or GRSE's theme is "defence_indigenization" while its sector reads
"Infrastructure & Construction".

Rule
----
For every stock:
  1. If it has at least one theme → primary_theme.parent_sector becomes the
     canonical sector. (See THEME_PARENT_SECTOR in config/themes_seed.py.)
  2. Otherwise → keep the existing valvo_sector from the revenue-keyword path.

This script is *read-only on the existing data* — it does not write to the DB,
does not modify the existing CSVs in /docs. It produces three fresh files:

  docs/stock_classification_v2.csv     full per-stock view (old vs new)
  docs/classification_drift_v2.csv     subset where sector changed
  docs/sector_overrides_v2.csv         stub for manual exceptions

Run
---
    python -m scripts.build_classification_v2

Then review the drift CSV. Once satisfied, a follow-up commit script will
sync the v2 sectors into stock_universe.valvo_sector.
"""
from __future__ import annotations

import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(BACKEND))

from config.themes_seed import THEME_PARENT_SECTOR  # noqa: E402

# Themes whose membership legitimately spans two canonical parent sectors.
# When a stock's primary theme is in this set AND its old (revenue-path) sector
# is one of the canonical candidates, KEEP the old sector instead of forcing
# the theme's default parent. Avoids regressions like AMCs being routed to
# Insurance just because the theme bundles AMCs+Insurers under one slug.
THEME_AMBIGUOUS_TO: dict[str, set[str]] = {
    # AMCs vs Insurers vs IT-BFSI vertical vs Banks holding insurance subs
    "wealth_amc_insurance": {"Capital Markets", "Insurance", "IT & Technology", "Banks & Finance"},
    # Consumer durables (Crompton, V-Guard, LG) vs true EMS (Dixon, Kaynes)
    "electronics_pli":      {"FMCG & Consumer", "IT & Technology"},
    "ems_electronics":      {"FMCG & Consumer", "IT & Technology"},
    # Specialty chemicals for pharma (PI Industries, Acutaas) vs pure CDMO (Dishman)
    "pharma_api_cdmo":      {"Chemicals & Fertilizers", "Pharma & Healthcare"},
    # HVAC consumer-durable plays (Voltas, Blue Star, Amber) vs pure-engineering cooling
    "dc_cooling_hvac":      {"FMCG & Consumer", "Engineering & Capital Goods"},
}

DOCS = REPO / "docs"
SEGMENTS_CSV = DOCS / "stock_segments_master.csv"
THEMES_CSV = DOCS / "stock_theme_classification.csv"

OUT_FULL = DOCS / "stock_classification_v2.csv"
OUT_DRIFT = DOCS / "classification_drift_v2.csv"
OUT_OVERRIDES = DOCS / "sector_overrides_v2.csv"


def load_segments() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with SEGMENTS_CSV.open() as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip().upper()
            if sym:
                rows[sym] = r
    return rows


def load_themes() -> dict[str, list[dict]]:
    """{symbol: [{slug, exposure, is_primary, source, confidence, ...}], sorted desc by is_primary then exposure}"""
    by_sym: dict[str, list[dict]] = defaultdict(list)
    with THEMES_CSV.open() as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip().upper()
            slug = (r.get("theme_slug") or "").strip()
            if not sym or not slug:
                continue
            by_sym[sym].append({
                "theme_slug": slug,
                "exposure_score": float(r.get("exposure_score") or 0.0),
                "is_primary": int(r.get("is_primary") or 0),
                "source": (r.get("source") or "").strip(),
                "confidence": float(r.get("confidence") or 0.0),
            })
    for sym in by_sym:
        by_sym[sym].sort(key=lambda t: (t["is_primary"], t["exposure_score"]), reverse=True)
    return by_sym


def load_overrides() -> dict[str, str]:
    """Read sector_overrides_v2.csv → {symbol: forced_sector}. Comment rows starting with '#' ignored."""
    out: dict[str, str] = {}
    if not OUT_OVERRIDES.exists():
        return out
    with OUT_OVERRIDES.open() as f:
        r = csv.DictReader(f)
        for row in r:
            sym = (row.get("symbol") or "").strip().upper()
            forced = (row.get("forced_sector") or "").strip()
            if not sym or sym.startswith("#") or not forced:
                continue
            out[sym] = forced
    return out


def is_etf(company_name: str) -> bool:
    """ETFs (and index funds) are listed financial instruments — route to Capital Markets."""
    if not company_name:
        return False
    n = company_name.upper()
    # "ETF" anywhere, "BEES" suffix family (NIFTYBEES, BANKBEES…), or "INDEX FUND"
    return (" ETF" in n or n.endswith(" ETF") or "ETF " in n or
            "BEES" in n or "INDEX FUND" in n)


def derive_sector(themes: list[dict], current_sector: str, current_yahoo: str,
                  company_name: str = "") -> tuple[str, str]:
    """
    Returns (new_sector, reason).
    Priority:
      1. ETF programmatic rule → Capital Markets
      2. primary theme's parent_sector
      3. fallback to current valvo_sector if it's a known canonical sector
      4. "Uncategorized"
    """
    if is_etf(company_name):
        return "Capital Markets", "etf_rule"
    if themes:
        primary = themes[0]
        slug = primary["theme_slug"]
        parent = THEME_PARENT_SECTOR.get(slug)
        # Ambiguous-theme carve-out: theme spans multiple canonical sectors.
        # If old sector is already one of the legitimate candidates, keep it.
        ambiguous = THEME_AMBIGUOUS_TO.get(slug)
        if ambiguous and current_sector in ambiguous:
            return current_sector, f"theme_ambiguous_kept:{slug}({current_sector})"
        if parent:
            return parent, f"theme:{slug}→{parent}"
        return current_sector or "Uncategorized", f"theme_unmapped:{slug}"
    if current_sector:
        return current_sector, "kept_revenue_path"
    return "Uncategorized", "no_signal"


def main() -> None:
    if not SEGMENTS_CSV.exists() or not THEMES_CSV.exists():
        print(f"ERROR: missing input. Need {SEGMENTS_CSV} and {THEMES_CSV}", file=sys.stderr)
        sys.exit(1)

    segs = load_segments()
    themes = load_themes()
    overrides = load_overrides()

    # Sanity check: every theme slug from the source CSV maps to a parent
    unmapped = sorted({t["theme_slug"] for ts in themes.values() for t in ts
                       if t["theme_slug"] not in THEME_PARENT_SECTOR})
    if unmapped:
        print(f"WARN: {len(unmapped)} theme slugs in CSV have no parent_sector mapping:")
        for u in unmapped:
            print(f"  - {u}")

    cols = [
        "symbol", "company_name", "mcap_cr",
        "yahoo_sector", "old_valvo_sector", "new_valvo_sector",
        "primary_theme_slug", "all_themes", "themes_count",
        "sector_changed", "had_no_sector", "reason",
        "primary_segment", "primary_segment_pct",
    ]

    full_rows = []
    drift_rows = []
    counts = {
        "total": 0,
        "had_themes": 0,
        "no_themes": 0,
        "sector_changed": 0,
        "had_no_sector_filled": 0,
        "wrong_sector_corrected": 0,
        "kept_same": 0,
    }
    sector_distribution_old: dict[str, int] = defaultdict(int)
    sector_distribution_new: dict[str, int] = defaultdict(int)

    for sym, seg in segs.items():
        company = seg.get("company_name") or ""
        mcap = seg.get("mcap_cr") or ""
        yahoo = (seg.get("yahoo_sector") or "").strip()
        old_sector = (seg.get("valvo_sector") or "").strip()

        ts = themes.get(sym, [])
        if sym in overrides:
            new_sector, reason = overrides[sym], f"override:{overrides[sym]}"
        else:
            new_sector, reason = derive_sector(ts, old_sector, yahoo, company)

        primary_theme = ts[0]["theme_slug"] if ts else ""
        all_themes = "|".join(t["theme_slug"] for t in ts)

        sector_changed = (new_sector != old_sector)
        had_no_sector = (old_sector == "")

        full_rows.append({
            "symbol": sym,
            "company_name": company,
            "mcap_cr": mcap,
            "yahoo_sector": yahoo,
            "old_valvo_sector": old_sector,
            "new_valvo_sector": new_sector,
            "primary_theme_slug": primary_theme,
            "all_themes": all_themes,
            "themes_count": len(ts),
            "sector_changed": "1" if sector_changed else "0",
            "had_no_sector": "1" if had_no_sector else "0",
            "reason": reason,
            "primary_segment": seg.get("seg1_name") or "",
            "primary_segment_pct": seg.get("seg1_pct") or "",
        })

        counts["total"] += 1
        if ts:
            counts["had_themes"] += 1
        else:
            counts["no_themes"] += 1
        if sector_changed:
            counts["sector_changed"] += 1
            if had_no_sector:
                counts["had_no_sector_filled"] += 1
            else:
                counts["wrong_sector_corrected"] += 1
            drift_rows.append(full_rows[-1])
        else:
            counts["kept_same"] += 1

        sector_distribution_old[old_sector or "(blank)"] += 1
        sector_distribution_new[new_sector or "(blank)"] += 1

    # Write full v2 CSV
    with OUT_FULL.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in full_rows:
            w.writerow(r)

    # Write drift CSV
    with OUT_DRIFT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in drift_rows:
            w.writerow(r)

    # Write a stub overrides CSV (empty rows beyond header) for manual exceptions
    if not OUT_OVERRIDES.exists():
        with OUT_OVERRIDES.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "forced_sector", "reason", "added_by", "added_on"])
            w.writerow(["# Add stocks here whose v2 sector should override the theme-derived one.", "", "", "", ""])

    # Print summary
    print("=" * 64)
    print("Classification v2 — summary")
    print("=" * 64)
    print(f"  Total stocks scanned                {counts['total']:>5}")
    print(f"  Had ≥1 theme                        {counts['had_themes']:>5}")
    print(f"  No theme (kept revenue-path sector) {counts['no_themes']:>5}")
    print()
    print(f"  Sector changed                      {counts['sector_changed']:>5}")
    print(f"    ↳ blank → filled                  {counts['had_no_sector_filled']:>5}")
    print(f"    ↳ wrong → corrected               {counts['wrong_sector_corrected']:>5}")
    print(f"  Sector unchanged                    {counts['kept_same']:>5}")
    print()
    print("Top 10 sector shifts (old → new, count):")
    shifts: dict[tuple[str, str], int] = defaultdict(int)
    for r in drift_rows:
        shifts[(r["old_valvo_sector"] or "(blank)", r["new_valvo_sector"])] += 1
    for (old, new), n in sorted(shifts.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {old:<35} → {new:<35}  ({n})")
    print()
    print(f"Outputs:")
    print(f"  {OUT_FULL.relative_to(REPO)}")
    print(f"  {OUT_DRIFT.relative_to(REPO)}")
    print(f"  {OUT_OVERRIDES.relative_to(REPO)} (stub)")


if __name__ == "__main__":
    main()
