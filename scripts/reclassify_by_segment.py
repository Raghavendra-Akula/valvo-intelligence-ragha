"""
Revenue-Segment Driven Sector Reclassification
═══════════════════════════════════════════════

Classifies every active stock in `stock_universe` into one of Valvo's 20 canonical
parent sectors based on **where its revenue actually comes from** — as recorded in
`segments_quarterly` (XBRL-sourced per-quarter business-line revenue).

For each stock:
  1. Pull the latest 8 quarters of segment rows.
  2. Drop geographic/placeholder segments (India, Europe, Others, Unallocated, …).
  3. Keyword-match every remaining segment_name to a custom_sectors row
     (longest-keyword-wins via services.custom_sectors_service._best_match).
  4. Aggregate by parent_sector at the latest quarter — pick the majority as primary.
  5. If ≥8 quarters of history exist, compute TTM YoY growth for primary & secondary
     parents. Flag the secondary as "emerging" when:
         secondary.share ≥ 20%
         AND secondary.yoy_growth ≥ 25%
         AND secondary.yoy_growth ≥ 1.5 × primary.yoy_growth
  6. Stocks with no usable segment rows fall back to keyword-matching the company
     name + yfinance sector + industry.
  7. Writes three places:
        stock_universe.valvo_sector          — denormalized primary parent_sector
        stock_custom_sector                  — primary (+ optional secondary) rows
        custom_sector_classification_log     — audit trail, one row per stock

Run:
    python -m scripts.reclassify_by_segment                       # full run
    python -m scripts.reclassify_by_segment --dry-run --limit 50  # preview
    python -m scripts.reclassify_by_segment --symbols RELIANCE,INFY --dry-run

Options:
    --dry-run           print decisions, don't write
    --limit N           stop after N stocks
    --symbols A,B,C     only process the listed NSE symbols (debugging)
    --skip-manual       respect rows where stock_custom_sector.source='manual'
    --refresh-keywords  re-seed the taxonomy before classifying
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

# Make Backend/* importable when run as `python -m scripts.reclassify_by_segment`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.database import get_db, close_db  # noqa: E402
from services.custom_sectors_service import _best_match, _norm, seed_taxonomy  # noqa: E402


# ─── Config ─────────────────────────────────────────────────────────────────

QUARTERS_TO_FETCH = 8
MIN_QUARTERS_FOR_GROWTH = 8
MIN_PRIMARY_PCT = 40.0            # below this we still pick top parent but with confidence 0.5
EMERGING_PCT_THRESHOLD = 20.0     # secondary must carry at least this share of latest revenue
EMERGING_GROWTH_THRESHOLD = 25.0  # secondary YoY growth (%) required to flag as emerging
EMERGING_GROWTH_RATIO = 1.5       # secondary must grow at least 1.5× primary's growth
MIN_TTM_BASE_CR = 10.0            # drop growth calc if prior-TTM revenue was <₹10 Cr
BATCH_SIZE = 200

# Segment names that should NOT contribute to sector classification.
# Geographic dimensions and placeholders only — everything else we try to map.
_GEO_PATTERNS = [
    r"^india$",
    r"^europe$",
    r"^america[s]?$",
    r"^asia$",
    r"^china$",
    r"^japan$",
    r"^uk$",
    r"^usa$",
    r"^united states( of america)?$",
    r"^united kingdom$",
    r"^middle east$",
    r"^africa$",
    r"^domestic( market)?$",
    r"^international$",
    r"^overseas$",
    r"^export[s]?$",
    r"^rest of[- ]the[- ]world$",
    r"^row$",
    r"^pan[- ]india$",
    r"^within india$",
    r"^outside india$",
    r"^out of india$",
    r"^apac$",
    r"^emea$",
]
_PLACEHOLDER_PATTERNS = [
    r"^other[s]?$",
    r"^unallocated$",
    r"^single segment$",
    r"^segment revenue$",
    r"^segment results$",
    r"^total$",
    r"^consolidated$",
    r"^standalone$",
    r"^inter[- ]segment$",
    r"^elimination[s]?$",
    r"^miscellaneous$",
    r"^n/a$",
    r"^not applicable$",
    r"^not reported$",
]
_DROP_RE = re.compile(
    "|".join([f"(?:{p})" for p in (_GEO_PATTERNS + _PLACEHOLDER_PATTERNS)]),
    re.IGNORECASE,
)


def is_droppable_segment(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return True
    return bool(_DROP_RE.match(n))


# ─── Data loading ───────────────────────────────────────────────────────────

def load_rules(cur) -> list[tuple[int, str, str, list[str]]]:
    """Returns [(custom_sector_id, slug, parent_sector, keywords), ...]"""
    cur.execute(
        "SELECT id, slug, parent_sector, keywords FROM custom_sectors WHERE is_active = TRUE"
    )
    rules = []
    for r in cur.fetchall():
        raw = r["keywords"]
        kw = raw if isinstance(raw, list) else json.loads(raw or "[]")
        rules.append((r["id"], r["slug"], r["parent_sector"], kw))
    return rules


def load_stocks(cur, symbols: Optional[list[str]] = None) -> list[dict]:
    if symbols:
        cur.execute(
            """SELECT security_id, symbol, company_name, sector, industry
                 FROM stock_universe
                WHERE symbol = ANY(%s)
             ORDER BY symbol""",
            (symbols,),
        )
    else:
        cur.execute(
            """SELECT security_id, symbol, company_name, sector, industry
                 FROM stock_universe
                WHERE COALESCE(is_active, true) = true
             ORDER BY symbol"""
        )
    return [dict(r) for r in cur.fetchall()]


def load_segments(cur) -> dict[str, list[dict]]:
    """Return {security_id: [rows...]} ordered by period_end_date DESC.
    Only picks latest QUARTERS_TO_FETCH distinct periods per stock.
    """
    cur.execute(
        """
        WITH ranked AS (
            SELECT s.security_id,
                   s.period_end_date,
                   s.segment_name,
                   s.segment_revenue_cr,
                   s.segment_revenue_pct,
                   s.is_consolidated,
                   DENSE_RANK() OVER (
                       PARTITION BY s.security_id
                       ORDER BY s.period_end_date DESC
                   ) AS period_rank
              FROM segments_quarterly s
             WHERE s.segment_name IS NOT NULL
               AND s.segment_name <> ''
               AND (s.segment_revenue_cr IS NOT NULL OR s.segment_revenue_pct IS NOT NULL)
        )
        SELECT security_id, period_end_date, segment_name,
               segment_revenue_cr, segment_revenue_pct
          FROM ranked
         WHERE period_rank <= %s
      ORDER BY security_id, period_end_date DESC, segment_name
        """,
        (QUARTERS_TO_FETCH,),
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for r in cur.fetchall():
        out[r["security_id"]].append(dict(r))
    return out


def load_manual_assignments(cur) -> set[str]:
    cur.execute(
        "SELECT DISTINCT security_id FROM stock_custom_sector WHERE source = 'manual'"
    )
    return {r["security_id"] for r in cur.fetchall()}


# ─── Classification logic ───────────────────────────────────────────────────

def classify_stock(
    stock: dict,
    segments: list[dict],
    rules: list[tuple[int, str, str, list[str]]],
) -> Optional[dict]:
    """Returns a decision dict or None if nothing could be classified.

    Decision shape:
        {
          primary: {sector_id, slug, parent_sector, latest_pct, confidence, note},
          secondary: {...}  # optional, only when "emerging"
          source: 'segment_revenue' | 'keyword_fallback',
          quarters_analyzed: n,
          dropped_segments: n,
          raw_log: str
        }
    """
    name = stock.get("company_name") or ""
    sector_str = stock.get("sector") or ""
    industry = stock.get("industry") or ""

    if not segments:
        return _fallback(stock, rules)

    # Group by period_end_date
    by_period: dict = defaultdict(list)
    for row in segments:
        if is_droppable_segment(row["segment_name"]):
            continue
        by_period[row["period_end_date"]].append(row)

    if not by_period:
        return _fallback(stock, rules)

    periods = sorted(by_period.keys(), reverse=True)
    latest_period = periods[0]
    latest_rows = by_period[latest_period]

    # Map each latest-period segment to a parent_sector
    def match_one(segment_name: str) -> Optional[tuple[int, str, str]]:
        text = _norm(f"{segment_name} {name} {industry}")
        m = _best_match(text, [(sid, slug, kw) for (sid, slug, _p, kw) in rules])
        if not m:
            return None
        sid, matched_kw = m
        parent = next(p for (i, _s, p, _k) in rules if i == sid)
        slug = next(s for (i, s, _p, _k) in rules if i == sid)
        return (sid, slug, parent)

    # Aggregate latest quarter by parent_sector
    parent_rev_cr: dict[str, float] = defaultdict(float)
    parent_rev_pct: dict[str, float] = defaultdict(float)
    parent_best_match: dict[str, tuple[int, str]] = {}  # parent → (sector_id, slug)
    unmatched = 0
    for row in latest_rows:
        match = match_one(row["segment_name"])
        if not match:
            unmatched += 1
            continue
        sid, slug, parent = match
        rev_cr = float(row["segment_revenue_cr"] or 0)
        rev_pct = float(row["segment_revenue_pct"] or 0)
        parent_rev_cr[parent] += rev_cr
        parent_rev_pct[parent] += rev_pct
        # Keep the *first* sub-sector slug we see for this parent; later refinement
        # would store all sub-sectors but this is enough for primary tagging.
        parent_best_match.setdefault(parent, (sid, slug))

    if not parent_rev_cr:
        return _fallback(stock, rules)

    # Share per parent
    total_cr = sum(parent_rev_cr.values())
    total_pct = sum(parent_rev_pct.values())

    if total_cr <= 0 and total_pct <= 0:
        return _fallback(stock, rules)

    shares: dict[str, float] = {}
    for parent, cr in parent_rev_cr.items():
        if total_cr > 0:
            shares[parent] = 100.0 * cr / total_cr
        else:
            shares[parent] = parent_rev_pct.get(parent, 0) / (total_pct or 1) * 100.0

    ranked = sorted(shares.items(), key=lambda kv: kv[1], reverse=True)
    primary_parent, primary_share = ranked[0]
    primary_sid, primary_slug = parent_best_match[primary_parent]

    confidence = 1.0 if primary_share >= MIN_PRIMARY_PCT else 0.5
    note_parts = [f"rev_share:{primary_share:.1f}%"]
    if primary_share < MIN_PRIMARY_PCT:
        note_parts.append("highly-diversified")

    decision = {
        "primary": {
            "sector_id": primary_sid,
            "slug": primary_slug,
            "parent_sector": primary_parent,
            "latest_pct": round(primary_share, 2),
            "confidence": confidence,
            "note": ", ".join(note_parts),
        },
        "source": "segment_revenue",
        "quarters_analyzed": len(by_period),
        "dropped_segments": sum(
            1 for r in segments if is_droppable_segment(r["segment_name"])
        ),
        "unmatched_latest": unmatched,
    }

    # Emerging secondary detection — requires ≥8 quarters
    if len(by_period) >= MIN_QUARTERS_FOR_GROWTH and len(ranked) >= 2:
        secondary_parent, secondary_share = ranked[1]
        if secondary_share >= EMERGING_PCT_THRESHOLD:
            primary_growth = _ttm_yoy_growth(
                periods, by_period, primary_parent, match_one
            )
            secondary_growth = _ttm_yoy_growth(
                periods, by_period, secondary_parent, match_one
            )
            if (
                secondary_growth is not None
                and secondary_growth >= EMERGING_GROWTH_THRESHOLD
                and (
                    primary_growth is None
                    or secondary_growth >= EMERGING_GROWTH_RATIO * (primary_growth or 0.01)
                )
            ):
                sec_sid, sec_slug = parent_best_match[secondary_parent]
                decision["secondary"] = {
                    "sector_id": sec_sid,
                    "slug": sec_slug,
                    "parent_sector": secondary_parent,
                    "latest_pct": round(secondary_share, 2),
                    "yoy_growth": round(secondary_growth, 1),
                    "primary_growth": (
                        round(primary_growth, 1) if primary_growth is not None else None
                    ),
                    "note": f"emerging rev_share:{secondary_share:.1f}% yoy:{secondary_growth:.1f}%",
                }

    return decision


def _ttm_yoy_growth(
    periods: list,
    by_period: dict,
    parent: str,
    match_fn,
) -> Optional[float]:
    """Compute (TTM recent − TTM prior) / TTM prior for a given parent_sector.
    Uses periods[0..3] (recent) and periods[4..7] (prior).
    """
    if len(periods) < 8:
        return None
    recent = _sum_parent_revenue(periods[0:4], by_period, parent, match_fn)
    prior = _sum_parent_revenue(periods[4:8], by_period, parent, match_fn)
    if prior is None or prior < MIN_TTM_BASE_CR:
        return None
    if recent is None:
        return None
    return 100.0 * (recent - prior) / prior


def _sum_parent_revenue(periods, by_period, parent, match_fn) -> Optional[float]:
    total = 0.0
    seen = False
    for p in periods:
        for row in by_period.get(p, []):
            m = match_fn(row["segment_name"])
            if m and m[2] == parent:
                rev = row["segment_revenue_cr"]
                if rev is not None:
                    total += float(rev)
                    seen = True
    return total if seen else None


def _fallback(stock: dict, rules) -> Optional[dict]:
    """No usable segment data → keyword-match company_name + sector + industry."""
    name = stock.get("company_name") or ""
    sector_str = stock.get("sector") or ""
    industry = stock.get("industry") or ""
    text = _norm(f"{name} {sector_str} {industry}")
    if not text:
        return None
    m = _best_match(text, [(sid, slug, kw) for (sid, slug, _p, kw) in rules])
    if not m:
        return None
    sid, matched_kw = m
    parent = next(p for (i, _s, p, _k) in rules if i == sid)
    slug = next(s for (i, s, _p, _k) in rules if i == sid)
    return {
        "primary": {
            "sector_id": sid,
            "slug": slug,
            "parent_sector": parent,
            "latest_pct": 0.0,
            "confidence": min(0.6, 0.35 + 0.04 * len(matched_kw)),
            "note": f"keyword-fallback matched:{matched_kw}",
        },
        "source": "keyword_fallback",
        "quarters_analyzed": 0,
        "dropped_segments": 0,
        "unmatched_latest": 0,
    }


# ─── Persistence ────────────────────────────────────────────────────────────

def upsert_decision(
    cur,
    security_id: str,
    decision: dict,
    top_segments_note: str = "",
) -> None:
    primary = decision["primary"]

    cur.execute(
        "DELETE FROM stock_custom_sector WHERE security_id = %s AND source <> 'manual'",
        (security_id,),
    )
    cur.execute(
        """
        INSERT INTO stock_custom_sector
            (security_id, custom_sector_id, source, confidence, is_primary, note)
        VALUES (%s, %s, %s, %s, TRUE, %s)
        ON CONFLICT (security_id, custom_sector_id) DO UPDATE
            SET source     = EXCLUDED.source,
                confidence = EXCLUDED.confidence,
                is_primary = TRUE,
                note       = EXCLUDED.note,
                updated_at = NOW()
        """,
        (
            security_id,
            primary["sector_id"],
            decision["source"],
            primary["confidence"],
            (primary["note"] + (" | " + top_segments_note if top_segments_note else ""))[:500],
        ),
    )

    secondary = decision.get("secondary")
    if secondary and secondary["sector_id"] != primary["sector_id"]:
        cur.execute(
            """
            INSERT INTO stock_custom_sector
                (security_id, custom_sector_id, source, confidence, is_primary, note)
            VALUES (%s, %s, %s, %s, FALSE, %s)
            ON CONFLICT (security_id, custom_sector_id) DO UPDATE
                SET source     = EXCLUDED.source,
                    confidence = EXCLUDED.confidence,
                    is_primary = FALSE,
                    note       = EXCLUDED.note,
                    updated_at = NOW()
            """,
            (
                security_id,
                secondary["sector_id"],
                decision["source"] + "_emerging",
                0.7,
                secondary["note"][:500],
            ),
        )

    cur.execute(
        "UPDATE stock_universe SET valvo_sector = %s WHERE security_id = %s",
        (primary["parent_sector"], security_id),
    )

    cur.execute(
        """INSERT INTO custom_sector_classification_log
              (security_id, custom_sector_id, source, confidence, matched_keyword, raw_input)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            security_id,
            primary["sector_id"],
            decision["source"],
            primary["confidence"],
            primary["slug"],
            json.dumps({
                "parent": primary["parent_sector"],
                "latest_pct": primary["latest_pct"],
                "quarters": decision["quarters_analyzed"],
                "secondary": secondary,
                "note": primary["note"],
            })[:2000],
        ),
    )


