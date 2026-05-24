"""
Coach Q&A — manual AI-led check-in.

Companion to:
  • daily_coach_service  — panoramic, quantitative, scheduled
  • rationale_service     — single-shot, fires after a bad close

This module runs a SHORT (5-question) structured interview the user
triggers from the Coach page. The first question is anchored to the
highest-severity leak in today's report; subsequent questions adapt to
prior answers. After the budget runs out we ask Gemini to summarise +
extract tags, then close the session.

Why bother:
  Detectors see WHAT happened (sized losers big, breached SL on KRN);
  they can't see WHY (mood, conviction, distractions, missed setups).
  This Q&A captures the qualitative layer so leak trends can be paired
  with subjective context when reviewing weeks later.

LLM: Gemini 2.5 Flash Lite (cheap). Falls back to a static prompt pool
if the API fails so the flow still works offline.
"""
from __future__ import annotations

import json
import os
from datetime import date as date_cls, datetime
from typing import Optional

from services import daily_coach_service
from services.rationale_service import RATIONALE_TAGS

# Total questions to ask before auto-summarising. Five hits the sweet
# spot: enough to surface a real pattern, short enough that the user
# doesn't bail mid-flow.
QUESTION_BUDGET = 5

# Extra tags specific to the Q&A layer (mood, conviction, focus etc.)
# These augment the rationale taxonomy. Stored alongside rationale tags
# in the same `tags` array so the dashboard can show both.
QA_EXTRA_TAGS = [
    "tilt",                   # emotional dysregulation, chasing
    "fatigue",                # tired, low focus
    "low_conviction",         # took the trade despite doubt
    "high_conviction",        # confident call (good or bad outcome)
    "distracted",             # outside-market noise
    "missed_setup",           # saw the setup, didn't act
    "patience_test",          # held through chop, didn't flinch
    "rules_followed",         # full process adherence
    "premature_exit",         # got out before plan triggered
]

ALL_TAGS = sorted(set(RATIONALE_TAGS) | set(QA_EXTRA_TAGS))


# ────────────────────────────────────────────────────────────────────
#  Gemini helpers — next question + final summary
# ────────────────────────────────────────────────────────────────────

def _gemini_client():
    """Returns a Gemini client or None if no API key is configured."""
    api_key = os.getenv("api_key", "").strip()
    if not api_key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=api_key, http_options={"timeout": 30 * 1000})
    except Exception as exc:
        print(f"[coach_qa] gemini client init failed: {exc}")
        return None


def _coach_context_blurb(coach_report: Optional[dict]) -> str:
    """Compress the day's coach report into a short paragraph the LLM can use
    to anchor its questions. Keeps token count bounded — no raw JSON dump."""
    if not coach_report:
        return "No coach report exists for today yet — keep questions general."

    findings = coach_report.get("findings") or {}
    leaks = findings.get("leaks") or []
    market = findings.get("market") or {}
    window = findings.get("trades_window") or {}

    leak_lines = []
    for leak in leaks:
        sev = leak.get("severity", "low")
        head = leak.get("headline", leak.get("key", ""))
        leak_lines.append(f"  - [{sev.upper()}] {head}")

    parts = [
        f"Leak score: {coach_report.get('leak_score', 0)}/100",
        f"Market regime: {market.get('regime', 'unknown')}; "
        f"interpretation: {market.get('interpretation', 'n/a')}",
        f"7d window: {window.get('trades_closed', 0)} closed, "
        f"win {window.get('win_rate_pct', 0)}%, "
        f"net P&L ₹{int(window.get('net_pnl', 0)):,}",
    ]
    if leak_lines:
        parts.append("Active leaks:\n" + "\n".join(leak_lines))
    else:
        parts.append("No leaks fired today.")
    return "\n".join(parts)


