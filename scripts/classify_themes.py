"""
Theme classification CLI — batch driver for themes_service.classify_all.

Shape matches `scripts/reclassify_by_segment.py` so the same muscle memory
applies (dry-run first, then full pass, then --commit).

Run:
    # Dry-run a sample → stdout + CSV
    python -m scripts.classify_themes --dry-run --limit 50

    # Dry-run specific symbols
    python -m scripts.classify_themes --symbols NETWEB,VOLTAMP,KRN --dry-run

    # Full dry-run writing CSV for review (no DB writes)
    python -m scripts.classify_themes --dry-run \
        --csv ../docs/stock_theme_classification.csv

    # Full dry-run with web overrides merged
    python -m scripts.classify_themes --dry-run \
        --web-overrides ../docs/theme_research_verified.csv \
        --csv ../docs/stock_theme_classification.csv

    # Commit to DB (respects manual + web_verified rows)
    python -m scripts.classify_themes --commit

Flags:
    --dry-run              print decisions + optionally write CSV, no DB writes
    --commit               write to stock_themes (mutually exclusive with --dry-run)
    --limit N              stop after N stocks
    --symbols A,B,C        only process the listed NSE symbols
    --skip-manual          (default ON) respect source='manual' rows
    --skip-web-verified    (default ON) respect source='web_verified' rows
    --web-overrides FILE   merge a web-verified CSV into the decisions
    --csv FILE             write full decision table to this path
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from services.themes_service import classify_all  # noqa: E402


def _load_web_overrides(path: str) -> dict[str, list[dict]]:
    """Parse the web-verified CSV:
        symbol,company_name,theme_slug,exposure,confidence,evidence_url,note

    Returns {UPPER_SYMBOL: [{theme_slug, exposure, confidence, evidence_url, note}]}.
    """
    out: dict[str, list[dict]] = defaultdict(list)
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = (row.get("symbol") or "").strip().upper()
            slug = (row.get("theme_slug") or "").strip()
            if not sym or not slug:
                continue
            out[sym].append({
                "theme_slug": slug,
                "exposure": float(row.get("exposure") or 0.9),
                "confidence": float(row.get("confidence") or 0.9),
                "evidence_url": (row.get("evidence_url") or "").strip() or None,
                "note": (row.get("note") or "").strip() or None,
            })
    return out


def _write_decisions_csv(decisions: list[dict], path: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "theme_slug", "exposure_score", "source",
            "is_primary", "confidence", "matched_term",
            "evidence_url", "evidence_note",
        ])
        for d in decisions:
            sym = d["symbol"]
            if not d["themes"]:
                w.writerow([sym, "", "", "", "", "", "", "", ""])
                continue
            for t in d["themes"]:
                w.writerow([
                    sym,
                    t.get("theme_slug", ""),
                    t.get("exposure_score", ""),
                    t.get("source", ""),
                    "1" if t.get("is_primary") else "0",
                    t.get("confidence", ""),
                    t.get("matched_term") or "",
                    t.get("evidence_url") or "",
                    t.get("evidence_note") or "",
                ])


def main():
    p = argparse.ArgumentParser(description="Batch-classify stocks into themes.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print decisions, don't write to DB (default).")
    p.add_argument("--commit", action="store_true",
                   help="Write to stock_themes. Mutually exclusive with --dry-run.")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--symbols", type=str, default=None,
                   help="Comma-separated list of NSE symbols to restrict to.")
    p.add_argument("--no-skip-manual", action="store_true",
                   help="Overwrite rows with source='manual' (dangerous).")
    p.add_argument("--no-skip-web-verified", action="store_true",
                   help="Overwrite rows with source='web_verified'.")
    p.add_argument("--web-overrides", type=str, default=None,
                   help="Path to web-verified CSV to merge.")
    p.add_argument("--csv", type=str, default=None,
                   help="Write full decision table to this CSV.")
    args = p.parse_args()

    if args.dry_run and args.commit:
        print("ERROR: --dry-run and --commit are mutually exclusive.", file=sys.stderr)
        sys.exit(2)

    dry_run = not args.commit  # default behavior: dry-run unless --commit
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    overrides = _load_web_overrides(args.web_overrides) if args.web_overrides else None

    if overrides:
        print(f"web overrides loaded: {len(overrides)} symbols")

    result = classify_all(
        limit=args.limit,
        symbols=symbols,
        dry_run=dry_run,
        skip_manual=not args.no_skip_manual,
        skip_web_verified=not args.no_skip_web_verified,
        web_overrides=overrides,
    )

    mode = "DRY RUN" if dry_run else "COMMIT"
    print(f"\n[{mode}] scanned={result['scanned']} assigned={result['assigned']} "
          f"no-theme={result['stocks_no_theme']} rows_written={result.get('themes_written', 0)}")

    if args.csv and dry_run:
        _write_decisions_csv(result["decisions"], args.csv)
        print(f"wrote decisions → {args.csv}")

    # Spot-log a few
    if dry_run and result["decisions"]:
        print("\nSample decisions (first 15 with themes):")
        shown = 0
        for d in result["decisions"]:
            if not d["themes"]:
                continue
            ts = [f"{t['theme_slug']}({t['exposure_score']:.2f}/{t['source']})" for t in d["themes"]]
            print(f"  {d['symbol']:<14}  {' , '.join(ts)}")
            shown += 1
            if shown >= 15:
                break


if __name__ == "__main__":
    main()
