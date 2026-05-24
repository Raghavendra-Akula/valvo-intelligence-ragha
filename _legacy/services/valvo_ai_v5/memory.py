"""
memory.py — Persistent user context for Valvo AI v5.

Stores durable facts about each user (name, trading style, recurring
interests, focus stocks) across sessions. The AI reads these facts into
every system prompt so it greets returning users correctly and frames
answers in their preferred style.

Storage: public.user_ai_context table (one row per user_id). Migration
at Backend/database/create_user_ai_context.sql.

Extraction: every 5th turn (or if >12h since last extract) we run a
Flash-Lite summarisation call that takes the existing context + last
10 turns from chat_messages and returns an updated context JSON. Cheap
enough that the latency is invisible to the user when run inline at
end of turn.

NEVER used for numeric facts — only tone / preferences / focus areas.
The AI is explicitly told in the injected block not to fabricate
portfolio data from memory; only tool output is authoritative.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from database.database import get_db, close_db


# ─── Tuning knobs ─────────────────────────────────────────────────────────
EXTRACT_EVERY_N_TURNS = 5      # re-extract every N turns
EXTRACT_MIN_INTERVAL_HOURS = 12  # ...or if this much time has passed
EXTRACT_HISTORY_LIMIT = 10     # last N turns fed to the extractor
MAX_OBSERVATIONS = 8           # cap on stored observations to keep prompt light


# ═══════════════════════════════════════════════════════════════════════════
#  LOAD / FORMAT
# ═══════════════════════════════════════════════════════════════════════════

def load_context(user_id) -> dict:
    """Return the user's memory dict, or {} if none exists yet."""
    if not user_id:
        return {}
    conn = get_db()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT context, turn_count, last_extracted_at "
            "FROM user_ai_context WHERE user_id = %s",
            (str(user_id),),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "context": dict(row.get("context") or {}),
            "turn_count": row.get("turn_count") or 0,
            "last_extracted_at": row.get("last_extracted_at"),
        }
    except Exception as exc:
        print(f"[memory] load_context failed: {exc}")
        return {}
    finally:
        close_db(conn)


def format_context_for_prompt(mem: dict) -> str:
    """Render the stored context into a human-readable block for injection
    into the system prompt. Returns empty string if no useful context.

    Deliberately terse — a trader's attention on the prompt is worth more
    than "thorough" recall. We list at most 5 observations.
    """
    if not mem:
        return ""
    ctx = mem.get("context") or {}
    if not ctx:
        return ""

    parts = []
    name = ctx.get("name")
    bio = ctx.get("bio")
    observations = ctx.get("observations") or []

    if name:
        parts.append(f"The user goes by **{name}**.")
    if bio:
        parts.append(f"Bio: {bio}")
    if observations:
        trimmed = [o for o in observations if isinstance(o, str) and o.strip()][:5]
        if trimmed:
            parts.append("Patterns observed:")
            parts.extend(f"  - {o.strip()}" for o in trimmed)

    if not parts:
        return ""

    return (
        "\nWHAT I KNOW ABOUT YOU (from prior sessions — use for tone / framing "
        "/ emphasis, NEVER fabricate portfolio facts from this; only tool output is authoritative):\n"
        + "\n".join(parts)
        + "\n"
    )


