"""
dump_universe_for_audit.py — One denormalized CSV per stock, ready for the
sector-by-sector audit (todo task 8) and the Uncategorized sweep (task 9).

Joins three sources:
  * docs/stock_classification_v2.csv     (v2 sector + primary theme + primary segment)
  * docs/stock_segments_master.csv       (up to 4 segments per stock with revenue %)
  * docs/stock_theme_classification.csv  (all themes per symbol — long format)

Outputs:
  docs/audit_v2/universe_audit.csv          single file, sorted (sector, -mcap)
  docs/audit_v2/by_sector/<Sector>.csv       one file per canonical sector
                                             (so we can review one bucket at a time)

Run from repo root:
    PYTHONPATH=Backend python3 Backend/scripts/dump_universe_for_audit.py
"""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]

DOCS = REPO / "docs"
V2_CSV = DOCS / "stock_classification_v2.csv"
SEG_CSV = DOCS / "stock_segments_master.csv"
THM_CSV = DOCS / "stock_theme_classification.csv"

OUT_DIR = DOCS / "audit_v2"
OUT_FULL = OUT_DIR / "universe_audit.csv"
OUT_BY_SEC = OUT_DIR / "by_sector"


def slugify(name: str) -> str:
    """Filesystem-safe sector name."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return s or "Unknown"


def load_v2() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with V2_CSV.open() as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip().upper()
            if sym:
                rows[sym] = r
    return rows


def load_segs() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with SEG_CSV.open() as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip().upper()
            if sym:
                rows[sym] = r
    return rows


def load_themes() -> dict[str, list[dict]]:
    """{symbol: [{theme_slug, is_primary, exposure_score}, ...]} sorted desc."""
    by: dict[str, list[dict]] = defaultdict(list)
    with THM_CSV.open() as f:
        for r in csv.DictReader(f):
            sym = (r.get("symbol") or "").strip().upper()
            slug = (r.get("theme_slug") or "").strip()
            if not sym or not slug:
                continue
            by[sym].append({
                "slug": slug,
                "is_primary": int(r.get("is_primary") or 0),
                "exposure": float(r.get("exposure_score") or 0.0),
            })
    for sym in by:
        by[sym].sort(key=lambda t: (t["is_primary"], t["exposure"]), reverse=True)
    return by


COLS = [
    "sector",                 # v2 sector currently in DB
    "symbol",
    "company_name",
    "mcap_cr",
    "old_sector",             # v1 sector we replaced
    "yahoo_sector",           # third-party label, useful sanity check
    "reason",                 # why v2 picked this sector (theme:foo / kept / override)
    "primary_theme",
    "all_themes",             # pipe-separated, primary first
    "themes_count",
    "seg1_name", "seg1_pct",
    "seg2_name", "seg2_pct",
    "seg3_name", "seg3_pct",
    "seg4_name", "seg4_pct",
    # Review-only columns (kept blank — for the human auditing batch-by-batch)
    "review_flag",            # "ok" / "wrong" / "uncertain"
    "suggested_sector",
    "audit_note",
]


def _mcap_num(r: dict) -> float:
    try:
        return float(r.get("mcap_cr") or 0)
    except ValueError:
        return 0.0


def main() -> None:
    for src in (V2_CSV, SEG_CSV, THM_CSV):
        if not src.exists():
            print(f"ERROR: missing {src}", file=sys.stderr)
            sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_BY_SEC.mkdir(parents=True, exist_ok=True)

    v2 = load_v2()
    segs = load_segs()
    themes = load_themes()

    out_rows: list[dict] = []
    for sym, vr in v2.items():
        sr = segs.get(sym, {})
        ts = themes.get(sym, [])
        primary = ts[0]["slug"] if ts else ""
        all_slugs = "|".join(t["slug"] for t in ts)

        out_rows.append({
            "sector": vr.get("new_valvo_sector", "") or "Uncategorized",
            "symbol": sym,
            "company_name": vr.get("company_name", "") or sr.get("company_name", ""),
            "mcap_cr": vr.get("mcap_cr", "") or sr.get("mcap_cr", ""),
            "old_sector": vr.get("old_valvo_sector", ""),
            "yahoo_sector": vr.get("yahoo_sector", ""),
            "reason": vr.get("reason", ""),
            "primary_theme": primary,
            "all_themes": all_slugs,
            "themes_count": str(len(ts)),
            "seg1_name": sr.get("seg1_name", ""),
            "seg1_pct":  sr.get("seg1_pct",  ""),
            "seg2_name": sr.get("seg2_name", ""),
            "seg2_pct":  sr.get("seg2_pct",  ""),
            "seg3_name": sr.get("seg3_name", ""),
            "seg3_pct":  sr.get("seg3_pct",  ""),
            "seg4_name": sr.get("seg4_name", ""),
            "seg4_pct":  sr.get("seg4_pct",  ""),
            "review_flag": "",
            "suggested_sector": "",
            "audit_note": "",
        })

    # Sort: sector A→Z, then mcap desc inside each sector
    out_rows.sort(key=lambda r: (r["sector"], -_mcap_num(r)))

    # Write the master file
    with OUT_FULL.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    # Write per-sector files (one per canonical bucket)
    by_sec: dict[str, list[dict]] = defaultdict(list)
    for r in out_rows:
        by_sec[r["sector"]].append(r)

    sector_summary: list[tuple[str, int, float]] = []
    for sec, rs in by_sec.items():
        path = OUT_BY_SEC / f"{slugify(sec)}.csv"
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            for r in rs:
                w.writerow(r)
        total_mcap = sum(_mcap_num(r) for r in rs)
        sector_summary.append((sec, len(rs), total_mcap))

    # Print summary
    sector_summary.sort(key=lambda t: -t[1])
    print("=" * 72)
    print(f"Universe audit dump — {len(out_rows)} stocks across {len(by_sec)} sectors")
    print("=" * 72)
    print(f"{'Sector':<35} {'Stocks':>7} {'Total Mcap (Cr)':>18}")
    print("-" * 72)
    for sec, n, mc in sector_summary:
        print(f"{sec:<35} {n:>7} {mc:>18,.0f}")
    print("-" * 72)
    print(f"\nMaster file : {OUT_FULL.relative_to(REPO)}")
    print(f"Per-sector  : {OUT_BY_SEC.relative_to(REPO)}/<Sector>.csv  ({len(by_sec)} files)")
    print()
    print("Next step: open one per-sector CSV at a time, scan top-mcap rows,")
    print("flag misclassifications in `review_flag` + `suggested_sector`,")
    print("then merge those rows back into docs/sector_overrides_v2.csv.")


if __name__ == "__main__":
    main()
