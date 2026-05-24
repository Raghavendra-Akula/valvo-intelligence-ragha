"""
sync_classification_v2_to_db.py — Push v2 classifications into stock_universe.

Reads docs/stock_classification_v2.csv and updates stock_universe.valvo_sector
to match the v2 column (`new_valvo_sector`). Before any update happens, the
current valvo_sector is preserved in a new column `valvo_sector_v1` so we
can revert if anything goes wrong.

Modes
-----
  --dry-run  (default)  Print what would change. No writes.
  --apply               Actually run the migration.

Safety
------
  * `valvo_sector_v1` is created if missing and back-filled from the
    current `valvo_sector` exactly ONCE per row (subsequent runs leave
    pre-existing v1 values alone — so the original snapshot is preserved
    even across multiple syncs).
  * The whole update runs in a single transaction.
  * Symbols in the DB that aren't in the CSV are left untouched (we never
    blank a sector we don't have a v2 answer for).
  * Symbols in the CSV that aren't in the DB are reported as orphans.

Run from repo root
------------------
    PYTHONPATH=Backend python3 Backend/scripts/sync_classification_v2_to_db.py            # dry run
    PYTHONPATH=Backend python3 Backend/scripts/sync_classification_v2_to_db.py --apply
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve()
BACKEND = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(BACKEND))

from database.database import close_db, get_db  # noqa: E402

CSV_PATH = REPO / "docs" / "stock_classification_v2.csv"


def load_v2_csv() -> dict[str, str]:
    """{symbol: new_valvo_sector} from the v2 CSV."""
    out: dict[str, str] = {}
    with CSV_PATH.open() as f:
        for row in csv.DictReader(f):
            sym = (row.get("symbol") or "").strip().upper()
            sec = (row.get("new_valvo_sector") or "").strip()
            if sym and sec:
                out[sym] = sec
    return out


def fetch_current(conn) -> tuple[dict[str, dict], bool]:
    """{symbol: {valvo_sector, valvo_sector_v1?}} from the DB. Pool returns RealDict rows."""
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        # valvo_sector_v1 may not exist yet; pull defensively.
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='public' AND table_name='stock_universe'
              AND column_name='valvo_sector_v1'
        """)
        has_backup = cur.fetchone() is not None

        if has_backup:
            cur.execute("SELECT symbol, valvo_sector, valvo_sector_v1 FROM stock_universe")
            for r in cur.fetchall():
                out[r["symbol"].upper()] = {
                    "valvo_sector": r["valvo_sector"],
                    "valvo_sector_v1": r["valvo_sector_v1"],
                }
        else:
            cur.execute("SELECT symbol, valvo_sector FROM stock_universe")
            for r in cur.fetchall():
                out[r["symbol"].upper()] = {
                    "valvo_sector": r["valvo_sector"],
                    "valvo_sector_v1": None,
                }
    return out, has_backup


def diff(v2: dict[str, str], db: dict[str, dict]) -> dict:
    """Compute what would change."""
    changes: list[tuple[str, str, str]] = []   # (sym, old, new)
    unchanged = 0
    csv_orphans: list[str] = []                # in CSV, not in DB
    db_only: list[str] = []                    # in DB, not in CSV (left as-is)

    for sym, new_sec in v2.items():
        if sym not in db:
            csv_orphans.append(sym)
            continue
        old_sec = db[sym]["valvo_sector"] or ""
        if old_sec == new_sec:
            unchanged += 1
        else:
            changes.append((sym, old_sec, new_sec))

    for sym in db:
        if sym not in v2:
            db_only.append(sym)

    # categorise the changes for reporting
    transitions = Counter((old or "(blank)", new) for _, old, new in changes)

    return {
        "changes": changes,
        "unchanged": unchanged,
        "csv_orphans": csv_orphans,
        "db_only": db_only,
        "transitions": transitions,
    }


def print_summary(d: dict, has_backup: bool) -> None:
    print("=" * 64)
    print("Classification v2 → stock_universe.valvo_sector")
    print("=" * 64)
    print(f"  v1 backup column already present : {has_backup}")
    print(f"  Rows that would change           : {len(d['changes'])}")
    print(f"  Rows already aligned             : {d['unchanged']}")
    print(f"  Symbols in CSV but not in DB     : {len(d['csv_orphans'])}")
    print(f"  Symbols in DB but not in CSV     : {len(d['db_only'])}")
    print()
    print("Top 15 transitions (old → new, count):")
    for (old, new), n in sorted(d["transitions"].items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {old:<35} → {new:<35}  ({n})")
    print()
    print("Sample of 25 rows that would change:")
    for sym, old, new in d["changes"][:25]:
        print(f"  {sym:<14} {old or '(blank)':<32} → {new}")
    if len(d["changes"]) > 25:
        print(f"  ... +{len(d['changes']) - 25} more")
    if d["csv_orphans"]:
        print()
        print(f"CSV orphans (first 10 of {len(d['csv_orphans'])}): "
              f"{', '.join(d['csv_orphans'][:10])}")
    if d["db_only"]:
        print()
        print(f"DB-only symbols left untouched (first 10 of {len(d['db_only'])}): "
              f"{', '.join(d['db_only'][:10])}")


def apply_changes(conn, changes: list[tuple[str, str, str]], has_backup: bool) -> None:
    """Run the migration in one transaction."""
    if not changes:
        print("No changes to apply.")
        return

    try:
        with conn.cursor() as cur:
            # 1. Add backup column if it doesn't exist (idempotent)
            if not has_backup:
                cur.execute("""
                    ALTER TABLE stock_universe
                    ADD COLUMN IF NOT EXISTS valvo_sector_v1 TEXT
                """)
                print("✓ Added valvo_sector_v1 column")

            # 2. Backfill v1 backup ONCE per row.
            #    Only fill rows where v1 is still NULL — preserves the
            #    original snapshot across re-runs.
            cur.execute("""
                UPDATE stock_universe
                SET valvo_sector_v1 = valvo_sector
                WHERE valvo_sector_v1 IS NULL
            """)
            backfilled = cur.rowcount
            print(f"✓ Back-filled {backfilled} rows into valvo_sector_v1")

            # 3. Update valvo_sector for each changed row.
            #    Use executemany for safety; psycopg2 transaction pool
            #    handles this fine in transaction mode.
            cur.executemany(
                "UPDATE stock_universe SET valvo_sector = %s WHERE symbol = %s",
                [(new, sym) for sym, _, new in changes],
            )
            print(f"✓ Updated valvo_sector on {len(changes)} rows")

        conn.commit()
        print("✓ Transaction committed")
    except Exception as e:
        conn.rollback()
        print(f"✗ Error — rolled back: {e}")
        raise


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually run the migration. Default is dry-run.")
    args = p.parse_args()

    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found. Run build_classification_v2.py first.",
              file=sys.stderr)
        sys.exit(1)

    v2 = load_v2_csv()
    conn = get_db()
    try:
        db, has_backup = fetch_current(conn)
        d = diff(v2, db)

        print_summary(d, has_backup)

        if args.apply:
            print()
            print("=" * 64)
            print("APPLYING CHANGES")
            print("=" * 64)
            apply_changes(conn, d["changes"], has_backup)
        else:
            print()
            print("Dry run only. To apply: --apply")
    finally:
        close_db(conn)


if __name__ == "__main__":
    main()