def _format_transcript(transcript: list[dict]) -> str:
    """Compact transcript renderer for the LLM prompt."""
    if not transcript:
        return "(no prior turns)"
    lines = []
    for turn in transcript:
        role = "AI" if turn.get("role") == "ai" else "USER"
        content = (turn.get("content") or "").strip()
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _generate_next_question(coach_report: Optional[dict],
                            transcript: list[dict],
                            questions_remaining: int) -> str:
    """Ask Gemini for the next question. Falls back to a static prompt
    when the API is unavailable."""
    client = _gemini_client()
    if not client:
        return _fallback_question(coach_report, transcript)

    try:
        from google.genai import types

        prompt = (
            "You are a behavioral trading coach interviewing a discretionary "
            "swing trader at end-of-day. Your job: ask ONE concise, specific "
            "question that surfaces the WHY behind today's behavior — not "
            "what happened (the detectors already know that), but what was "
            "going on in their head.\n\n"
            "Rules:\n"
            "  • One question only. Plain text. No preamble. No options. "
            "Under 25 words.\n"
            "  • Anchor to the report's highest-severity leak first; pivot "
            "if the user's prior answer opens a more useful thread.\n"
            "  • Never moralise or lecture. Be a curious peer, not a parent.\n"
            "  • If transcript is empty, open with a question on the worst "
            "leak. If 1-2 turns deep, dig into a specific name or moment. "
            "If 3+ turns deep, pivot to mood / focus / setups skipped.\n\n"
            f"TODAY'S COACH REPORT CONTEXT:\n{_coach_context_blurb(coach_report)}\n\n"
            f"TRANSCRIPT SO FAR:\n{_format_transcript(transcript)}\n\n"
            f"Questions remaining after this one: {questions_remaining - 1}\n\n"
            "Next question:"
        )

        config = types.GenerateContentConfig(
            temperature=0.5,
            max_output_tokens=80,
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=config,
        )
        text = (resp.text or "").strip().strip('"').strip("'")
        # Strip a leading "Q:" or "Question:" prefix the model sometimes adds
        for prefix in ("Question:", "Q:", "Next question:"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].strip()
        return text or _fallback_question(coach_report, transcript)
    except Exception as exc:
        print(f"[coach_qa] gemini next-question failed: {exc}")
        return _fallback_question(coach_report, transcript)


def _fallback_question(coach_report: Optional[dict],
                       transcript: list[dict]) -> str:
    """Static fallback so the flow still works without an API key.
    Picks based on transcript depth + which leaks fired."""
    findings = (coach_report or {}).get("findings") or {}
    leaks = findings.get("leaks") or []
    high = [l for l in leaks if l.get("severity") == "high"]
    medium = [l for l in leaks if l.get("severity") == "medium"]
    primary = (high or medium or [{}])[0]
    leak_key = primary.get("key", "")

    depth = sum(1 for t in transcript if t.get("role") == "user")

    if depth == 0:
        opener = {
            "sizing_inversion": "Walk me through your sizing today — what made you commit big to the losers and small to the winners?",
            "sl_breach": "Which trade did you let run past your stop, and what made you hold?",
            "concurrency_overload": "You've been carrying a lot of names lately. What was your conviction on each at the moment of entry?",
            "early_exit_on_winners": "Tell me about a winner you exited too early — what was going through your head when you booked?",
            "winner_concentration": "Most of your P&L this FY came from one or two trades. Is that a process win or luck — and what's your honest read?",
        }.get(leak_key, "What was the most uncomfortable trade you took today, and why did you take it anyway?")
        return opener

    if depth == 1:
        return "Was that fully your plan, or did something in the moment shift it?"
    if depth == 2:
        return "How was your focus today — were you actually present, or distracted by something outside the market?"
    if depth == 3:
        return "Was there a setup you SAW today and didn't take? What stopped you?"
    return "If you could redo today with one rule firmly enforced, which rule and why?"


def _generate_summary_and_tags(transcript: list[dict],
                               coach_report: Optional[dict]) -> dict:
    """After the question budget is exhausted, ask Gemini for a 2-3 sentence
    summary + a tag list. Falls back to a stub on failure so the session
    still closes cleanly."""
    client = _gemini_client()
    if not client or not transcript:
        return {
            "summary": "Check-in complete. Re-open the session to add notes.",
            "tags": [],
        }

    try:
        from google.genai import types

        prompt = (
            "You are summarising a trader's end-of-day check-in. The trader "
            "answered 4–5 questions about their behavior today. Produce:\n"
            "  1. summary: 2-3 sentences in the trader's voice (use 'I', not "
            "'the trader'). Capture what they actually said — don't add "
            "advice or moralise.\n"
            "  2. tags: array of 1-6 tags from the FIXED taxonomy. Pick what "
            "actually applies — empty array is fine if nothing fits.\n\n"
            f"TAXONOMY: {json.dumps(ALL_TAGS)}\n\n"
            f"COACH REPORT CONTEXT:\n{_coach_context_blurb(coach_report)}\n\n"
            f"TRANSCRIPT:\n{_format_transcript(transcript)}\n\n"
            "Return ONLY JSON: { \"summary\": \"...\", \"tags\": [\"...\"] }"
        )

        config = types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=400,
            response_mime_type="application/json",
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=config,
        )
        raw = (resp.text or "").strip()
        if not raw:
            return {"summary": "", "tags": []}
        try:
            obj = json.loads(raw)
        except Exception:
            cleaned = raw.strip("`").strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            obj = json.loads(cleaned)

        summary = (obj.get("summary") or "").strip()
        tags_in = obj.get("tags") or []
        known = set(ALL_TAGS)
        tags = [t for t in tags_in if isinstance(t, str) and t in known]
        return {"summary": summary, "tags": tags}
    except Exception as exc:
        print(f"[coach_qa] gemini summary failed: {exc}")
        return {
            "summary": "Auto-summary unavailable. Re-open the session to view answers.",
            "tags": [],
        }


