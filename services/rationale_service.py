"""
Trade rationale capture — behavioral coaching loop.

Hooked from actions.py after a position closes. Decides whether the
close + recent history warrants asking the user "why did you take
those trades", and if so, writes a 'pending' row to
trade_rationale_prompts. The frontend polls /api/rationale/pending
on page load and pops the Valvo AI floating chat with the question.

When the user answers, /api/rationale/answer routes the natural-
language response back here for tag extraction (Gemini Flash, fixed
taxonomy) and persistence. The graph page then visualises tag
frequency and P&L impact over time.

Admin-gated for now (see rationale_routes.py).
"""
from __future__ import annotations

import json
import os
from typing import Optional

# Fixed taxonomy. The graph page renders nodes from this list, so adding
# a new tag here automatically gets it a node. Keep the list small (<25)
# or the bubble chart becomes noise.
RATIONALE_TAGS = [
    # Emotion-driven
    "fomo",                         # fear of missing out
    "revenge_trade",                # trying to make back a loss
    "overconfidence",               # winning streak made me complacent
    "anxiety",                      # market noise / news scared me into it
    # External signals
    "news_chasing",                 # acted on a news headline
    "tip_followed",                 # someone told me to buy
    "social_media_pump",            # twitter/telegram/discord hype
    "analyst_recommendation",       # broker / research call
    # Process violations
    "plan_violation",               # broke own rules
    "ignored_sl",                   # SL hit but I held
    "no_setup",                     # no clear technical setup
    "position_too_large",           # over-sized for risk
    # Market context
    "index_underperformance",       # felt I was lagging the index
    "breakout_chase",               # chased a breakout that already moved
    "momentum_chase",               # chased late momentum
    "sector_rotation_chase",        # piled into a hot sector late
    # Decision quality
    "gut_call",                     # pure intuition, no analysis
    "didnt_check_regime",           # ignored bear/bull regime
    "confirmation_bias",            # only looked at supporting evidence
]


# ────────────────────────────────────────────────────────────────────
#  Trigger detection — called from actions.py AFTER a position closes
# ────────────────────────────────────────────────────────────────────

# How recently we'll consider losses for the streak check
LOSS_STREAK_WINDOW_HOURS = 48
# Minimum number of losing closes within the window to fire 'loss_streak'
LOSS_STREAK_MIN_COUNT = 2
# R-multiple threshold for the 'big_loss' single-trade trigger.
# A 1.5R loss means the trade lost 1.5x the planned risk amount.
BIG_LOSS_R_THRESHOLD = -1.5
# How long to wait between prompts so we don't spam (a multi-close
# cleanup shouldn't generate 4 prompts in 30 seconds).
COOLDOWN_HOURS = 6


