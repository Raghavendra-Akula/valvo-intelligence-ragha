"""
episodes.py — Raw turn log for Valvo AI v5 (episodic memory).

Every successful (or failed) agent turn writes one row into ai_episodes. This
is the *raw* layer — no interpretation, no clustering, no de-dup. The dream
cycle (services.valvo_ai_v5.dream) reads from here, finds recurring patterns,
and stages candidate lessons in ai_lessons.

Sits next to memory.py (personal context) and lessons.py (semantic memory),
forming the three-tier learning ladder:

    episodes.log_turn  →  dream.run_cycle  →  lessons.stage_candidate
                                                      ↓
                                          admin graduates / rejects
                                                      ↓
                                       lessons.load_relevant_lessons
                                                      ↓
                                        injected into system prompt

Called from engine.query / query_stream right after history persistence —
inside a broad try/except so an episode-write failure NEVER breaks a user
turn. Latency budget: a single INSERT, no LLM call.
"""
from __future__ import annotations

import json
from typing import Iterable

from database.database import get_db, close_db


# ─── Tuning knobs ─────────────────────────────────────────────────────────
MAX_USER_MESSAGE_LEN = 4000     # truncate long pastes; we don't need verbatim
MAX_FINAL_ANSWER_LEN = 8000     # same — clusterer only needs the gist
MAX_TOOL_CALLS_LOGGED = 24      # safety cap — pathological loops shouldn't bloat the log
MAX_TOOL_INPUT_CHARS = 1500     # per tool input, after JSON-encoding


# ═══════════════════════════════════════════════════════════════════════════
#  WRITE
# ═══════════════════════════════════════════════════════════════════════════

def log_turn(
    user_id,
    user_message: str,
    final_answer: str | None,
    tool_calls: Iterable[dict] | None = None,
    *,
    page_context: str | None = None,
    rounds: int | None = None,
    total_latency_ms: int | None = None,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    intent: str | None = None,
    had_error: bool = False,
    follow_ups: list[str] | None = None,
    path: str = "llm",
) -> int | None:
    """Insert one row into ai_episodes. Returns the new id, or None on
    failure (failures are logged + swallowed — never propagate).

    `tool_calls` is the engine's tool_results_log: list of
    {tool, input, ok}. We trim each input to MAX_TOOL_INPUT_CHARS so a single
    pathological tool call (e.g. a giant SQL string) can't bloat the row.

    `signals` is built from a handful of optional kwargs the dream cycle uses
    to cluster: model, intent, token counts, follow-up presence, error flag.

    `path` records which engine branch produced the answer:
      - "oracle"  → deterministic portfolio_oracle short-circuit (no LLM)
      - "llm"     → normal Gemini tool-loop path
    Used by Backend/scripts/oracle_hit_rate.py to measure how much of
    real traffic the deterministic specialists are catching.
    """
    if not user_id:
        return None

    user_message = (user_message or "")[:MAX_USER_MESSAGE_LEN]
    final_answer = (final_answer or "")[:MAX_FINAL_ANSWER_LEN]
    status = "error" if had_error else ("partial" if not final_answer else "ok")

    trimmed_calls = _trim_tool_calls(tool_calls)
    signals = {
        "intent": intent,
        "model": model,
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "had_follow_ups": bool(follow_ups),
        "follow_up_count": len(follow_ups) if follow_ups else 0,
        "path": path,
    }

    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_episodes (
                user_id, page_context, user_message, final_answer,
                tool_calls, signals, rounds, total_latency_ms, model, status
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(user_id),
                page_context,
                user_message,
                final_answer,
                json.dumps(trimmed_calls, default=str, ensure_ascii=False),
                json.dumps(signals, default=str, ensure_ascii=False),
                int(rounds) if rounds is not None else None,
                int(total_latency_ms) if total_latency_ms is not None else None,
                model,
                status,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["id"]) if row else None
    except Exception as exc:
        print(f"[episodes] log_turn failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  READ — for the dream cycle
# ═══════════════════════════════════════════════════════════════════════════

def fetch_recent(user_id, lookback_days: int = 7, limit: int = 500) -> list[dict]:
    """Pull recent episodes for clustering. Bounded by both time and count
    so a noisy week can't OOM the dream worker. Newest first."""
    if not user_id:
        return []
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, user_message, final_answer, tool_calls, signals,
                   rounds, status, created_at
            FROM ai_episodes
            WHERE user_id = %s
              AND created_at >= NOW() - (%s || ' days')::interval
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (str(user_id), int(lookback_days), int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        print(f"[episodes] fetch_recent failed: {exc}")
        return []
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  INTERNALS
# ═══════════════════════════════════════════════════════════════════════════

def _trim_tool_calls(tool_calls: Iterable[dict] | None) -> list[dict]:
    """Defensive sanitisation. The engine's tool_results_log is trusted, but
    we still cap shape and size so the row stays bounded."""
    if not tool_calls:
        return []
    out: list[dict] = []
    for raw in tool_calls:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("tool") or raw.get("name") or "")[:80]
        if not name:
            continue
        try:
            inp = json.dumps(raw.get("input"), default=str, ensure_ascii=False)
        except Exception:
            inp = ""
        if len(inp) > MAX_TOOL_INPUT_CHARS:
            inp = inp[:MAX_TOOL_INPUT_CHARS] + "...(truncated)"
        out.append({
            "name": name,
            "input": inp,
            "ok": bool(raw.get("ok", True)),
        })
        if len(out) >= MAX_TOOL_CALLS_LOGGED:
            break
    return out
