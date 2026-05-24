"""
Valvo AI v4 -- Regime tool (read-only).

One merged tool: get_regime(include_history=False, limit=10).

Returns the user's current market regime from market_regime_history, how
long they've been in it (computed server-side), and optionally the full
history with per-regime duration.

Write path for regime changes: v2 action `set_market_regime` (already wired
through V2_ACTIONS + confirmation flow). The regime specialist calls it
directly; the engine prompts the user to confirm before execution.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from database.database import get_db, close_db
from services.valvo_ai_v2.utils import to_jsonable


# ═══════════════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════════════

# Valid regime values — must match backend + Settings dropdown + trailing rules
VALID_REGIMES = ("bull", "sideways", "grind_down", "sharp_down")


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


def _set_rls(cur, uid):
    if uid:
        cur.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": str(uid)}),),
        )
        cur.execute("SET LOCAL ROLE authenticated")


def _days_between(later, earlier):
    """Days between two timestamp values. Returns None if either is missing."""
    if not later or not earlier:
        return None
    try:
        if isinstance(later, str):
            later = datetime.fromisoformat(later.replace("Z", "+00:00"))
        if isinstance(earlier, str):
            earlier = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
        if later.tzinfo is None:
            later = later.replace(tzinfo=timezone.utc)
        if earlier.tzinfo is None:
            earlier = earlier.replace(tzinfo=timezone.utc)
        return (later - earlier).days
    except Exception:
        return None


def _row_to_entry(row):
    """Plain-dict shape for a single regime-history row."""
    return {
        "id": row.get("id"),
        "regime": row.get("regime"),
        "note": row.get("note"),
        "updated_at": str(row.get("updated_at") or ""),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Tool executor
# ═══════════════════════════════════════════════════════════════════════════

def exec_get_regime(params: dict) -> dict:
    """
    Return the user's current regime state + optional history.

    Params:
      include_history: bool = False  -- include full history with durations
      limit: int = 10                -- max history entries when included (max 50)

    Returns:
      current: str                   -- current regime name
      current_note: str | None       -- note from the most recent entry
      current_set_at: str            -- ISO timestamp when current regime was set
      current_duration_days: int     -- how long the current regime has been active
      history: list[entry]           -- only when include_history=True
        each entry: {regime, note, updated_at, duration_days}
        duration_days = how long that regime lasted before the next change
        (for the most recent entry, it's "days up to now")
      total_changes: int             -- total count in history (for context)
    """
    include_history = bool(params.get("include_history", False))

    try:
        limit = int(params.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    uid = _get_user_id()
    print(
        f"[get_regime] START include_history={include_history} "
        f"limit={limit} uid={uid}"
    )

    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            _set_rls(cur, uid)

            # Get the most recent entry (current regime).
            # user_id scoping happens via RLS; if RLS isn't on this table
            # yet, the explicit filter in settings_routes.py is what
            # user-scopes regime — we mirror that by restricting to the
            # current user when uid is present.
            if uid:
                cur.execute(
                    """
                    SELECT id, regime, note, updated_at
                    FROM market_regime_history
                    WHERE user_id = %s
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (uid,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, regime, note, updated_at
                    FROM market_regime_history
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                )
            latest_row = cur.fetchone()

            if latest_row is None:
                print("[get_regime] no regime history rows for user")
                return to_jsonable({
                    "current": None,
                    "current_note": None,
                    "current_set_at": None,
                    "current_duration_days": None,
                    "history": [] if include_history else None,
                    "total_changes": 0,
                    "note": (
                        "No regime has been set yet. Open Settings -> Market "
                        "Regime, or tell me to set one from chat."
                    ),
                })

            try:
                current = dict(latest_row)
            except (TypeError, ValueError):
                cols = [d[0] for d in cur.description]
                current = dict(zip(cols, latest_row))

            now = datetime.now(timezone.utc)
            current_duration = _days_between(now, current.get("updated_at"))

            result = {
                "current": current.get("regime"),
                "current_note": current.get("note"),
                "current_set_at": str(current.get("updated_at") or ""),
                "current_duration_days": current_duration,
            }

            # Total count — single COUNT query
            if uid:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM market_regime_history WHERE user_id = %s",
                    (uid,),
                )
            else:
                cur.execute("SELECT COUNT(*) AS c FROM market_regime_history")
            count_row = cur.fetchone()
            try:
                result["total_changes"] = int(dict(count_row).get("c") or 0)
            except Exception:
                result["total_changes"] = 0

            if include_history:
                if uid:
                    cur.execute(
                        """
                        SELECT id, regime, note, updated_at
                        FROM market_regime_history
                        WHERE user_id = %s
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        (uid, limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, regime, note, updated_at
                        FROM market_regime_history
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                raw_rows = cur.fetchall() or []
                rows = []
                for r in raw_rows:
                    try:
                        rows.append(dict(r))
                    except (TypeError, ValueError):
                        cols = [d[0] for d in cur.description]
                        rows.append(dict(zip(cols, r)))

                # Compute duration of each regime: time until the next change.
                # Rows are newest-first; for row[i], next change was at row[i-1].
                # For row[0] (current), duration = now - updated_at.
                entries = []
                for i, r in enumerate(rows):
                    entry = _row_to_entry(r)
                    if i == 0:
                        entry["duration_days"] = _days_between(
                            now, r.get("updated_at")
                        )
                    else:
                        entry["duration_days"] = _days_between(
                            rows[i - 1].get("updated_at"),
                            r.get("updated_at"),
                        )
                    entries.append(entry)
                result["history"] = entries
                print(f"[get_regime] history returned {len(entries)} entries")
            else:
                result["history"] = None

            return to_jsonable(result)

    except Exception as db_exc:
        import traceback
        print(
            f"[get_regime] DB error: {type(db_exc).__name__}: {db_exc}"
        )
        traceback.print_exc()
        return to_jsonable({
            "error": "Could not retrieve regime state. Try again in a moment.",
            "retryable": True,
            "current": None,
            "history": None,
        })
    finally:
        if conn is not None:
            try:
                close_db(conn)
            except Exception:
                pass