def _safe_float(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _has_recent_prompt(cur, user_id: str) -> bool:
    """Return True if there's already a pending prompt OR a created-within-
    cooldown prompt for this user. Both are anti-spam: don't ask twice."""
    cur.execute(
        """
        SELECT 1 FROM trade_rationale_prompts
        WHERE user_id = %s
          AND (
            status = 'pending'
            OR created_at > NOW() - INTERVAL '%s hours'
          )
        LIMIT 1
        """,
        (user_id, COOLDOWN_HOURS),
    )
    return cur.fetchone() is not None


def _r_multiple(entry: float, exit_p: float, sl: float) -> float:
    """R-multiple: trade P&L / planned risk per share. Negative for losses."""
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    return (exit_p - entry) / risk


def _recent_losing_closes(cur, user_id: str, hours: int) -> list[dict]:
    """Pull positions closed at a loss in the last N hours."""
    cur.execute(
        """
        SELECT id, stock_name, entry_price, exit_price, stop_loss,
               quantity, total_pnl, exit_date
        FROM positions
        WHERE user_id = %s
          AND status = 'closed'
          AND exit_date > NOW() - INTERVAL '%s hours'
          AND total_pnl < 0
        ORDER BY exit_date DESC
        """,
        (user_id, hours),
    )
    return cur.fetchall() or []


def detect_triggers_after_close(cur, user_id: str, position_id: int) -> Optional[dict]:
    """Inspect the just-closed position + recent history; return a prompt
    spec dict if a trigger fires, else None.

    Returns: {
        "trigger_kind": "loss_streak" | "big_loss",
        "trigger_details": {...},
        "position_ids": [int, ...],
        "question_text": str,
        "pnl_impact": float,
    }

    Caller (rationale_routes or actions hook) is responsible for the INSERT.
    """
    # Anti-spam: skip entirely if there's already a pending or recent prompt.
    if _has_recent_prompt(cur, user_id):
        return None

    # Load the position we just closed
    cur.execute(
        """
        SELECT id, stock_name, entry_price, exit_price, stop_loss,
               quantity, total_pnl, total_pnl_pct, exit_date, status
        FROM positions
        WHERE id = %s AND user_id = %s
        """,
        (position_id, user_id),
    )
    pos = cur.fetchone()
    if not pos:
        return None

    # Only fire on actually-closed positions. Routes call us optimistically
    # after commit, but the partial-sell path may not have reached close yet.
    if (pos.get("status") or "").lower() != "closed":
        return None

    pnl = _safe_float(pos.get("total_pnl"))
    if pnl >= 0:
        # Winner — no rationale prompt. We're only interested in losses for now.
        return None

    entry_p = _safe_float(pos.get("entry_price"))
    exit_p = _safe_float(pos.get("exit_price"))
    sl = _safe_float(pos.get("stop_loss"))
    r_mult = _r_multiple(entry_p, exit_p, sl)

    # Trigger A: single big loss (>= 1.5R loss). Always fires standalone.
    if r_mult <= BIG_LOSS_R_THRESHOLD:
        return {
            "trigger_kind": "big_loss",
            "trigger_details": {
                "position_id": int(pos["id"]),
                "stock_name": pos["stock_name"],
                "entry_price": entry_p,
                "exit_price": exit_p,
                "r_multiple": round(r_mult, 2),
                "pnl": round(pnl, 2),
            },
            "position_ids": [int(pos["id"])],
            "question_text": (
                f"You just closed {pos['stock_name']} at a {abs(round(r_mult, 1))}R loss "
                f"(₹{abs(round(pnl, 0)):,.0f}). What was driving the entry? Was it a clear "
                "setup, FOMO, a tip, news? Be honest — this is for your own pattern recognition."
            ),
            "pnl_impact": round(pnl, 2),
        }

    # Trigger B: loss streak — 2+ losing closes in the last 48h
    recent_losses = _recent_losing_closes(cur, user_id, LOSS_STREAK_WINDOW_HOURS)
    if len(recent_losses) >= LOSS_STREAK_MIN_COUNT:
        names = [r["stock_name"] for r in recent_losses]
        total_pnl = sum(_safe_float(r.get("total_pnl")) for r in recent_losses)
        names_str = ", ".join(names[:5])
        if len(names) > 5:
            names_str += f" + {len(names) - 5} more"
        return {
            "trigger_kind": "loss_streak",
            "trigger_details": {
                "count": len(recent_losses),
                "window_hours": LOSS_STREAK_WINDOW_HOURS,
                "stocks": names,
                "total_pnl": round(total_pnl, 2),
            },
            "position_ids": [int(r["id"]) for r in recent_losses],
            "question_text": (
                f"You've closed {len(recent_losses)} losing trades in the last 48 hours "
                f"({names_str}) — total ₹{abs(round(total_pnl, 0)):,.0f} down. "
                "What was driving each entry? FOMO, revenge, news chasing, breaking your "
                "own rules? Walk me through it — I'll log the patterns so we can spot them next time."
            ),
            "pnl_impact": round(total_pnl, 2),
        }

    return None


def insert_pending_prompt(cur, user_id: str, spec: dict) -> int:
    """Persist the spec. Returns the new prompt id."""
    cur.execute(
        """
        INSERT INTO trade_rationale_prompts
            (user_id, trigger_kind, trigger_details, position_ids,
             question_text, pnl_impact, status, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, 'pending', NOW())
        RETURNING id
        """,
        (
            user_id,
            spec["trigger_kind"],
            json.dumps(spec["trigger_details"]),
            spec["position_ids"],
            spec["question_text"],
            spec.get("pnl_impact"),
        ),
    )
    return int(cur.fetchone()["id"])


def maybe_create_prompt(cur, user_id: str, position_id: int) -> Optional[int]:
    """The single entry point called from actions.py after a close.
    Returns the new prompt id, or None if no trigger fired.

    Failures here are swallowed — a rationale-system bug must not fail a
    user's actual trade close. We log and move on."""
    try:
        spec = detect_triggers_after_close(cur, user_id, position_id)
        if not spec:
            return None
        return insert_pending_prompt(cur, user_id, spec)
    except Exception as exc:
        print(f"[rationale] maybe_create_prompt failed: {exc}")
        return None


# ────────────────────────────────────────────────────────────────────
#  Tag extraction — Gemini Flash, fixed taxonomy
# ────────────────────────────────────────────────────────────────────

def _extract_tags_with_gemini(answer_text: str) -> list[str]:
    """Single Gemini Flash call. Returns a list of tags from RATIONALE_TAGS,
    or empty list on any failure (we still persist the raw answer)."""
    api_key = os.getenv("api_key", "").strip()
    if not api_key:
        print("[rationale] no api_key env var — skipping tag extraction")
        return []

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key, http_options={"timeout": 30 * 1000})

        prompt = (
            "You are a behavioral-finance classifier. Read the trader's "
            "free-form rationale below and return a JSON array of tags from "
            "this fixed taxonomy. Only use tags from this list, nothing "
            "else. Pick all that apply (typically 1-4 tags). Return ONLY "
            "the JSON array, no prose, no markdown.\n\n"
            f"TAXONOMY: {json.dumps(RATIONALE_TAGS)}\n\n"
            f"TRADER RATIONALE:\n{answer_text}\n\n"
            "JSON ARRAY:"
        )

        config = types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=200,
            response_mime_type="application/json",
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=config,
        )
        raw = (resp.text or "").strip()
        if not raw:
            return []
        try:
            tags = json.loads(raw)
        except Exception:
            # Sometimes the model wraps in code fences despite the mime type
            cleaned = raw.strip("`").strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            tags = json.loads(cleaned)

        if not isinstance(tags, list):
            return []
        # Filter to known taxonomy — silently drop hallucinated tags
        known = set(RATIONALE_TAGS)
        return [t for t in tags if isinstance(t, str) and t in known]
    except Exception as exc:
        print(f"[rationale] gemini tag extraction failed: {exc}")
        return []