# ────────────────────────────────────────────────────────────────────
#  Persistence helpers
# ────────────────────────────────────────────────────────────────────

def _row_to_session(row) -> dict:
    return {
        "id": row[0],
        "user_id": str(row[1]),
        "session_date": row[2].isoformat() if row[2] else None,
        "coach_report_id": row[3],
        "status": row[4],
        "transcript": row[5] or [],
        "summary": row[6],
        "tags": list(row[7] or []),
        "questions_asked": row[8],
        "created_at": row[9].isoformat() if row[9] else None,
        "updated_at": row[10].isoformat() if row[10] else None,
        "completed_at": row[11].isoformat() if row[11] else None,
    }


_SESSION_COLS = (
    "id, user_id, session_date, coach_report_id, status, transcript, "
    "summary, tags, questions_asked, created_at, updated_at, completed_at"
)


def _fetch_session(cur, session_id: int, user_id: str) -> Optional[dict]:
    cur.execute(
        f"SELECT {_SESSION_COLS} FROM coach_qa_sessions "
        "WHERE id = %s AND user_id = %s",
        (session_id, user_id),
    )
    row = cur.fetchone()
    return _row_to_session(row) if row else None


def _coach_report_for_date(cur, user_id: str, on_date: date_cls) -> tuple[Optional[dict], Optional[int]]:
    """Returns (report_dict, report_id) — id is needed for FK on the session."""
    cur.execute(
        "SELECT id, findings, leak_score, adherence_streak, fy, report_date "
        "FROM daily_coach_reports "
        "WHERE user_id = %s AND report_date = %s "
        "ORDER BY id DESC LIMIT 1",
        (user_id, on_date),
    )
    row = cur.fetchone()
    if not row:
        return None, None
    report = {
        "id": row[0],
        "findings": row[1] or {},
        "leak_score": row[2],
        "adherence_streak": row[3] or {},
        "fy": row[4],
        "report_date": row[5].isoformat() if row[5] else None,
    }
    return report, row[0]


# ────────────────────────────────────────────────────────────────────
#  Public API — start / answer / fetch / list
# ────────────────────────────────────────────────────────────────────

def start_session(cur, user_id: str, on_date: Optional[date_cls] = None) -> dict:
    """Create a new in-progress session for `on_date` (default today).
    Auto-pulls the coach report for that date if one exists, then asks
    Gemini for the opener.

    Note: doesn't reuse an existing in-progress session — every call
    creates a new one. The frontend should warn if there's already an
    open one for the day."""
    on_date = on_date or date_cls.today()
    coach_report, coach_report_id = _coach_report_for_date(cur, user_id, on_date)

    first_q = _generate_next_question(coach_report, transcript=[], questions_remaining=QUESTION_BUDGET)
    now_iso = datetime.utcnow().isoformat() + "Z"
    transcript = [{"role": "ai", "content": first_q, "ts": now_iso}]

    cur.execute(
        """
        INSERT INTO coach_qa_sessions
            (user_id, session_date, coach_report_id, status, transcript, questions_asked)
        VALUES (%s, %s, %s, 'in_progress', %s, 1)
        RETURNING id, created_at, updated_at
        """,
        (user_id, on_date, coach_report_id, json.dumps(transcript)),
    )
    row = cur.fetchone()

    return {
        "id": row[0],
        "session_date": on_date.isoformat(),
        "coach_report_id": coach_report_id,
        "status": "in_progress",
        "transcript": transcript,
        "questions_asked": 1,
        "questions_remaining": QUESTION_BUDGET - 1,
        "next_question": first_q,
        "created_at": row[1].isoformat() if row[1] else None,
        "updated_at": row[2].isoformat() if row[2] else None,
    }


