"""
Valvo AI v6 -- Main query engine.

Agentic tool-use loop: sends user message to Claude, executes tool calls,
feeds results back, repeats for up to 6 rounds until Claude produces a
final text response.
"""
from __future__ import annotations

import json
import traceback

from services.valvo_ai_v2.utils import to_jsonable

from .gateway import ModelGateway
from .prompts import build_system_prompt
from .tools import execute_tool, get_all_tool_definitions


# ---------------------------------------------------------------------------
# Chat history (re-uses the same chat_messages table with a v3 scope)
# ---------------------------------------------------------------------------

_HISTORY_PREFIX = "valvo-ai-v6"


def _history_scope(page_context: str | None = None) -> str:
    suffix = (page_context or "global").strip() or "global"
    return f"{_HISTORY_PREFIX}:{suffix}"


def _get_user_id():
    """Safely get user_id from Flask request context."""
    try:
        from flask import g
        return getattr(g, 'user_id', None)
    except RuntimeError:
        return None


def _load_history(page_context: str | None = None, limit: int = 10) -> list[dict]:
    from database.database import get_db

    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        if uid:
            cur.execute(
                """
                SELECT role, content
                FROM chat_messages
                WHERE page_context = %s AND (user_id = %s OR user_id IS NULL)
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (_history_scope(page_context), uid, limit),
            )
        else:
            cur.execute(
                """
                SELECT role, content
                FROM chat_messages
                WHERE page_context = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (_history_scope(page_context), limit),
            )
        rows = cur.fetchall()
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        print(f"[v3-engine] load_history failed: {e}")
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _save_message(role: str, content: str, page_context: str | None = None, stock_context: str | None = None):
    from database.database import get_db

    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        cur.execute(
            """
            INSERT INTO chat_messages (role, content, page_context, stock_context, user_id)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (role, content, _history_scope(page_context), stock_context, uid),
        )
        conn.commit()
    except Exception as e:
        print(f"[v3-engine] save_message failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _clear_history(page_context: str | None = None) -> dict:
    from database.database import get_db

    conn = get_db()
    if not conn:
        return {"cleared": False, "error": "database unavailable"}
    try:
        cur = conn.cursor()
        uid = _get_user_id()
        if uid:
            cur.execute(
                "DELETE FROM chat_messages WHERE page_context = %s AND (user_id = %s OR user_id IS NULL)",
                (_history_scope(page_context), uid),
            )
        else:
            cur.execute(
                "DELETE FROM chat_messages WHERE page_context = %s",
                (_history_scope(page_context),),
            )
        deleted = cur.rowcount
        conn.commit()
        return {"cleared": True, "deleted": deleted}
    except Exception as exc:
        print(f"[v3-engine] clear_history failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass
        return {"cleared": False, "error": str(exc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 12


def _normalize_history_override(history_override: list[dict] | None) -> list[dict]:
    normalized = []
    for item in history_override or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        normalized.append({"role": role, "content": text})
    return normalized


def _is_action_tool(tool_name: str) -> bool:
    if tool_name in {"confirm_action", "cancel_action"}:
        return True
    from services.valvo_ai_v2.actions import ACTIONS as V2_ACTIONS

    return tool_name in V2_ACTIONS


class ValvoAIV6Engine:
    """Agentic Valvo AI engine -- Claude + multi-round tool loop."""

    def __init__(self):
        self.gateway = ModelGateway()

    # -- public --

    def health(self) -> dict:
        from database.database import get_db

        checks = self.gateway.health()
        checks["tools"] = len(get_all_tool_definitions())
        conn = get_db()
        checks["database"] = "connected" if conn else "failed"
        if conn:
            conn.close()
        return checks

    def clear_history(self, page_context: str | None = None) -> dict:
        return _clear_history(page_context)

    def query(
        self,
        message: str,
        page_context: str = "",
        stock_context: str = "",
        voice: bool = False,
        model: str | None = None,
        load_history: bool = True,
        persist_history: bool = True,
        history_override: list[dict] | None = None,
    ) -> dict:
        if not message or not message.strip():
            return {"error": "message is required"}

        if not self.gateway.available():
            return {"error": "No Valvo AI model provider is configured"}

        # 1. Build system prompt
        system = build_system_prompt(user_id=_get_user_id())

        # 2. Load chat history
        if history_override is not None:
            history = _normalize_history_override(history_override)
        else:
            history = _load_history(page_context, limit=24) if load_history else []

        # 3. Build messages
        messages: list = history + [{"role": "user", "content": message}]

        # 4. Tool definitions
        tools = get_all_tool_definitions()

        # 5. Token budget
        max_tokens = 300 if voice else 8192

        # 6. Tool loop
        resolved_model = self.gateway.resolve_model(model)
        response_text = ""
        pending_action = None
        tool_results_log: list[dict] = []
        usage_input = 0
        usage_output = 0
        result = None
        skip_history_reuse = False

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                result = self.gateway.create_message(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    tools=tools,
                )

                # Accumulate usage
                usage_input += result.input_tokens
                usage_output += result.output_tokens

                # Check for final text response
                if result.stop_reason == "end_turn":
                    response_text = result.text
                    break

                if result.stop_reason == "tool_use":
                    # Process all tool calls in this round
                    tool_result_blocks = []
                    for tc in result.tool_calls:
                        tool_name = tc.name
                        tool_input = tc.input

                        # Execute the tool
                        tool_output = execute_tool(tool_name, tool_input)

                        # Track pending actions
                        if isinstance(tool_output, dict) and tool_output.get("requires_confirmation"):
                            pending_action = tool_output.get("pending_action")

                        # Log for response metadata
                        tool_results_log.append({
                            "tool": tool_name,
                            "input": tool_input,
                            "ok": not bool((tool_output or {}).get("error")),
                        })
                        if _is_action_tool(tool_name):
                            skip_history_reuse = True

                        # Compact serialization for token efficiency
                        content_str = json.dumps(
                            to_jsonable(tool_output), default=str, ensure_ascii=False
                        )
                        # Hard cap to avoid blowing context
                        if len(content_str) > 12000:
                            content_str = content_str[:12000] + '..."}'

                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": content_str,
                        })

                    # Append assistant turn + tool results to conversation
                    messages.append({"role": "assistant", "content": result.to_message_content()})
                    messages.append({"role": "user", "content": tool_result_blocks})
                else:
                    # Unexpected stop reason -- extract whatever text is there
                    response_text = result.text or "I could not generate a response."
                    break
            else:
                # Exhausted all rounds; extract partial text
                response_text = result.text if result else "Tool loop exhausted without a final response."

            # Force-final-answer pass — same as v3. Catches the case where the
            # loop ends at end_turn with empty text after at least one tool.
            if not (response_text or "").strip() and tool_results_log:
                try:
                    fallback = self.gateway.create_message(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        messages=messages + [{
                            "role": "user",
                            "content": "Please compose your final answer to my last question now in plain English, using the tool results you already have. No additional tool calls.",
                        }],
                        tools=[],
                    )
                    if (fallback.text or "").strip():
                        response_text = fallback.text
                        usage_input += fallback.input_tokens
                        usage_output += fallback.output_tokens
                except Exception as exc:
                    print(f"[engine] force-final-answer pass failed: {exc}")

            if not (response_text or "").strip():
                response_text = (
                    "I ran the tools but couldn't produce a final answer. "
                    "Try rephrasing — e.g. include a stock symbol, price, or quantity."
                )

        except Exception as exc:
            traceback.print_exc()
            return {"error": f"Engine error: {exc}"}

        # 7. Save to history
        skip_history_reuse = skip_history_reuse or bool(pending_action)
        if response_text and persist_history and not skip_history_reuse:
            _save_message("user", message, page_context=page_context, stock_context=stock_context)
            _save_message("assistant", response_text, page_context=page_context, stock_context=stock_context)

        # 8. Return
        return {
            "response": response_text,
            "model": resolved_model,
            "requires_confirmation": bool(pending_action),
            "pending_action": pending_action,
            "tool_calls": tool_results_log,
            "input_tokens": usage_input,
            "output_tokens": usage_output,
            "skip_history_reuse": skip_history_reuse,
        }

    def query_stream(
        self,
        message: str,
        page_context: str = "",
        stock_context: str = "",
        voice: bool = False,
        model: str | None = None,
        load_history: bool = True,
        persist_history: bool = True,
        history_override: list[dict] | None = None,
    ):
        """Generator that yields SSE events for real-time tool-step display."""
        import time

        if not message or not message.strip():
            yield _sse({"type": "error", "message": "message is required"})
            return

        if not self.gateway.available():
            yield _sse({"type": "error", "message": "Anthropic API key not configured"})
            return

        system = build_system_prompt(user_id=_get_user_id())
        if history_override is not None:
            history = _normalize_history_override(history_override)
        else:
            history = _load_history(page_context, limit=24) if load_history else []
        messages: list = history + [{"role": "user", "content": message}]
        tools = get_all_tool_definitions()
        max_tokens = 300 if voice else 8192
        resolved_model = self.gateway.resolve_model(model)
        pending_action = None
        result = None
        skip_history_reuse = False

        yield _sse({"type": "status", "step": "thinking", "detail": "Understanding your question..."})

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                if _round == 0:
                    yield _sse({"type": "status", "step": "reasoning", "detail": "Thinking..."})
                else:
                    yield _sse({"type": "status", "step": "reasoning", "detail": "Analyzing results..."})

                result = self.gateway.create_message(
                    model=model, max_tokens=max_tokens,
                    system=system, messages=messages, tools=tools,
                )

                if result.stop_reason == "end_turn":
                    response_text = result.text or ""
                    if not response_text.strip() and _round > 0:
                        try:
                            fallback = self.gateway.create_message(
                                model=model,
                                max_tokens=max_tokens,
                                system=system,
                                messages=messages + [{
                                    "role": "user",
                                    "content": "Please compose your final answer to my last question now in plain English, using the tool results you already have. No additional tool calls.",
                                }],
                                tools=[],
                            )
                            if (fallback.text or "").strip():
                                response_text = fallback.text
                        except Exception as exc:
                            print(f"[engine] stream end_turn force-final-answer failed: {exc}")
                    if not response_text.strip():
                        response_text = (
                            "I couldn't produce a final answer. Try rephrasing — "
                            "e.g. include a stock symbol, price, or quantity."
                        )
                    skip_history_reuse = skip_history_reuse or bool(pending_action)
                    yield _sse({
                        "type": "answer",
                        "response": response_text,
                        "requires_confirmation": bool(pending_action),
                        "pending_action": pending_action,
                        "skip_history_reuse": skip_history_reuse,
                    })
                    if persist_history and not skip_history_reuse:
                        _save_message("user", message, page_context=page_context, stock_context=stock_context)
                        _save_message("assistant", response_text, page_context=page_context, stock_context=stock_context)
                    return

                if result.stop_reason == "tool_use":
                    tool_result_blocks = []
                    for tc in result.tool_calls:
                        tool_name = tc.name
                        tool_input = tc.input

                        # Emit step event BEFORE executing
                        step_detail = _describe_tool_step(tool_name, tool_input)
                        yield _sse({"type": "tool_start", "tool": tool_name, "detail": step_detail})

                        tool_output = execute_tool(tool_name, tool_input)
                        if _is_action_tool(tool_name):
                            skip_history_reuse = True

                        if isinstance(tool_output, dict) and tool_output.get("requires_confirmation"):
                            pending_action = tool_output.get("pending_action")

                        # Emit completion
                        ok = not bool((tool_output or {}).get("error"))
                        rows = tool_output.get("row_count") if isinstance(tool_output, dict) else None
                        yield _sse({"type": "tool_done", "tool": tool_name, "ok": ok, "rows": rows})

                        content_str = json.dumps(to_jsonable(tool_output), default=str, ensure_ascii=False)
                        if len(content_str) > 12000:
                            content_str = content_str[:12000] + '..."}'

                        tool_result_blocks.append({"type": "tool_result", "tool_use_id": tc.id, "content": content_str})

                    messages.append({"role": "assistant", "content": result.to_message_content()})
                    messages.append({"role": "user", "content": tool_result_blocks})
                else:
                    response_text = result.text or "Could not generate a response."
                    yield _sse({"type": "answer", "response": response_text})
                    return

            # Tool-round budget exhausted. Force-final-answer if blank.
            final_text = (result.text if result else "") or ""
            if not final_text.strip():
                try:
                    fallback = self.gateway.create_message(
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        messages=messages + [{
                            "role": "user",
                            "content": "Please compose your final answer to my last question now in plain English, using the tool results you already have. No additional tool calls.",
                        }],
                        tools=[],
                    )
                    if (fallback.text or "").strip():
                        final_text = fallback.text
                except Exception as exc:
                    print(f"[engine] stream force-final-answer failed: {exc}")
            if not final_text.strip():
                final_text = (
                    "I ran the tools but couldn't produce a final answer. "
                    "Try rephrasing — e.g. include a stock symbol, price, or quantity."
                )
            yield _sse({"type": "answer", "response": final_text})

        except Exception as exc:
            traceback.print_exc()
            yield _sse({"type": "error", "message": f"Engine error: {exc}"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(result) -> str:
    """Pull all TextBlock content from an Anthropic message response."""
    parts = []
    for block in (result.content or []):
        if hasattr(block, "text") and block.text:
            parts.append(block.text)
    return "\n".join(parts)


def _sse(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


def _describe_tool_step(tool_name: str, tool_input: dict) -> str:
    """Clean, user-friendly step description. No table names or internal details."""
    if tool_name == "sql_query":
        q = (tool_input.get("query") or "").lower()
        # Detect intent from SQL keywords
        if "win" in q and "rate" in q or "is_winner" in q:
            return "Calculating win rate..."
        if "realized_pl" in q and ("sum" in q or "total" in q):
            return "Computing total P&L..."
        if "count" in q and "group by" not in q:
            return "Counting trades..."
        if "month_label" in q and "group by" in q:
            return "Breaking down by month..."
        if any(f"fy{y}" in q.replace("_", "").replace("-", "") for y in ["2122", "2223", "2324", "2425"]):
            fys = []
            if "2122" in q.replace("_", ""): fys.append("21-22")
            if "2223" in q.replace("_", ""): fys.append("22-23")
            if "2324" in q.replace("_", ""): fys.append("23-24")
            if "2425" in q.replace("_", ""): fys.append("24-25")
            if "legacy_trades " in q or "legacy_trades\n" in q: fys.append("25-26")
            if len(fys) > 1:
                return f"Searching across FY {fys[0]} to {fys[-1]}..."
            elif fys:
                return f"Looking up FY {fys[0]} trades..."
        if "position" in q:
            return "Checking portfolio positions..."
        if "candles" in q or "close" in q and "date" in q:
            return "Fetching market data..."
        if "order by" in q and "desc" in q and "limit" in q:
            return "Finding top results..."
        if "union all" in q:
            return "Searching across multiple years..."
        return "Querying trading data..."
    elif tool_name == "get_analytics":
        ep = tool_input.get("endpoint", "full")
        fy = tool_input.get("fy", "2025-26")
        labels = {"full": "performance stats", "outliers": "outlier analysis", "advanced-v2": "advanced analytics"}
        return f"Loading {labels.get(ep, ep)} for FY {fy}..."
    elif tool_name == "get_equity_curve":
        t = tool_input.get("type", "long-term")
        return "Building equity curve..." if t == "long-term" else "Analyzing drawdowns..."
    elif tool_name == "get_live_market":
        syms = tool_input.get("symbols", [])
        if syms:
            return f"Checking live price for {', '.join(syms[:2])}{'...' if len(syms) > 2 else ''}"
        return "Fetching market data..."
    elif tool_name == "get_positions":
        return "Loading portfolio..."
    elif tool_name == "search_stock":
        return f"Looking up {tool_input.get('query', 'stock')}..."
    else:
        # Action tools
        clean = tool_name.replace("_", " ").title()
        return f"{clean}..."


def _safe_input(tool_input: dict) -> dict:
    """Truncate large inputs for the SSE event."""
    safe = {}
    for k, v in tool_input.items():
        if isinstance(v, str) and len(v) > 200:
            safe[k] = v[:200] + "..."
        else:
            safe[k] = v
    return safe