def record_answer(cur, prompt_id: int, user_id: str, answer_text: str) -> dict:
    """Extract tags, persist both raw answer and tags, return the updated row.
    Idempotent — calling twice on the same prompt re-extracts and overwrites."""
    answer_clean = (answer_text or "").strip()
    if not answer_clean:
        raise ValueError("answer_text required")

    tags = _extract_tags_with_gemini(answer_clean)

    cur.execute(
        """
        UPDATE trade_rationale_prompts
        SET answer_text   = %s,
            extracted_tags = %s,
            status        = 'answered',
            answered_at   = NOW()
        WHERE id = %s AND user_id = %s
        RETURNING id, status, extracted_tags, answer_text, answered_at,
                  trigger_kind, trigger_details, position_ids, pnl_impact,
                  question_text, created_at
        """,
        (answer_clean, json.dumps(tags), prompt_id, user_id),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"prompt {prompt_id} not found for user")
    return row


def dismiss_prompt(cur, prompt_id: int, user_id: str) -> bool:
    """Mark a prompt as dismissed. Returns True if a row was updated."""
    cur.execute(
        """
        UPDATE trade_rationale_prompts
        SET status       = 'dismissed',
            dismissed_at = NOW()
        WHERE id = %s AND user_id = %s AND status = 'pending'
        """,
        (prompt_id, user_id),
    )
    return cur.rowcount > 0


def list_pending(cur, user_id: str, limit: int = 5) -> list[dict]:
    cur.execute(
        """
        SELECT id, trigger_kind, trigger_details, position_ids,
               question_text, pnl_impact, created_at
        FROM trade_rationale_prompts
        WHERE user_id = %s AND status = 'pending'
        ORDER BY created_at ASC
        LIMIT %s
        """,
        (user_id, limit),
    )
    return cur.fetchall() or []


