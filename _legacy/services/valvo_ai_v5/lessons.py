"""
lessons.py — Semantic memory (graduated lessons) for Valvo AI v5.

Sits one tier above `memory.py` (personal context) and one tier above
`episodes.py` (raw turn log, separate module). The lesson pipeline:

    ai_episodes  ──[ dream cycle ]──►  ai_lessons.status='staged'
                                              │
                                              │ admin reviews + decides
                                              ▼
                              graduate ─►  status='graduated' (loaded into prompt)
                              reject   ─►  status='rejected'  (kept for churn signal)
                              reopen   ─►  status='staged'    (requeue)

Loaded into the system prompt by `format_lessons_for_prompt` after
`format_context_for_prompt`. Retrieval is heuristic in v1 (recency × tag
overlap × lexical score); we add embeddings only if lesson count grows past
a few hundred per user.

Design choices that diverge from `memory.py`:
- A graduation is a deliberate human/agent decision with a recorded rationale.
  We do NOT auto-graduate from clusters — the dream cycle only stages.
- Rejected lessons are NOT deleted. Re-staging the same idea later should
  surface "you rejected this on <date> with reason X" so we see real churn
  rather than treating each rejection as fresh.
- Lessons are per-user. A future migration can promote to global (user_id NULL).

NEVER store numeric portfolio facts in a lesson. Same rule as memory.py:
lessons are for *how to think*, not *what is true today*.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from database.database import get_db, close_db


# ─── Tuning knobs ─────────────────────────────────────────────────────────
MAX_LESSONS_IN_PROMPT = 5         # cap on how many lessons go into a system prompt
MIN_LESSON_BODY_LEN = 20          # reject empty/garbage bodies at write time
MAX_LESSON_BODY_LEN = 600         # keep prompt cost bounded


# ═══════════════════════════════════════════════════════════════════════════
#  RETRIEVAL — for prompt injection
# ═══════════════════════════════════════════════════════════════════════════

def load_relevant_lessons(
    user_id,
    query_text: str | None = None,
    tags: Iterable[str] | None = None,
    limit: int = MAX_LESSONS_IN_PROMPT,
) -> list[dict]:
    """Return up to `limit` graduated lessons most relevant to the current
    turn. Heuristic ranking (no embeddings yet):
      1. Tag overlap with `tags` (sector, tool name, intent label)
      2. Lexical token overlap with `query_text`
      3. Recency of last_used_at (gentle decay)

    The actual scoring is done in Python after pulling a small candidate set
    (status='graduated' for this user) — that table stays small per user, so
    a SQL ORDER BY is fine. If we ever cross ~500 lessons/user, switch to
    pgvector + a proper retriever.
    """
    if not user_id:
        return []
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, body, tags, last_used_at, use_count
            FROM ai_lessons
            WHERE user_id = %s AND status = 'graduated'
            ORDER BY updated_at DESC
            LIMIT 50
            """,
            (str(user_id),),
        )
        rows = cur.fetchall()
    except Exception as exc:
        print(f"[lessons] load_relevant_lessons failed: {exc}")
        return []
    finally:
        close_db(conn)

    if not rows:
        return []

    query_tokens = _tokenize(query_text)
    tag_set = {t.strip().lower() for t in (tags or []) if t and t.strip()}

    scored: list[tuple[float, dict]] = []
    for r in rows:
        row_tags = {(t or "").lower() for t in (r.get("tags") or [])}
        score = 0.0
        if tag_set:
            score += 2.0 * len(tag_set & row_tags)
        if query_tokens:
            body_tokens = _tokenize(r.get("body") or "")
            title_tokens = _tokenize(r.get("title") or "")
            score += 1.0 * len(query_tokens & body_tokens)
            score += 1.5 * len(query_tokens & title_tokens)
        # Mild recency boost, capped, so a never-used recent lesson can still surface.
        if r.get("last_used_at"):
            age_days = (datetime.utcnow() - r["last_used_at"]).total_seconds() / 86400
            score += max(0.0, 0.5 - age_days * 0.02)
        if score > 0:
            scored.append((score, dict(r)))

    scored.sort(key=lambda s: s[0], reverse=True)
    return [r for _, r in scored[:limit]]


def format_lessons_for_prompt(lessons: list[dict]) -> str:
    """Render a list of lessons into a system-prompt block. Keep it terse —
    titles + bodies, numbered. Empty-string when no lessons so the prompt
    builder can omit the section cleanly."""
    if not lessons:
        return ""
    lines = ["\nLEARNED LESSONS (from past sessions — apply when relevant; tool output still wins on facts):"]
    for i, lsn in enumerate(lessons, 1):
        title = (lsn.get("title") or "").strip()
        body = (lsn.get("body") or "").strip()
        if not body:
            continue
        if title:
            lines.append(f"  {i}. {title}")
            lines.append(f"     {body}")
        else:
            lines.append(f"  {i}. {body}")
    return "\n".join(lines) + "\n"