def log_unclassified(cur, security_id: str, note: str) -> None:
    cur.execute(
        """INSERT INTO custom_sector_classification_log
              (security_id, custom_sector_id, source, confidence, matched_keyword, raw_input)
           VALUES (%s, NULL, 'segment_revenue', 0.000, NULL, %s)""",
        (security_id, note[:2000]),
    )


# ─── Orchestration ──────────────────────────────────────────────────────────

def top_segments_summary(segments: list[dict]) -> str:
    """Short human-readable summary of the top 3 latest-period segments."""
    if not segments:
        return ""
    latest_period = max(r["period_end_date"] for r in segments)
    latest = [
        r for r in segments
        if r["period_end_date"] == latest_period
        and not is_droppable_segment(r["segment_name"])
    ]
    latest.sort(key=lambda r: (r["segment_revenue_cr"] or 0), reverse=True)
    top = latest[:3]
    parts = []
    for r in top:
        name = r["segment_name"]
        pct = r["segment_revenue_pct"]
        parts.append(f"{name}({pct:.0f}%)" if pct is not None else name)
    return "top: " + ", ".join(parts)


def run(
    dry_run: bool = False,
    limit: Optional[int] = None,
    symbols: Optional[list[str]] = None,
    skip_manual: bool = True,
    refresh_keywords: bool = False,
) -> dict:
    if refresh_keywords:
        print("→ Reseeding taxonomy …")
        seed_taxonomy()

    conn = get_db()
    summary = {
        "scanned": 0,
        "classified_by_segment": 0,
        "classified_by_fallback": 0,
        "unclassified": 0,
        "emerging_flagged": 0,
        "skipped_manual": 0,
        "by_parent_sector": defaultdict(int),
        "unmatched_segments": defaultdict(int),
    }
    try:
        cur = conn.cursor()

        t0 = time.time()
        print("→ Loading taxonomy …")
        rules = load_rules(cur)
        print(f"  {len(rules)} sub-sectors")

        print("→ Loading stocks …")
        stocks = load_stocks(cur, symbols=symbols)
        if limit:
            stocks = stocks[:limit]
        print(f"  {len(stocks)} stocks to process")

        print("→ Loading segments (all stocks, latest 8 quarters) …")
        segments = load_segments(cur)
        n_seg_rows = sum(len(v) for v in segments.values())
        print(f"  {len(segments)} stocks with segment data, {n_seg_rows} rows")

        manual_ids: set[str] = set()
        if skip_manual:
            manual_ids = load_manual_assignments(cur)
            print(f"  {len(manual_ids)} stocks pinned by manual assignment (will skip)")

        t_load = time.time() - t0
        print(f"  loaded in {t_load:.1f}s\n")

        t0 = time.time()
        for i, stock in enumerate(stocks, 1):
            sid = stock["security_id"]
            summary["scanned"] += 1

            if sid in manual_ids:
                summary["skipped_manual"] += 1
                continue

            stock_segs = segments.get(sid, [])
            decision = classify_stock(stock, stock_segs, rules)

            if not decision:
                summary["unclassified"] += 1
                if not dry_run:
                    log_unclassified(
                        cur,
                        sid,
                        f"no match: name={stock.get('company_name')} "
                        f"sector={stock.get('sector')} segs={len(stock_segs)}",
                    )
                if dry_run:
                    print(f"  [SKIP] {stock['symbol']:<12} → no classification ({len(stock_segs)} segs)")
                continue

            if decision["source"] == "segment_revenue":
                summary["classified_by_segment"] += 1
            else:
                summary["classified_by_fallback"] += 1

            if decision.get("secondary"):
                summary["emerging_flagged"] += 1

            summary["by_parent_sector"][decision["primary"]["parent_sector"]] += 1

            if dry_run:
                prim = decision["primary"]
                sec = decision.get("secondary")
                tag = "SEG" if decision["source"] == "segment_revenue" else "KW "
                line = (
                    f"  [{tag}] {stock['symbol']:<12} → "
                    f"{prim['parent_sector']} ({prim['latest_pct']:.0f}%, "
                    f"conf={prim['confidence']:.2f})"
                )
                if sec:
                    line += f"  ↗ emerging: {sec['parent_sector']} ({sec['latest_pct']:.0f}%, yoy={sec['yoy_growth']:.0f}%)"
                print(line)
            else:
                upsert_decision(
                    cur,
                    sid,
                    decision,
                    top_segments_note=top_segments_summary(stock_segs),
                )

            if not dry_run and i % BATCH_SIZE == 0:
                conn.commit()
                print(
                    f"  … committed {i}/{len(stocks)} "
                    f"({i/len(stocks)*100:.0f}%, {(time.time()-t0):.1f}s)"
                )

        if not dry_run:
            conn.commit()

    finally:
        close_db(conn)

    summary["by_parent_sector"] = dict(summary["by_parent_sector"])
    summary["unmatched_segments"] = dict(summary["unmatched_segments"])
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated NSE symbols (for spot-checks)")
    parser.add_argument("--skip-manual", action="store_true", default=True)
    parser.add_argument("--refresh-keywords", action="store_true", default=False)
    args = parser.parse_args()

    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    t0 = time.time()
    summary = run(
        dry_run=args.dry_run,
        limit=args.limit,
        symbols=symbols,
        skip_manual=args.skip_manual,
        refresh_keywords=args.refresh_keywords,
    )
    elapsed = time.time() - t0

    print("\n" + "═" * 60)
    print(f"{'DRY RUN' if args.dry_run else 'RECLASSIFY'} — Summary")
    print("═" * 60)
    print(f"  scanned                : {summary['scanned']}")
    print(f"  classified by segment  : {summary['classified_by_segment']}")
    print(f"  classified by fallback : {summary['classified_by_fallback']}")
    print(f"  unclassified           : {summary['unclassified']}")
    print(f"  emerging flagged       : {summary['emerging_flagged']}")
    print(f"  skipped (manual)       : {summary['skipped_manual']}")
    print(f"  elapsed                : {elapsed:.1f}s")
    print("\n  by_parent_sector:")
    for parent, n in sorted(
        summary["by_parent_sector"].items(), key=lambda kv: kv[1], reverse=True
    ):
        print(f"    {parent:<35} {n}")

    # Persist summary to logs/
    if not args.dry_run:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        out_path = os.path.join(log_dir, f"reclassify_{ts}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"\n  summary written → {out_path}")


if __name__ == "__main__":
    main()
