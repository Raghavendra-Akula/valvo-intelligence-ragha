"""
audit_stock_universe_resolution.py — Integration check for the AI stock resolver.

After we hard-stopped _create_position when security_id can't be resolved,
we need to prove the resolver doesn't reject legitimate stocks. This
script loops over every active row in stock_universe and tries to
resolve it via _resolve_stock_reference (the same function the AI's
create_position path calls) using:

  1. The row's own symbol as the hint ("RELIANCE")
  2. The row's own company_name as the hint ("Reliance Industries Ltd")
  3. The company_name with " Limited" / " Ltd" / " Ltd." stripped —
     common LLM normalisation before tool call.

For each case we report:
  - hard fail: resolver returned None → creating a position with that
    exact stock_name would trigger unresolvable_security
  - wrong match: resolver returned a different security_id than the
    row's own → the AI would create a position against a different stock

Run from the repo root:

    PYTHONPATH=Backend python3 Backend/scripts/audit_stock_universe_resolution.py

Env needed: whatever your app normally uses to reach Supabase
(DATABASE_URL or the split SUPABASE_* vars). This script does NOT
mutate anything — pure SELECTs against stock_universe.
"""
from __future__ import annotations

import os
import sys
import time

# Make the Backend/services imports resolve regardless of cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from database.database import close_db, get_db  # noqa: E402
from services.valvo_ai_v2.catalog import _resolve_stock_reference  # noqa: E402


def _variants(company_name: str) -> list[str]:
    """LLM-style variants a caller might send as stock_name."""
    name = (company_name or "").strip()
    if not name:
        return []
    out = [name]
    # Strip common suffixes the LLM often drops.
    for suffix in (" Limited", " Ltd.", " Ltd", " LTD", " LIMITED"):
        if name.endswith(suffix):
            out.append(name[: -len(suffix)].strip())
            break
    return out


def main() -> int:
    conn = get_db()
    if not conn:
        print("ERROR: could not open DB — check SUPABASE_* / DATABASE_URL env")
        return 2

    cur = conn.cursor()
    cur.execute(
        """
        SELECT security_id, symbol, company_name
        FROM stock_universe
        WHERE is_active = true
        ORDER BY symbol ASC
        """
    )
    rows = cur.fetchall() or []
    total = len(rows)
    print(f"Auditing {total} active stock_universe rows…")

    hard_fails: list[dict] = []
    wrong_match: list[dict] = []
    started = time.time()

    for i, row in enumerate(rows, 1):
        sid_expected = str(row["security_id"])
        symbol = row["symbol"] or ""
        company = row["company_name"] or ""

        candidates: list[tuple[str, str]] = [("symbol", symbol)]
        for v in _variants(company):
            candidates.append(("company_name", v))

        for source, hint in candidates:
            if not hint:
                continue
            resolved = _resolve_stock_reference(cur, symbol_hint=hint)
            if not resolved or not resolved.get("security_id"):
                hard_fails.append({
                    "security_id": sid_expected,
                    "symbol": symbol,
                    "company_name": company,
                    "hint_source": source,
                    "hint_value": hint,
                })
                continue
            if str(resolved["security_id"]) != sid_expected:
                wrong_match.append({
                    "security_id_expected": sid_expected,
                    "security_id_got": str(resolved["security_id"]),
                    "symbol_expected": symbol,
                    "symbol_got": resolved.get("symbol"),
                    "company_expected": company,
                    "company_got": resolved.get("company_name"),
                    "hint_source": source,
                    "hint_value": hint,
                })

        if i % 500 == 0:
            elapsed = time.time() - started
            print(f"  …{i}/{total} rows checked ({elapsed:.1f}s)")

    close_db(conn)

    print("\n" + "=" * 72)
    print(f"HARD FAIL (resolver returned None)             : {len(hard_fails)}")
    print(f"WRONG MATCH (resolver picked a different row)  : {len(wrong_match)}")
    print("=" * 72)

    if hard_fails:
        print("\nFirst 20 hard fails (these trigger unresolvable_security):")
        for r in hard_fails[:20]:
            print(f"  - {r['symbol']:15s}  [{r['hint_source']:12s}]  hint={r['hint_value']!r}")

    if wrong_match:
        print("\nFirst 20 wrong matches (AI would create against a different stock):")
        for r in wrong_match[:20]:
            print(
                f"  - expected={r['symbol_expected']:12s} "
                f"got={r['symbol_got'] or '?':12s}  "
                f"[{r['hint_source']}] hint={r['hint_value']!r}"
            )

    # Exit 0 if clean, 1 if issues — easy to wire into CI later.
    return 0 if not (hard_fails or wrong_match) else 1


if __name__ == "__main__":
    raise SystemExit(main())
