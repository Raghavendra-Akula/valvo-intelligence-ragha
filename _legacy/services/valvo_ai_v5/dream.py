"""
dream.py — Nightly clusterer that turns episodes into staged lessons.

Runs once per user (manually via the admin endpoint, or on a cron). The flow:

  1. fetch_recent(user_id, lookback_days)
  2. Bucket episodes by tool-call signature (sorted tuple of tool names).
  3. For each bucket with >= MIN_CLUSTER_SIZE distinct user messages, ask
     Flash-Lite: "what's the durable lesson here?" using the existing user
     memory + the bucket's example Q&A pairs.
  4. Sanity-check the response, dedupe against already-staged lessons by
     title prefix, then call lessons.stage_candidate.

Cheap by construction: at most ~10 LLM calls per user per cycle (the bucket
cap), each on flash, no thinking, no tools, ≤1k output tokens. The
dream cycle's job is to *propose* — graduation is still a deliberate human
decision via the admin endpoints.

Deliberately NOT auto-graduating. This is the central agentic-stack rule we
copied: clustering is mechanical, promotion is judgemental. Skipping the
review step is what turns a lesson library into a noise pile.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Iterable

from . import episodes, lessons


# ─── Tuning knobs ─────────────────────────────────────────────────────────
MIN_CLUSTER_SIZE = 3              # at least this many distinct user msgs in a bucket
MAX_BUCKETS_PER_RUN = 8           # cap LLM calls per user per cycle
MAX_EXAMPLES_PER_BUCKET = 5       # how many Q/A pairs the LLM sees
EXAMPLE_QUESTION_LEN = 280        # truncate examples for prompt economy
EXAMPLE_ANSWER_LEN = 400
DEDUPE_TITLE_PREFIX = 60          # treat same-prefix titles as a duplicate cluster


_LESSON_INSTRUCTION = """\
You are reading recurring Q&A patterns from a single trader using Valvo AI.
Your job: propose ONE durable lesson the assistant should remember so future
similar questions are handled better. Output strictly as JSON:

{
  "title":  "<short imperative, max 90 chars>",
  "body":   "<2-4 sentences. WHEN this applies, WHAT to do, and one nuance.>",
  "tags":   ["<lowercase>", "<lowercase>"]
}

Rules:
- A lesson is about HOW TO THINK, not what is currently true. NEVER bake in
  prices, position sizes, P&L numbers, or specific stock counts — those rot.
- Tags should be reusable retrieval keys: tool names actually called
  (e.g. "get_compare_stocks"), or stable concepts ("rs", "sector_rotation",
  "pyramid", "stoploss"). 2–5 tags. Lowercase, snake_case if multiword.
- If the cluster doesn't actually represent a real pattern (just unrelated
  questions that happen to share a tool), return: {"title": "", "body": "", "tags": []}