def record_use(lesson_ids: Iterable[int]) -> None:
    """Bump use_count + last_used_at after a lesson set is injected into a
    prompt. Cheap; fire-and-forget. A graduated lesson that never accrues
    use_count is a retrieval bug worth surfacing in eval."""
    ids = [int(i) for i in lesson_ids if i is not None]
    if not ids:
        return
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ai_lessons
            SET use_count = use_count + 1,
                last_used_at = NOW()
            WHERE id = ANY(%s)
            """,
            (ids,),
        )
        conn.commit()
    except Exception as exc:
        print(f"[lessons] record_use failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  STAGING — called by the nightly dream cycle (services.valvo_ai_v5.dream)
# ═══════════════════════════════════════════════════════════════════════════

def stage_candidate(
    user_id,
    title: str,
    body: str,
    tags: Iterable[str] | None = None,
    source: dict | None = None,
) -> int | None:
    """Insert a new staged lesson. The dream cycle calls this after clustering
    episodic patterns. We do basic sanitisation here so a noisy clusterer
    can't pollute the staged queue.

    Returns the new lesson id, or None on failure."""
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or len(body) < MIN_LESSON_BODY_LEN:
        return None
    body = body[:MAX_LESSON_BODY_LEN]
    tag_list = sorted({(t or "").strip().lower() for t in (tags or []) if t and t.strip()})

    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ai_lessons (user_id, title, body, tags, status, source)
            VALUES (%s, %s, %s, %s, 'staged', %s::jsonb)
            RETURNING id
            """,
            (str(user_id), title[:200], body, tag_list, _json_or_empty(source)),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["id"]) if row else None
    except Exception as exc:
        print(f"[lessons] stage_candidate failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  REVIEW — admin endpoints back these (mirror memory.force_refresh pattern)
# ═══════════════════════════════════════════════════════════════════════════

def list_staged(user_id, limit: int = 50) -> list[dict]:
    """Return staged lessons newest-first, with prior decision history attached
    so the reviewer sees if this is a re-staging of something already rejected."""
    if not user_id:
        return []
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT l.id, l.title, l.body, l.tags, l.source, l.created_at,
                   COALESCE(
                     (SELECT json_agg(json_build_object(
                              'decision', d.decision,
                              'rationale', d.rationale,
                              'created_at', d.created_at
                            ) ORDER BY d.created_at DESC)
                      FROM ai_lesson_decisions d WHERE d.lesson_id = l.id),
                     '[]'::json
                   ) AS history
            FROM ai_lessons l
            WHERE l.user_id = %s AND l.status = 'staged'
            ORDER BY l.created_at DESC
            LIMIT %s
            """,
            (str(user_id), limit),
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        print(f"[lessons] list_staged failed: {exc}")
        return []
    finally:
        close_db(conn)


def graduate(lesson_id: int, user_id, rationale: str, actor_id=None) -> bool:
    """Promote a staged (or reopened) lesson to graduated. Rationale required —
    the whole point of the protocol is that decisions come with reasons."""
    return _decide(lesson_id, user_id, "graduate", "graduated", rationale, actor_id)


def reject(lesson_id: int, user_id, rationale: str, actor_id=None) -> bool:
    """Decline a staged lesson. The row is kept (status='rejected') so future
    re-staging surfaces the prior pushback."""
    return _decide(lesson_id, user_id, "reject", "rejected", rationale, actor_id)


def reopen(lesson_id: int, user_id, rationale: str, actor_id=None) -> bool:
    """Requeue a previously-rejected (or graduated) lesson back to staged.
    Useful when context changes — e.g. a lesson rejected last quarter that
    now applies because the user changed strategy."""
    return _decide(lesson_id, user_id, "reopen", "staged", rationale, actor_id)


# ═══════════════════════════════════════════════════════════════════════════
#  INTERNALS
# ═══════════════════════════════════════════════════════════════════════════

def _decide(
    lesson_id: int,
    user_id,
    decision: str,
    new_status: str,
    rationale: str,
    actor_id,
) -> bool:
    """Atomic status flip + audit row. Single transaction so we never end up
    with a status change without a recorded rationale (or vice versa)."""
    rationale = (rationale or "").strip()
    if not rationale:
        # Hard rule — protocol is meaningless without a reason.
        print(f"[lessons] {decision} for {lesson_id} rejected: empty rationale")
        return False
    if not user_id:
        return False

    conn = get_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE ai_lessons
            SET status = %s
            WHERE id = %s AND user_id = %s
            RETURNING id
            """,
            (new_status, int(lesson_id), str(user_id)),
        )
        if not cur.fetchone():
            conn.rollback()
            return False
        cur.execute(
            """
            INSERT INTO ai_lesson_decisions (lesson_id, user_id, decision, rationale, actor_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                int(lesson_id),
                str(user_id),
                decision,
                rationale[:1000],
                str(actor_id) if actor_id else None,
            ),
        )
        conn.commit()
        return True
    except Exception as exc:
        print(f"[lessons] _decide({decision}) failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        close_db(conn)


_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def _tokenize(text: str | None) -> set[str]:
    """Cheap lowercase tokenizer for lexical scoring. 3+ char alnum runs only —
    drops punctuation and tiny stop-ish words (a, of, to, …) without a list."""
    if not text:
        return set()
    return set(_TOKEN_RE.findall(text.lower()))


def _json_or_empty(value: dict | None) -> str:
    import json
    if not value:
        return "{}"
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return "{}"
