"""
oracle_hit_rate.py — Measure how much real traffic the deterministic
portfolio_oracle is catching vs. routing to the LLM.

Why: we said reliability comes from two legs — the oracle short-circuit
and Flash for everything else. After a week of production traffic,
this script tells you whether the oracle is actually pulling its
weight, and if not, *which queries are slipping past it* so you know
exactly which intents to add next.

Usage:
    PYTHONPATH=Backend python3 Backend/scripts/oracle_hit_rate.py            # last 7 days
    PYTHONPATH=Backend python3 Backend/scripts/oracle_hit_rate.py 30         # last 30 days
    PYTHONPATH=Backend python3 Backend/scripts/oracle_hit_rate.py 7 --misses # also dump LLM-path messages

Reports:
  - Total turns, oracle hits, LLM turns
  - Hit rate as a percentage
  - Per-user breakdown if >1 user
  - Optional: a sample of the LLM-path user messages (the candidates
    for new oracle intents)

Pure SELECTs. No mutations.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from database.database import close_db, get_db  # noqa: E402


def main() -> int:
    days = 7
    show_misses = "--misses" in sys.argv
    for arg in sys.argv[1:]:
        if arg.isdigit():
            days = int(arg)

    conn = get_db()
    if not conn:
        print("ERROR: cannot open DB — check SUPABASE_* / DATABASE_URL env")
        return 2
    cur = conn.cursor()

    # 1. Overall split.
    cur.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE COALESCE(signals->>'path', 'llm') = 'oracle')  AS oracle_hits,
            COUNT(*) FILTER (WHERE COALESCE(signals->>'path', 'llm') = 'llm')     AS llm_turns,
            COUNT(*) AS total
        FROM ai_episodes
        WHERE created_at >= NOW() - make_interval(days => %s)
        """,
        (days,),
    )
    row = cur.fetchone()
    total = int(row["total"] or 0)
    hits = int(row["oracle_hits"] or 0)
    llm = int(row["llm_turns"] or 0)

    if total == 0:
        print(f"No episodes logged in the last {days} day(s). "
              "Either no traffic yet, or ai_episodes isn't being written.")
        close_db(conn)
        return 1

    rate = hits / total * 100
    print("=" * 64)
    print(f"Oracle hit rate — last {days} day(s)")
    print("=" * 64)
    print(f"  Total turns       : {total:,}")
    print(f"  Oracle hits       : {hits:,}  ({rate:.1f}%)")
    print(f"  LLM turns         : {llm:,}  ({llm / total * 100:.1f}%)")
    print()
    if rate < 30:
        print("  → Oracle coverage is LOW. Most traffic is paying the LLM tax.")
        print("    Re-run with --misses to see candidates for new intents.")
    elif rate < 60:
        print("  → Oracle coverage is medium. Several add-able intents likely.")
    else:
        print("  → Oracle coverage is healthy. Strategy is paying off.")
    print()

    # 2. Per-user breakdown (only if >1 user).
    cur.execute(
        """
        SELECT user_id,
               COUNT(*) FILTER (WHERE COALESCE(signals->>'path', 'llm') = 'oracle') AS hits,
               COUNT(*) AS total
        FROM ai_episodes
        WHERE created_at >= NOW() - make_interval(days => %s)
        GROUP BY user_id
        ORDER BY total DESC
        LIMIT 20
        """,
        (days,),
    )
    user_rows = cur.fetchall() or []
    if len(user_rows) > 1:
        print(f"Top {len(user_rows)} users by traffic:")
        print(f"  {'user_id':<40s}  {'turns':>7s}  {'hits':>7s}  {'rate':>6s}")
        for r in user_rows:
            t = int(r["total"] or 0)
            h = int(r["hits"] or 0)
            rt = (h / t * 100) if t else 0
            uid = str(r["user_id"])[:38]
            print(f"  {uid:<40s}  {t:>7d}  {h:>7d}  {rt:>5.1f}%")
        print()

    # 3. The actually-actionable output: messages that went to the LLM.
    #    These are the candidates for new oracle intents.
    if show_misses:
        cur.execute(
            """
            SELECT user_message, created_at
            FROM ai_episodes
            WHERE created_at >= NOW() - make_interval(days => %s)
              AND COALESCE(signals->>'path', 'llm') = 'llm'
              AND user_message IS NOT NULL
              AND char_length(user_message) > 4
              AND char_length(user_message) < 200
            ORDER BY created_at DESC
            LIMIT 60
            """,
            (days,),
        )
        misses = cur.fetchall() or []
        if misses:
            print("Recent LLM-path messages (candidates for new oracle intents):")
            print("-" * 64)
            for r in misses:
                msg = (r["user_message"] or "").replace("\n", " ").strip()
                print(f"  · {msg[:120]}")
            print()
            print("Patterns appearing >2× here are the next intents to add.")

    close_db(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
