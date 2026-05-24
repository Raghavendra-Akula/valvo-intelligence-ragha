"""
audit_v2_anomalies.py — Flag stocks whose v2 sector contradicts their
segment-name keywords or company name.

Idea: Each canonical sector has unmistakable keyword signals in the
primary segment description. If a stock's v2 sector doesn't match those
signals, it's a candidate for an override.

This is a DETECTION tool, not a fix — every flagged stock still needs
a human eyeball before going into the override CSV.

Output: docs/audit_v2_anomalies.csv with columns:
    symbol, company, mcap_cr, current_v2_sector, suggested_sector,
    confidence, primary_segment, primary_theme

Run:
    python3 -m scripts.audit_v2_anomalies
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
SRC = REPO / "docs" / "stock_classification_v2.csv"
OUT = REPO / "docs" / "audit_v2_anomalies.csv"

# Each rule: (keyword pattern in lower-case segment, suggested_sector, confidence)
# Order matters — first match wins. Confidence: high/medium/low.
RULES: list[tuple[list[str], str, str]] = [
    # === Textiles & Apparel ===
    (["textile", "yarn", "spinning", "garment", "apparel", "fabric",
      "denim", "hosiery", "polyester", "knitting", "weaving",
      "cotton ginning", "cotton-ginning", "synthetic blended",
      "jute", "silk mill", "knit ", "fashion", "innerwear", "lingerie"],
     "Textiles & Apparel", "high"),
    # === Pharma & Healthcare ===
    (["pharma", "medicines", "drug", "api ", "formulation", "diagnostic",
      "hospital", "medical device", "clinical", "healthcare", "biotech"],
     "Pharma & Healthcare", "high"),
    # === Chemicals & Fertilizers ===
    (["fertiliz", "fertilis", "agrochem", "specialty chem",
      "industrial gas", "polymer", "plastics", "packaging material",
      "flexible packaging", "BOPET", "BOPP", "pvc", "agro chem"],
     "Chemicals & Fertilizers", "medium"),
    # === Metals & Mining ===
    (["iron ore", "iron and steel", "steel manufact", "steel pipes",
      "steel tubes", "steel forging", "tmt bars", "aluminum extrusion",
      "aluminium extrusion", "ferro alloy", "manganese", "zinc oxide",
      "copper "],
     "Metals & Mining", "medium"),
    # === Auto & Ancillary ===
    (["automotive", "auto components", "auto-component", "auto stamping",
      "tractor", "two wheel", "passenger vehicle", "commercial vehicle",
      "automotive air-condition", "camshaft", "auto cabl", "tyre"],
     "Auto & Ancillary", "high"),
    # === FMCG & Consumer ===
    (["alcoholic beverages", "country liquor", "spirits", "brewery",
      "ice cream", "dairy", "biscuit", "confection", "writing instrument",
      "stationery", "amusement park", "retail of "],
     "FMCG & Consumer", "high"),
    # === Cement & Building Materials ===
    (["cement", "ready mix concrete", "rmc ", "plywood", "mdf", "tiles",
      "ceramic tiles", "sanitary ware"],
     "Cement & Building Materials", "high"),
    # === IT & Technology ===
    (["software develop", "saas", "analytics solutions", "cybersecurity",
      "edtech", "fintech soft", "cloud platform", "digital signature"],
     "IT & Technology", "medium"),
    # === Power & Utilities ===
    (["power generation", "power transmission", "renewable energy",
      "wind energy", "solar power generation", "solar pv", "hydro power"],
     "Power & Utilities", "high"),
    # === Banks & Finance ===
    (["nbfc", "non-banking", "microfinance", "small finance bank",
      "housing finance", "lending"],
     "Banks & Finance", "high"),
    # === Agriculture & Allied ===
    (["seed compan", " seeds ", "sugar manufact", "agro commodity"],
     "Agriculture & Allied", "medium"),
    # === Defence & Aerospace ===
    (["defence", "ammunition", "explosive", "aerospace"],
     "Defence & Aerospace", "high"),
    # === Railways & Logistics ===
    (["logistics", "warehousing", "container freight", "shipping",
      "container terminal", "ports", "freight forward"],
     "Railways & Logistics", "medium"),
    # === Telecom & Media ===
    (["telecom equipment", "optical fibre", "broadcasting", "publishing"],
     "Telecom & Media", "medium"),
    # === Insurance ===
    (["life insurance", "general insurance", "health insurance"],
     "Insurance", "high"),
]


def _matches(blob: str, kw: str) -> bool:
    """Match keyword against blob with word boundaries on alphanumeric prefixes,
    so 'fabric' doesn't fire on 'fabrication' and 'api' doesn't fire on 'capital'.
    Multi-word phrases and patterns ending in space/hyphen fall back to substring."""
    kw = kw.strip()
    # If keyword has whitespace, hyphen, or trailing space — treat as substring
    if " " in kw or "-" in kw:
        return kw in blob
    # Otherwise enforce \b boundaries
    return re.search(r"\b" + re.escape(kw) + r"s?\b", blob) is not None


def suggest(seg: str, name: str, current: str) -> tuple[str, str] | None:
    """Return (suggested_sector, confidence) if a rule fires and
    contradicts current. Else None."""
    blob = (seg + " " + name).lower()
    for keywords, sector, conf in RULES:
        for kw in keywords:
            if _matches(blob, kw):
                if sector != current:
                    return (sector, conf)
                return None  # already correct
    return None


def main() -> None:
    if not SRC.exists():
        print(f"ERROR: {SRC} not found", file=sys.stderr)
        sys.exit(1)

    out_rows = []
    with SRC.open() as f:
        for r in csv.DictReader(f):
            sym = r["symbol"]
            cur = r["new_valvo_sector"]
            seg = r.get("primary_segment", "") or ""
            name = r.get("company_name", "") or ""
            sug = suggest(seg, name, cur)
            if sug is None:
                continue
            new, conf = sug
            out_rows.append({
                "symbol": sym,
                "company_name": name,
                "mcap_cr": r.get("mcap_cr", ""),
                "current_v2_sector": cur,
                "suggested_sector": new,
                "confidence": conf,
                "primary_segment": seg,
                "primary_theme": r.get("primary_theme_slug", ""),
            })

    # Sort by confidence (high first) then by mcap desc
    def _mcap(r):
        try:
            return float(r["mcap_cr"] or 0)
        except ValueError:
            return 0
    out_rows.sort(key=lambda r: ({"high": 0, "medium": 1, "low": 2}[r["confidence"]], -_mcap(r)))

    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()) if out_rows else [
            "symbol","company_name","mcap_cr","current_v2_sector",
            "suggested_sector","confidence","primary_segment","primary_theme",
        ])
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    # Summary
    from collections import Counter
    by_conf = Counter(r["confidence"] for r in out_rows)
    by_pair = Counter((r["current_v2_sector"], r["suggested_sector"]) for r in out_rows)

    print("=" * 64)
    print(f"Anomaly audit — {len(out_rows)} flagged across {len(set(r['symbol'] for r in out_rows))} stocks")
    print("=" * 64)
    for conf, n in by_conf.items():
        print(f"  {conf:<8} {n}")
    print()
    print("Top 20 (current → suggested, count):")
    for (cur, sug), n in by_pair.most_common(20):
        print(f"  {cur:<32} → {sug:<32}  ({n})")
    print()
    print(f"Output: {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
