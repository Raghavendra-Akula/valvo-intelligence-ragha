"""
Valvo AI v4 -- Pre-Act Planner (Phase 1)

For complex queries, generates a multi-step execution plan BEFORE tool calls begin.
Prevents the "gives up after 2 tools" problem by planning the full chain upfront.

Based on Pre-Act paper (EMNLP 2025) — 70% better action recall vs plain ReAct.
Uses Flash Lite for cheap planning (~200 tokens).
"""
from __future__ import annotations

import json
from typing import Any

from .gateway import GeminiFlashGateway, FLASH_LITE_MODEL


_PLANNER_SYSTEM = """\
You are a query planner for a trading AI assistant. Given a user's question, you create a clear step-by-step plan that another AI will execute.

Your plans should:
1. Identify what data is needed and from which tools
2. Handle multi-entity queries (e.g., "for each stock in my watchlist")
3. Anticipate dependencies (step 3 needs output of step 1)
4. NEVER say "this is not possible" — there's always sql_query as a fallback
5. Be concise — 3-6 steps maximum

Output format: JSON with a "plan" array of step objects, each having:
- "step": sequential number
- "action": tool name or "reasoning"
- "description": what this step accomplishes
- "depends_on": step numbers this depends on (empty if none)

Example for "Get fundamentals of all stocks in my watchlist and positions":
{
  "plan": [
    {"step": 1, "action": "get_watchlist", "description": "Fetch user's watchlist stocks", "depends_on": []},
    {"step": 2, "action": "get_positions", "description": "Fetch active portfolio positions", "depends_on": []},
    {"step": 3, "action": "reasoning", "description": "Deduplicate stock list from steps 1 and 2", "depends_on": [1, 2]},
    {"step": 4, "action": "get_fundamentals", "description": "Call get_fundamentals for EACH unique stock (loop)", "depends_on": [3]},
    {"step": 5, "action": "reasoning", "description": "Compile all fundamentals into a comparison table", "depends_on": [4]}
  ]
}
"""


def create_plan(
    message: str,
    tools: list[dict],
    gateway: GeminiFlashGateway,
) -> dict | None:
    """
    Generate an execution plan for a complex query.
    Returns dict with "plan" array, or None if planning fails.
    """
    if not gateway.available():
        return None

    tool_names = [t["name"] for t in tools]
    tool_list_str = ", ".join(tool_names)

    user_prompt = f"""\
User question: "{message}"

Available tools: {tool_list_str}

Create a step-by-step plan. Output ONLY valid JSON, no markdown, no prose.
"""

    try:
        result = gateway.create_message(
            model_id=FLASH_LITE_MODEL,
            max_tokens=600,
            system=_PLANNER_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[],  # no tools for planning itself
        )

        text = (result.text or "").strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text else text
            if text.startswith("json"):
                text = text[4:].strip()

        plan = json.loads(text)
        if "plan" in plan and isinstance(plan["plan"], list):
            return plan
        return None
    except Exception as e:
        print(f"[planner] Plan generation failed: {e}")
        return None


def format_plan_for_prompt(plan: dict) -> str:
    """Convert plan dict into a human-readable block for the system prompt."""
    if not plan or "plan" not in plan:
        return ""

    lines = ["EXECUTION PLAN (follow this sequence):"]
    for step in plan["plan"]:
        n = step.get("step", "?")
        action = step.get("action", "?")
        desc = step.get("description", "")
        deps = step.get("depends_on", [])
        deps_str = f" [after steps {deps}]" if deps else ""
        lines.append(f"{n}. [{action}]{deps_str} {desc}")

    lines.append("\nExecute each step in order. Don't skip steps. If step N depends on step M, use the output of M.")
    return "\n".join(lines)