# ────────────────────────────────────────────────────────────────────
#  Graph data — admin page
# ────────────────────────────────────────────────────────────────────

def get_graph_data(cur, user_id: str, days: int = 180) -> dict:
    """Return the bubble-chart data + raw prompts for the rationale graph page.

    Shape:
    {
      "nodes": [
        {"tag": "fomo", "count": 5, "total_pnl": -12500, "avg_pnl": -2500,
         "prompt_ids": [1, 7, 12, ...]},
        ...
      ],
      "prompts": [
        {"id": 1, "trigger_kind": "loss_streak", "question_text": "...",
         "answer_text": "...", "extracted_tags": ["fomo", "revenge_trade"],
         "pnl_impact": -8500, "answered_at": "2026-05-04T...", "stocks": ["KRN", "GROWW"]},
        ...
      ],
      "summary": {
         "total_prompts": 12,
         "total_loss_captured": -45000,
         "top_tag": "fomo",
         "window_days": 180
      }
    }
    """
    cur.execute(
        """
        SELECT id, trigger_kind, trigger_details, position_ids,
               question_text, answer_text, extracted_tags, pnl_impact,
               created_at, answered_at
        FROM trade_rationale_prompts
        WHERE user_id = %s
          AND status = 'answered'
          AND answered_at > NOW() - INTERVAL '%s days'
        ORDER BY answered_at DESC
        """,
        (user_id, days),
    )
    answered = cur.fetchall() or []

    # Aggregate per tag
    tag_agg: dict[str, dict] = {}
    for row in answered:
        raw_tags = row.get("extracted_tags") or []
        if isinstance(raw_tags, str):
            try:
                raw_tags = json.loads(raw_tags)
            except Exception:
                raw_tags = []
        pnl = _safe_float(row.get("pnl_impact"))
        for t in raw_tags:
            if t not in tag_agg:
                tag_agg[t] = {"tag": t, "count": 0, "total_pnl": 0.0, "prompt_ids": []}
            tag_agg[t]["count"] += 1
            tag_agg[t]["total_pnl"] += pnl
            tag_agg[t]["prompt_ids"].append(int(row["id"]))

    nodes = []
    for t, data in tag_agg.items():
        avg = data["total_pnl"] / data["count"] if data["count"] else 0
        nodes.append({
            "tag": t,
            "count": data["count"],
            "total_pnl": round(data["total_pnl"], 2),
            "avg_pnl": round(avg, 2),
            "prompt_ids": data["prompt_ids"],
        })
    nodes.sort(key=lambda n: n["count"], reverse=True)

    # Resolve stock names per prompt for the drilldown panel
    all_pos_ids = sorted({pid for r in answered for pid in (r.get("position_ids") or [])})
    pos_lookup: dict[int, str] = {}
    if all_pos_ids:
        cur.execute(
            "SELECT id, stock_name FROM positions WHERE id = ANY(%s)",
            (all_pos_ids,),
        )
        for r in cur.fetchall() or []:
            pos_lookup[int(r["id"])] = r["stock_name"]

    prompts_out = []
    for r in answered:
        raw_tags = r.get("extracted_tags") or []
        if isinstance(raw_tags, str):
            try:
                raw_tags = json.loads(raw_tags)
            except Exception:
                raw_tags = []
        pids = r.get("position_ids") or []
        prompts_out.append({
            "id": int(r["id"]),
            "trigger_kind": r["trigger_kind"],
            "question_text": r["question_text"],
            "answer_text": r["answer_text"],
            "extracted_tags": raw_tags,
            "pnl_impact": _safe_float(r.get("pnl_impact")),
            "answered_at": r["answered_at"].isoformat() if r.get("answered_at") else None,
            "stocks": [pos_lookup.get(int(p), f"#{p}") for p in pids],
        })

    total_loss = sum(_safe_float(r.get("pnl_impact")) for r in answered)
    top_tag = nodes[0]["tag"] if nodes else None

    return {
        "nodes": nodes,
        "prompts": prompts_out,
        "summary": {
            "total_prompts": len(answered),
            "total_loss_captured": round(total_loss, 2),
            "top_tag": top_tag,
            "window_days": days,
            "taxonomy": RATIONALE_TAGS,
        },
    }