def submit_answer(cur, user_id: str, session_id: int, answer_text: str) -> dict:
    """Append the user's answer, then either:
      - Generate the next question (if budget remains), OR
      - Run the summariser and mark the session completed.

    Idempotent on accidental double-submits is best-effort: if the last
    transcript entry is already a USER turn, we still append (doc note,
    but in practice the frontend disables the button while in-flight)."""
    session = _fetch_session(cur, session_id, user_id)
    if not session:
        raise ValueError("Session not found")
    if session["status"] != "in_progress":
        raise ValueError(f"Session is {session['status']} — cannot submit")

    answer_clean = (answer_text or "").strip()
    if not answer_clean:
        raise ValueError("Empty answer")

    transcript = session["transcript"][:]
    now_iso = datetime.utcnow().isoformat() + "Z"
    transcript.append({"role": "user", "content": answer_clean, "ts": now_iso})

    questions_asked = session["questions_asked"]
    questions_remaining = QUESTION_BUDGET - questions_asked

    coach_report, _ = _coach_report_for_date(
        cur, user_id, date_cls.fromisoformat(session["session_date"])
    )

    done = questions_remaining <= 0

    if not done:
        next_q = _generate_next_question(coach_report, transcript, questions_remaining)
        transcript.append({"role": "ai", "content": next_q, "ts": datetime.utcnow().isoformat() + "Z"})
        questions_asked += 1

        cur.execute(
            """
            UPDATE coach_qa_sessions
            SET transcript = %s, questions_asked = %s
            WHERE id = %s AND user_id = %s
            """,
            (json.dumps(transcript), questions_asked, session_id, user_id),
        )

        return {
            "id": session_id,
            "status": "in_progress",
            "transcript": transcript,
            "questions_asked": questions_asked,
            "questions_remaining": QUESTION_BUDGET - questions_asked,
            "next_question": next_q,
            "done": False,
        }

    # Budget exhausted — summarise + close
    summary_obj = _generate_summary_and_tags(transcript, coach_report)
    summary = summary_obj.get("summary", "")
    tags = summary_obj.get("tags", [])

    cur.execute(
        """
        UPDATE coach_qa_sessions
        SET transcript = %s,
            summary = %s,
            tags = %s,
            status = 'completed',
            completed_at = NOW()
        WHERE id = %s AND user_id = %s
        """,
        (json.dumps(transcript), summary, tags, session_id, user_id),
    )

    return {
        "id": session_id,
        "status": "completed",
        "transcript": transcript,
        "questions_asked": questions_asked,
        "questions_remaining": 0,
        "summary": summary,
        "tags": tags,
        "done": True,
    }


def abandon_session(cur, user_id: str, session_id: int) -> bool:
    """Mark an in-progress session as abandoned (user closed the modal)."""
    cur.execute(
        """
        UPDATE coach_qa_sessions
        SET status = 'abandoned'
        WHERE id = %s AND user_id = %s AND status = 'in_progress'
        """,
        (session_id, user_id),
    )
    return cur.rowcount > 0


def get_session(cur, user_id: str, session_id: int) -> Optional[dict]:
    return _fetch_session(cur, session_id, user_id)


def list_sessions(cur, user_id: str, days: int = 30) -> list[dict]:
    """Light listing for the history strip — most recent first."""
    cur.execute(
        f"""
        SELECT {_SESSION_COLS} FROM coach_qa_sessions
        WHERE user_id = %s
          AND created_at >= NOW() - make_interval(days => %s)
        ORDER BY created_at DESC
        LIMIT 100
        """,
        (user_id, days),
    )
    return [_row_to_session(r) for r in cur.fetchall()]


def latest_for_date(cur, user_id: str, on_date: date_cls) -> Optional[dict]:
    """Most recent session for a given trading day (any status)."""
    cur.execute(
        f"""
        SELECT {_SESSION_COLS} FROM coach_qa_sessions
        WHERE user_id = %s AND session_date = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (user_id, on_date),
    )
    row = cur.fetchone()
    return _row_to_session(row) if row else None
