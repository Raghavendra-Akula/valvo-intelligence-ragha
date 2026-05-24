"""
Valvo AI v4 -- Reflexion loop (Phase 3)

Based on Reflexion (Shinn et al., NeurIPS 2023) — verbal reinforcement learning.

When tools fail or return unexpected results, the agent generates a natural-language
reflection explaining WHY, and what to do differently next time. These reflections
are stored per-user and retrieved on similar future queries.

Result: AI gets smarter across sessions without any model retraining.

Storage: reuses chat_messages table with role='reflection'
"""
from __future__ import annotations

import json
import re
from database.database import get_db, close_db

from .gateway import GeminiFlashGateway, FLASH_LITE_MODEL


_HISTORY_PREFIX = "valvo-ai-v4-reflection"


def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


def _keywords(text: str) -> set[str]:
    """Extract meaningful keywords from text for similarity matching."""
    text = text.lower()
    # Remove common words
    stop = {
        "the", "a", "an", "of", "to", "in", "for", "and", "or", "is", "was",
        "my", "me", "i", "you", "what", "how", "show", "get", "give", "all",
        "this", "that", "which", "with", "from", "on", "by",
    }
    words = re.findall(r"\b[a-z][a-z0-9_]{2,}\b", text)
    return {w for w in words if w not in stop}


# ═══════════════════════════════════════════════════════════════════════════
#  Failure recording
# ═══════════════════════════════════════════════════════════════════════════

def record_failure(
    user_message: str,
    tool_name: str,
    tool_input: dict,
    error: str,
    gateway: GeminiFlashGateway,
) -> None:
    """
    Generate a reflection on a tool failure and store it for future learning.
    Runs in a background thread so it doesn't block the response.
    """
    if not gateway.available():
        return

    import threading

    def _generate_and_store():
        try:
            reflection = _generate_reflection(user_message, tool_name, tool_input, error, gateway)
            if reflection:
                _store_reflection(user_message, tool_name, reflection)
        except Exception as e:
            print(f"[reflexion] record_failure failed: {e}")

    threading.Thread(target=_generate_and_store, daemon=True).start()


def _generate_reflection(
    user_message: str,
    tool_name: str,
    tool_input: dict,
    error: str,
    gateway: GeminiFlashGateway,
) -> str | None:
    """Generate a concise reflection using Flash Lite."""
    system = """\
You are analyzing a tool failure in a trading AI. Generate a ONE-LINE reflection that describes:
1. What went wrong (briefly)
2. What approach should be tried instead next time

Format: "When <situation>, <tool/approach> fails because <reason>. Instead, use <alternative>."
Keep it under 200 characters. Be specific but concise.
"""
    user_prompt = f"""\
User asked: "{user_message}"
Tool called: {tool_name}
Parameters: {json.dumps(tool_input)[:200]}
Error: {error[:300]}

Reflection:"""

    try:
        result = gateway.create_message(
            model_id=FLASH_LITE_MODEL,
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[],
        )
        reflection = (result.text or "").strip()
        if reflection and len(reflection) > 20:
            return reflection[:300]
        return None
    except Exception:
        return None


def _store_reflection(user_message: str, tool_name: str, reflection: str) -> None:
    """Store reflection in chat_messages table."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        uid = _get_user_id()

        # Encode metadata in the content for later retrieval
        # Format: [tool] keywords: word1, word2 | reflection text
        keywords = " ".join(sorted(_keywords(user_message))[:10])
        content = f"[{tool_name}] keywords: {keywords} | {reflection}"

        cur.execute(
            "INSERT INTO chat_messages (role, content, page_context, user_id) "
            "VALUES (%s, %s, %s, %s)",
            ("reflection", content, _HISTORY_PREFIX, uid),
        )
        conn.commit()
    except Exception as e:
        print(f"[reflexion] _store_reflection failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        close_db(conn)


# ═══════════════════════════════════════════════════════════════════════════
#  Reflection retrieval
# ═══════════════════════════════════════════════════════════════════════════

def get_relevant_reflections(user_message: str, limit: int = 3) -> list[str]:
    """
    Retrieve reflections relevant to the current query using keyword overlap.
    Returns list of reflection texts (without metadata).
    """
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        if not uid:
            return []

        # Fetch user's recent reflections (last 50)
        cur.execute(
            "SELECT content FROM chat_messages "
            "WHERE role = 'reflection' AND user_id = %s "
            "ORDER BY created_at DESC LIMIT 50",
            (uid,),
        )
        rows = cur.fetchall()
        if not rows:
            return []

        # Score each by keyword overlap with current message
        query_kw = _keywords(user_message)
        scored = []
        for r in rows:
            content = r["content"] or ""
            # Parse format: [tool] keywords: w1 w2 | reflection
            kw_match = re.search(r"keywords: ([^|]+) \|", content)
            if not kw_match:
                continue
            stored_kw = set(kw_match.group(1).strip().split())
            overlap = len(query_kw & stored_kw)
            if overlap >= 1:
                # Extract just the reflection text
                reflection_text = content.split("|", 1)[1].strip() if "|" in content else content
                scored.append((overlap, reflection_text))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [text for _, text in scored[:limit]]
    except Exception as e:
        print(f"[reflexion] get_relevant_reflections failed: {e}")
        return []
    finally:
        close_db(conn)


def format_reflections_for_prompt(reflections: list[str]) -> str:
    """Format reflections as a prompt injection block."""
    if not reflections:
        return ""
    lines = ["LEARNED FROM PAST FAILURES (avoid repeating these):"]
    for r in reflections:
        lines.append(f"- {r}")
    return "\n".join(lines)
