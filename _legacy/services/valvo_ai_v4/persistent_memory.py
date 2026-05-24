"""
Valvo AI v4 -- Persistent Memory (Phase 7)

Based on Mem0 (2025) — persistent facts extracted from conversations, retrieved
across sessions. 67% accuracy on LOCOMO benchmark, 80% token reduction vs
full-context methods.

Three memory types:
1. Episodic  — "Last Tuesday you analyzed FY 24-25 pharma trades"
2. Semantic  — "User prefers momentum strategy, 4% SL, breakouts"
3. Procedural — "BSE fundamentals: use sql_query on financials_quarterly, not overview table"

Storage: user_memory table (or chat_messages with role='user_fact' as fallback).
"""
from __future__ import annotations

import json
import re
from datetime import date

from database.database import get_db, close_db
from .gateway import GeminiFlashGateway, FLASH_LITE_MODEL


_MEMORY_CONTEXT = "valvo-ai-v4-memory"


def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  Fact extraction from conversations
# ═══════════════════════════════════════════════════════════════════════════

_EXTRACTION_SYSTEM = """\
You extract PERSISTENT FACTS about a user from a trading conversation.

Only extract facts that are TRUE LONG-TERM preferences, habits, or patterns —
NOT one-time queries or transient information.

Categories:
- preference: trading style, risk tolerance, favorite sectors, preferred timeframes
- pattern: common query types, frequently checked stocks, recurring workflows
- profile: capital size, stoploss %, trading experience

Output format: JSON array of facts, each with:
- "category": "preference" | "pattern" | "profile"
- "fact": one concise sentence (max 100 chars)
- "confidence": "high" | "medium" | "low"

If no persistent facts can be extracted, return {"facts": []}.
Return ONLY valid JSON, no markdown.

Examples of GOOD extraction:
- {"category": "preference", "fact": "Frequently tracks pharma and IT sector stocks", "confidence": "medium"}
- {"category": "pattern", "fact": "Checks win rate for specific FYs more than P&L", "confidence": "high"}
- {"category": "profile", "fact": "Trades with 4% stoploss, breakout setups", "confidence": "high"}

BAD extraction (don't do this):
- "User asked about TCS" (transient, not a preference)
- "Portfolio P&L was Rs 5L" (point-in-time data, not a fact)
"""


def extract_facts_from_conversation(
    user_message: str,
    assistant_response: str,
    tool_calls: list[dict],
    gateway: GeminiFlashGateway,
) -> list[dict]:
    """Extract persistent facts from a conversation using Flash Lite."""
    if not gateway.available():
        return []

    # Only extract from substantive conversations (had tool calls)
    if not tool_calls:
        return []

    user_short = user_message[:300]
    assistant_short = assistant_response[:600]
    tools_summary = ", ".join({tc.get("tool", "") for tc in tool_calls})

    user_prompt = f"""\
User said: "{user_short}"
Assistant response: "{assistant_short}"
Tools used: {tools_summary}

Extract persistent facts (if any). Return JSON.
"""

    try:
        result = gateway.create_message(
            model_id=FLASH_LITE_MODEL,
            max_tokens=400,
            system=_EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[],
        )
        text = (result.text or "").strip()
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.startswith("json"):
                text = text[4:].strip()
        parsed = json.loads(text)

        # Support both formats
        if isinstance(parsed, dict) and "facts" in parsed:
            return parsed["facts"] or []
        if isinstance(parsed, list):
            return parsed
        return []
    except Exception as e:
        print(f"[memory] fact extraction failed: {e}")
        return []


def save_facts(facts: list[dict]) -> int:
    """Save extracted facts to the database. Deduplicates against existing facts."""
    if not facts:
        return 0

    uid = _get_user_id()
    if not uid:
        return 0

    conn = get_db()
    if not conn:
        return 0

    saved = 0
    try:
        cur = conn.cursor()

        # Get existing facts for this user to dedupe
        cur.execute(
            "SELECT content FROM chat_messages "
            "WHERE role = 'user_fact' AND user_id = %s "
            "ORDER BY created_at DESC LIMIT 100",
            (uid,),
        )
        existing = {r["content"] for r in cur.fetchall()}

        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fact_text = fact.get("fact", "").strip()
            if not fact_text or len(fact_text) < 10:
                continue
            category = fact.get("category", "preference")
            confidence = fact.get("confidence", "medium")

            # Skip low confidence
            if confidence == "low":
                continue

            # Encode metadata in content
            content = f"[{category}] {fact_text}"

            # Dedupe by exact match
            if content in existing:
                continue

            cur.execute(
                "INSERT INTO chat_messages (role, content, page_context, user_id) "
                "VALUES (%s, %s, %s, %s)",
                ("user_fact", content, _MEMORY_CONTEXT, uid),
            )
            saved += 1

        conn.commit()
        return saved
    except Exception as e:
        print(f"[memory] save_facts failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        close_db(conn)


def extract_and_save_facts_async(
    user_message: str,
    assistant_response: str,
    tool_calls: list[dict],
    gateway: GeminiFlashGateway,
) -> None:
    """Extract and save facts in a background thread."""
    import threading

    def _run():
        try:
            facts = extract_facts_from_conversation(
                user_message, assistant_response, tool_calls, gateway,
            )
            if facts:
                count = save_facts(facts)
                if count:
                    print(f"[memory] Saved {count} new facts")
        except Exception as e:
            print(f"[memory] extract_and_save failed: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════
#  Fact retrieval
# ═══════════════════════════════════════════════════════════════════════════

def get_user_facts(limit: int = 15) -> list[str]:
    """Get all persistent facts for the current user, most recent first."""
    uid = _get_user_id()
    if not uid:
        return []

    conn = get_db()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT content FROM chat_messages "
            "WHERE role = 'user_fact' AND user_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (uid, limit),
        )
        return [r["content"] for r in cur.fetchall()]
    except Exception as e:
        print(f"[memory] get_user_facts failed: {e}")
        return []
    finally:
        close_db(conn)


def format_facts_for_prompt(facts: list[str]) -> str:
    """Format facts for injection into the system prompt."""
    if not facts:
        return ""

    # Group by category
    grouped: dict[str, list[str]] = {"preference": [], "pattern": [], "profile": []}
    for f in facts:
        match = re.match(r"\[(\w+)\]\s*(.+)", f)
        if match:
            cat = match.group(1)
            text = match.group(2)
            if cat in grouped:
                grouped[cat].append(text)

    if not any(grouped.values()):
        return ""

    lines = ["WHAT I KNOW ABOUT THIS USER (use to personalize responses):"]
    if grouped["profile"]:
        lines.append("Profile:")
        for f in grouped["profile"][:5]:
            lines.append(f"  - {f}")
    if grouped["preference"]:
        lines.append("Preferences:")
        for f in grouped["preference"][:5]:
            lines.append(f"  - {f}")
    if grouped["pattern"]:
        lines.append("Usage patterns:")
        for f in grouped["pattern"][:5]:
            lines.append(f"  - {f}")

    return "\n".join(lines)