# ═══════════════════════════════════════════════════════════════════════════
#  INCREMENT + THROTTLE EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def increment_turn_and_maybe_extract(
    user_id,
    user_message: str,
    assistant_message: str,
    gateway,
) -> None:
    """Called at the end of every successful query turn. Bumps the user's
    turn_count; if we hit an extraction threshold (every N turns OR >12h
    since last extract), runs the Flash-Lite extractor to update the
    stored context. Safe to call synchronously — the whole thing is ~1
    cheap tool call and gated by the throttle.
    """
    if not user_id:
        return
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        # Upsert and bump the counter atomically
        cur.execute(
            """
            INSERT INTO user_ai_context (user_id, context, turn_count, updated_at)
            VALUES (%s, '{}'::jsonb, 1, NOW())
            ON CONFLICT (user_id) DO UPDATE
                SET turn_count = user_ai_context.turn_count + 1,
                    updated_at = NOW()
            RETURNING context, turn_count, last_extracted_at
            """,
            (str(user_id),),
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return
        ctx = dict(row.get("context") or {})
        turn_count = row.get("turn_count") or 0
        last_extracted = row.get("last_extracted_at")

        # Gate: only extract on threshold OR stale
        should_extract = turn_count % EXTRACT_EVERY_N_TURNS == 0
        if not should_extract and last_extracted:
            if datetime.utcnow() - last_extracted > timedelta(hours=EXTRACT_MIN_INTERVAL_HOURS):
                should_extract = True
        elif not last_extracted:
            # First-ever turn → extract immediately so the name is captured early
            should_extract = True

        if not should_extract:
            return

    except Exception as exc:
        print(f"[memory] increment failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return
    finally:
        close_db(conn)

    # Heavy path (DB connection released before the LLM call)
    try:
        _run_extraction(user_id, ctx, gateway)
    except Exception as exc:
        # Non-fatal — memory update failure never breaks a user turn.
        print(f"[memory] extraction failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════
#  EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

_EXTRACTION_INSTRUCTION = """\
You are maintaining a durable MEMORY for a single trader using Valvo AI, a
trading analytics assistant. Your job: read the CURRENT memory and the user's
recent Q&A turns, and return an UPDATED memory as JSON.

OUTPUT STRICTLY as JSON with this shape:
{
  "name":         "<their name or null>",
  "bio":          "<one short sentence about their trading — style, SL rule, portfolio size, cadence>",
  "observations": ["<short fact>", "<short fact>", ...]
}

Rules:
- Keep "name" if the user identified themselves at any point (often in the
  opener's greeting or an explicit "I'm X").
- "bio" should stay stable — only update if genuinely new info contradicts or
  adds to it. One sentence max.
- "observations" is a ranked list (most important first) of durable patterns:
  preferred metrics, sectors they watch, typical question style, recurring
  focus stocks, response-length preference. Maximum 8 entries. Drop stale
  items — don't let the list grow unbounded.
- NEVER include portfolio numbers (P&L, position sizes, prices) — those
  change constantly and belong in tools, not memory.
- NEVER guess. If a field is unclear, keep the existing value.
- Output JSON only, no markdown fence, no prose.
"""


def _run_extraction(user_id, current_ctx: dict, gateway) -> None:
    """Call Gemini Flash-Lite with the current memory + recent chat history,
    parse the returned JSON, write it back."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT role, content, created_at
            FROM chat_messages
            WHERE page_context LIKE 'valvo-ai-v5%%' AND user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (str(user_id), EXTRACT_HISTORY_LIMIT),
        )
        rows = cur.fetchall()
    except Exception as exc:
        print(f"[memory] history read failed: {exc}")
        rows = []
    finally:
        close_db(conn)

    if not rows:
        return

    # Build a compact transcript (oldest → newest)
    transcript_lines = []
    for r in reversed(rows):
        role = "User" if r["role"] == "user" else "AI"
        content = (r["content"] or "").strip()
        if len(content) > 500:
            content = content[:500] + "…"
        transcript_lines.append(f"{role}: {content}")
    transcript = "\n".join(transcript_lines)

    user_msg = (
        f"Current memory (may be empty):\n{json.dumps(current_ctx, ensure_ascii=False)}\n\n"
        f"Recent conversation (oldest first):\n{transcript}\n\n"
        f"Return updated memory JSON now:"
    )

    try:
        # Use flash (the agent default) so memory extraction inherits the
        # same accuracy bar as the rest of the system. flash-lite was
        # cheaper but produced flakier extractions, and the discrepancy
        # between "AI says one thing, memory remembers another" was
        # confusing in practice. No thinking, no tools — extraction is a
        # one-shot summarisation.
        response = gateway.create_message(
            model="gemini-flash",
            max_tokens=1024,
            system=_EXTRACTION_INSTRUCTION,
            messages=[{"role": "user", "content": user_msg}],
            tools=[],
            thinking=False,
        )
    except Exception as exc:
        print(f"[memory] gateway call failed: {exc}")
        return

    text = (response.text or "").strip()
    # Strip accidental code fences
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()

    try:
        parsed = json.loads(text)
    except Exception:
        print(f"[memory] extraction returned invalid JSON (len={len(text)}), ignoring")
        return

    # Sanitise + clamp
    new_ctx = {}
    if isinstance(parsed.get("name"), str) and parsed["name"].strip():
        new_ctx["name"] = parsed["name"].strip()[:80]
    if isinstance(parsed.get("bio"), str) and parsed["bio"].strip():
        new_ctx["bio"] = parsed["bio"].strip()[:300]
    if isinstance(parsed.get("observations"), list):
        obs = []
        for o in parsed["observations"]:
            if isinstance(o, str) and o.strip():
                obs.append(o.strip()[:200])
            if len(obs) >= MAX_OBSERVATIONS:
                break
        if obs:
            new_ctx["observations"] = obs

    if not new_ctx:
        return

    # Write back
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE user_ai_context
            SET context = %s::jsonb,
                last_extracted_at = NOW(),
                updated_at = NOW()
            WHERE user_id = %s
            """,
            (json.dumps(new_ctx, ensure_ascii=False), str(user_id)),
        )
        conn.commit()
    except Exception as exc:
        print(f"[memory] write failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  ADMIN OPS
# ═══════════════════════════════════════════════════════════════════════════

def reset_context(user_id) -> bool:
    """Wipe everything we know about this user. Invoked from the admin
    endpoint (DELETE /api/valvo-ai-v5/memory)."""
    if not user_id:
        return False
    conn = get_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE user_ai_context SET context = '{}'::jsonb, turn_count = 0, "
            "last_extracted_at = NULL, updated_at = NOW() WHERE user_id = %s",
            (str(user_id),),
        )
        conn.commit()
        return True
    except Exception as exc:
        print(f"[memory] reset failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        close_db(conn)


def force_refresh(user_id, gateway) -> dict:
    """Admin endpoint helper: run the extractor immediately regardless of
    throttle. Returns the updated context."""
    if not user_id:
        return {}
    mem = load_context(user_id)
    current_ctx = mem.get("context") or {}
    _run_extraction(user_id, current_ctx, gateway)
    return load_context(user_id).get("context") or {}