- JSON only. No markdown fences, no prose, no apology.
"""


# ═══════════════════════════════════════════════════════════════════════════
#  PUBLIC ENTRY
# ═══════════════════════════════════════════════════════════════════════════

def run_cycle(user_id, gateway, lookback_days: int = 7) -> dict:
    """One full dream pass for one user. Returns a small report dict so the
    admin endpoint can show what happened. Safe to call concurrently — each
    candidate is INSERTed independently."""
    if not user_id or not gateway:
        return {"ok": False, "reason": "missing user_id or gateway"}

    rows = episodes.fetch_recent(user_id, lookback_days=lookback_days)
    if not rows:
        return {"ok": True, "episodes": 0, "buckets": 0, "staged": 0}

    buckets = _bucketize(rows)
    if not buckets:
        return {"ok": True, "episodes": len(rows), "buckets": 0, "staged": 0}

    # Stage-once guard: skip clusters whose tool-signature already produced a
    # staged-or-graduated lesson recently. Prevents the same pattern from
    # re-staging every night.
    seen_titles = _existing_lesson_titles(user_id)

    ranked = sorted(buckets.values(), key=lambda b: -len(b["episodes"]))
    staged_ids: list[int] = []

    for bucket in ranked[:MAX_BUCKETS_PER_RUN]:
        if len(bucket["distinct_msgs"]) < MIN_CLUSTER_SIZE:
            continue
        candidate = _ask_for_lesson(bucket, gateway)
        if not candidate:
            continue
        title = candidate.get("title", "").strip()
        body = candidate.get("body", "").strip()
        tags_in = candidate.get("tags") or []
        if not title or not body:
            continue
        # Cheap dedupe: same first 60 chars of title already exists.
        if any(title[:DEDUPE_TITLE_PREFIX].lower() in t for t in seen_titles):
            continue

        # Ensure tool names from the cluster are in the tag set so retrieval
        # later picks this lesson up when those tools come up again.
        merged_tags = set(t for t in tags_in if isinstance(t, str)) | set(bucket["tool_names"])
        new_id = lessons.stage_candidate(
            user_id=user_id,
            title=title,
            body=body,
            tags=merged_tags,
            source={
                "tool_signature": list(bucket["signature"]),
                "episode_ids": bucket["episode_ids"][:10],
                "cluster_size": len(bucket["episodes"]),
                "lookback_days": lookback_days,
            },
        )
        if new_id:
            staged_ids.append(new_id)
            seen_titles.add(title[:DEDUPE_TITLE_PREFIX].lower())

    return {
        "ok": True,
        "episodes": len(rows),
        "buckets": len(buckets),
        "staged": len(staged_ids),
        "staged_ids": staged_ids,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  CLUSTERING
# ═══════════════════════════════════════════════════════════════════════════

def _bucketize(rows: list[dict]) -> dict[tuple, dict]:
    """Group episodes by sorted tuple of tool-call names. Empty signatures
    (pure-text turns) bucket together as ('__chat__',) — sometimes a recurring
    chat-only pattern is itself a lesson."""
    buckets: dict[tuple, dict] = defaultdict(lambda: {
        "signature": (),
        "tool_names": set(),
        "episodes": [],
        "episode_ids": [],
        "distinct_msgs": set(),
    })
    for r in rows:
        if r.get("status") == "error":
            continue
        calls = r.get("tool_calls") or []
        # tool_calls came back as a JSON array of {name, input, ok}
        names = sorted({(c.get("name") or "").strip() for c in calls if isinstance(c, dict) and c.get("name")})
        sig = tuple(names) if names else ("__chat__",)
        b = buckets[sig]
        b["signature"] = sig
        b["tool_names"] = set(sig) - {"__chat__"}
        b["episodes"].append(r)
        b["episode_ids"].append(r["id"])
        msg = (r.get("user_message") or "").strip().lower()
        if msg:
            b["distinct_msgs"].add(msg[:120])
    return buckets


# ═══════════════════════════════════════════════════════════════════════════
#  LLM CALL
# ═══════════════════════════════════════════════════════════════════════════

def _ask_for_lesson(bucket: dict, gateway) -> dict | None:
    """One Flash-Lite call per bucket. Returns the parsed JSON candidate, or
    None if anything went wrong / the model returned the empty-shape
    sentinel."""
    examples = _select_examples(bucket["episodes"])
    if not examples:
        return None

    sig = ", ".join(bucket["signature"]) or "(no tools)"
    examples_block = "\n\n".join(
        f"User: {ex['q']}\nAssistant: {ex['a']}"
        for ex in examples
    )
    user_msg = (
        f"Cluster tool signature: {sig}\n"
        f"Cluster size (turns): {len(bucket['episodes'])}\n\n"
        f"Recent examples:\n{examples_block}\n\n"
        "Return the lesson JSON now:"
    )

    try:
        # Match the agent's default model — keeps lesson-extraction
        # quality consistent with the surface that consumes those
        # lessons. Cheap because dream runs once per session, not
        # per turn.
        resp = gateway.create_message(
            model="gemini-flash",
            max_tokens=1024,
            system=_LESSON_INSTRUCTION,
            messages=[{"role": "user", "content": user_msg}],
            tools=[],
            thinking=False,
        )
    except Exception as exc:
        print(f"[dream] gateway call failed: {exc}")
        return None

    text = (resp.text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()

    try:
        parsed = json.loads(text)
    except Exception:
        print(f"[dream] invalid JSON from clusterer (len={len(text)}); skipping bucket")
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _select_examples(episodes_in_bucket: list[dict]) -> list[dict]:
    """Pick up to MAX_EXAMPLES_PER_BUCKET *distinct* Q/A pairs, newest first.
    Distinctness is on the truncated user_message — the same paraphrased
    question shouldn't fill the example budget."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in episodes_in_bucket:
        q = (r.get("user_message") or "").strip()
        a = (r.get("final_answer") or "").strip()
        if not q or not a:
            continue
        key = q[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "q": q[:EXAMPLE_QUESTION_LEN],
            "a": a[:EXAMPLE_ANSWER_LEN],
        })
        if len(out) >= MAX_EXAMPLES_PER_BUCKET:
            break
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  DEDUPE HELPER
# ═══════════════════════════════════════════════════════════════════════════

def _existing_lesson_titles(user_id) -> set[str]:
    """All staged + graduated lesson titles (lowercased prefix) for this user.
    Used to skip re-clustering the same pattern. Rejected lessons are
    deliberately NOT excluded here — the dream cycle should be free to
    re-stage them later if the cluster keeps appearing; the prior decision
    history will surface in list_staged."""
    from database.database import get_db, close_db

    if not user_id:
        return set()
    conn = get_db()
    if not conn:
        return set()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT title FROM ai_lessons
            WHERE user_id = %s AND status IN ('staged', 'graduated')
            """,
            (str(user_id),),
        )
        return {(r["title"] or "")[:DEDUPE_TITLE_PREFIX].lower() for r in cur.fetchall()}
    except Exception as exc:
        print(f"[dream] existing-titles read failed: {exc}")
        return set()
    finally:
        close_db(conn)
