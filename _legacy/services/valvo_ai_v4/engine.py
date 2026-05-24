"""
Valvo AI v4 -- Main query engine with all 7 intelligence phases.

Orchestrates:
  Phase 1 — Pre-Act planner (complex queries get planned upfront)
  Phase 2 — SQL validator with auto-retry (fixes 70% of SQL failures)
  Phase 3 — Reflexion (learn from failures, avoid repeating)
  Phase 4 — Semantic router (embedding-based intent classification)
  Phase 5 — Specialist agents (intent-specific prompts + tool subsets)
  Phase 6 — Schema selector (focused schema context per query)
  Phase 7 — Persistent memory (user facts extracted across sessions)

All phases run within a single LLM turn — no extra roundtrips for routing
or planning unless the query is complex enough to benefit.
"""
from __future__ import annotations

import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.valvo_ai_v2.utils import to_jsonable

from .gateway import GeminiFlashGateway, FLASH_LITE_MODEL
from .prompts import build_system_prompt
from .tools import execute_tool, get_all_tool_definitions, get_simple_tool_definitions, is_action_tool
from .memory import (
    load_history,
    save_message,
    clear_history,
    load_memory_context,
    normalize_history_override,
    generate_and_save_summary,
)

# Phase modules
from .planner import create_plan, format_plan_for_prompt
from .sql_validator import execute_validated_sql
from .reflexion import record_failure, get_relevant_reflections, format_reflections_for_prompt
from .semantic_router import route_query
from .agents import get_agent_config
from .schema_selector import select_tables, build_focused_schema
from .persistent_memory import (
    extract_and_save_facts_async,
    get_user_facts,
    format_facts_for_prompt,
)


MAX_TOOL_ROUNDS = 12

# Shared thread pool for parallel tool execution.
# Kept small so we don't exhaust the DB connection pool (5 min / 10 max).
_TOOL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="valvo-tool")


def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


class ValvoAIV4Engine:
    """Intelligent Valvo AI v4 with all 7 SOTA enhancements."""

    def __init__(self):
        self.gateway = GeminiFlashGateway()
        # Preload stock embeddings in background (ready for first search)
        try:
            from .stock_embeddings import preload_async
            preload_async()
        except Exception:
            pass

    # ── Public API ──────────────────────────────────────────────────────

    def health(self) -> dict:
        from database.database import get_db, close_db

        checks = self.gateway.health()
        checks["version"] = "v4"
        checks["tools"] = len(get_all_tool_definitions())
        checks["phases"] = [
            "Pre-Act planner",
            "SQL validator + retry",
            "Reflexion",
            "Semantic router",
            "Specialist agents",
            "Schema selector",
            "Persistent memory",
        ]
        conn = get_db()
        checks["database"] = "connected" if conn else "failed"
        if conn:
            close_db(conn)
        return checks

    def clear_history(self, page_context: str | None = None) -> dict:
        return clear_history(page_context)

    def _synthesize_final_text(
        self,
        result,
        *,
        model_id: str,
        system: str,
        messages: list,
    ) -> str:
        """Guarantee a non-empty user-facing reply.

        Flash Lite occasionally exits the tool loop with empty text after a
        successful tool call (budget eaten by the tool transcript). If that
        happens and we have tool results in the history, make one more call
        with tools disabled and an explicit summarize instruction. If even
        that fails, fall back to a deterministic message so the UI never
        renders an empty bubble.
        """
        text = (result.text if result else "") or ""
        if text.strip():
            return text

        has_tool_results = any(
            isinstance(m.get("content"), list)
            and any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in m["content"]
            )
            for m in messages
        )
        if not has_tool_results:
            return "I couldn't generate a response. Please try rephrasing the question."

        nudge = (
            "Summarize the tool results above in a concise, user-facing answer. "
            "Do not call any more tools. If the data is empty, say so plainly "
            "and suggest the next step."
        )
        try:
            retry = self.gateway.create_message(
                model_id=model_id,
                max_tokens=1024,
                system=system,
                messages=messages + [{"role": "user", "content": nudge}],
                tools=None,
            )
            if retry and (retry.text or "").strip():
                return retry.text
        except Exception as exc:
            print(f"[engine] final-synthesis retry failed: {exc}")

        return (
            "I pulled the data but couldn't compose a summary. "
            "Please retry or rephrase the question."
        )

    # ── Orchestration helpers ───────────────────────────────────────────

    def _orchestrate(
        self,
        message: str,
        voice: bool,
        history: list[dict] | None = None,
        prior_intent: str | None = None,
    ) -> dict:
        """
        Run all pre-execution phases. Each phase is wrapped in try/except —
        if any phase fails, the engine continues with sensible defaults.

        history + prior_intent are passed to the semantic router so
        follow-ups route correctly. When the router returns
        requires_clarification, we short-circuit and return early — the
        caller is responsible for emitting the clarify event.
        """
        # Phase 4: Semantic router (context-aware)
        try:
            routing = route_query(
                message,
                history=history,
                prior_intent=prior_intent,
            )
        except Exception as e:
            print(f"[engine] router failed: {e}")
            from .classifier import classify_query
            routing = classify_query(message)
            routing["intent"] = "unknown"

        # Short-circuit: clarify needed, don't bother running the rest of
        # the orchestration phases. Returning early keeps the response fast.
        if routing.get("requires_clarification"):
            return {
                "requires_clarification": True,
                "intent": routing.get("intent", "general"),
                "candidates": routing.get("candidates", []),
                "confidence": routing.get("confidence", "low"),
                "tier": routing.get("tier", "simple"),
                "model_id": routing.get("model_id", FLASH_LITE_MODEL),
                "max_tokens": routing.get("max_tokens", 4096),
                "tools": [],
                "system": "",
                "plan": None,
            }

        tier = routing.get("tier", "simple")
        model_id = routing.get("model_id", FLASH_LITE_MODEL)
        intent = routing.get("intent", "unknown")

        # Phase 5: Specialist agent tools
        try:
            fallback_tools = get_all_tool_definitions() if routing.get("use_full_tools") else get_simple_tool_definitions()
            agent = get_agent_config(intent, fallback_tools)
            tools = agent.get("tools") or fallback_tools
            agent_prompt = agent.get("prompt", "")
        except Exception as e:
            print(f"[engine] agent config failed: {e}")
            tools = get_all_tool_definitions()
            agent_prompt = ""

        # Phase 6: Schema selection
        try:
            relevant_tables = select_tables(message, max_tables=8)
            focused_schema = build_focused_schema(relevant_tables)
        except Exception as e:
            print(f"[engine] schema selector failed: {e}")
            focused_schema = ""

        # Phase 7: Persistent memory
        try:
            user_facts = get_user_facts(limit=15)
            facts_block = format_facts_for_prompt(user_facts)
        except Exception as e:
            print(f"[engine] persistent memory failed: {e}")
            facts_block = ""

        # Phase 3: Reflections
        try:
            reflections = get_relevant_reflections(message, limit=3)
            reflections_block = format_reflections_for_prompt(reflections)
        except Exception as e:
            print(f"[engine] reflexion failed: {e}")
            reflections_block = ""

        # Memory summaries
        try:
            memory_ctx = load_memory_context(None)
        except Exception:
            memory_ctx = ""

        # Build system prompt
        system = build_system_prompt(
            user_id=_get_user_id(),
            memory_context=memory_ctx,
            voice=voice,
            focused_schema=focused_schema,
            agent_prompt=agent_prompt,
            user_facts=facts_block,
            reflections=reflections_block,
        )

        # Phase 1: Pre-Act planner (complex only, non-critical)
        plan = None
        if tier == "complex":
            try:
                plan = create_plan(message, tools, self.gateway)
                if plan:
                    plan_block = format_plan_for_prompt(plan)
                    system = f"{system}\n\n{plan_block}"
            except Exception as e:
                print(f"[engine] planner failed: {e}")

        return {
            "tier": tier,
            "model_id": model_id,
            "intent": intent,
            "tools": tools,
            "system": system,
            "max_tokens": routing.get("max_tokens", 4096),
            "plan": plan,
            "router": routing.get("router"),
            "confidence": routing.get("confidence"),
        }

    def _execute_tool_with_phases(
        self,
        tc,
        user_message: str,
    ) -> dict:
        """Execute a tool call with SQL validation and reflexion. Never crashes."""
        try:
            if tc.name == "sql_query":
                output = execute_validated_sql(tc.input, self.gateway)
            else:
                output = execute_tool(tc.name, tc.input)
        except Exception as e:
            output = {"error": f"Tool '{tc.name}' crashed: {e}"}

        # Ensure output is always a dict
        if not isinstance(output, dict):
            output = {"result": str(output) if output else "No result"}

        # Phase 3: Record failures for reflexion learning
        if output.get("error"):
            try:
                record_failure(
                    user_message,
                    tc.name,
                    tc.input,
                    output["error"],
                    self.gateway,
                )
            except Exception:
                pass

        return output

    def _dispatch_tool_calls(
        self,
        tool_calls: list,
        user_message: str,
        on_start=None,
        on_done=None,
    ) -> list[tuple]:
        """
        Execute a batch of tool calls with read tools in parallel and action
        tools serialized. Flask's request context is propagated into workers
        so tools relying on `g.user_id` keep working.

        Returns a list of (tool_call, output) tuples in ORIGINAL order so that
        tool_result blocks line up with tool_use IDs for the next model turn.

        Hooks:
          on_start(tc)          — called in the main thread before dispatch
          on_done(tc, output)   — called as each tool finishes (any thread)
        """
        # Build a request-context-preserving wrapper so threads can read g
        try:
            from flask import copy_current_request_context

            def _wrap(fn):
                return copy_current_request_context(fn)
        except RuntimeError:
            # Outside a request context (e.g. tests) — no wrapping needed
            def _wrap(fn):
                return fn

        def _run(tc):
            return tc, self._execute_tool_with_phases(tc, user_message)

        action_calls = [tc for tc in tool_calls if is_action_tool(tc.name)]
        read_calls = [tc for tc in tool_calls if not is_action_tool(tc.name)]

        if on_start:
            for tc in tool_calls:
                try:
                    on_start(tc)
                except Exception:
                    pass

        results: dict[str, dict] = {}

        # Parallel reads
        if read_calls:
            wrapped = _wrap(_run)
            futures = {_TOOL_POOL.submit(wrapped, tc): tc for tc in read_calls}
            for fut in as_completed(futures):
                tc = futures[fut]
                try:
                    _, out = fut.result()
                except Exception as exc:
                    out = {"error": f"Tool '{tc.name}' crashed: {exc}"}
                results[tc.id] = out
                if on_done:
                    try:
                        on_done(tc, out)
                    except Exception:
                        pass

        # Serialize writes (same thread, same request context)
        for tc in action_calls:
            try:
                _, out = _run(tc)
            except Exception as exc:
                out = {"error": f"Action '{tc.name}' crashed: {exc}"}
            results[tc.id] = out
            if on_done:
                try:
                    on_done(tc, out)
                except Exception:
                    pass

        return [(tc, results[tc.id]) for tc in tool_calls]

    # ── Non-streaming query ─────────────────────────────────────────────

    def query(
        self,
        message: str,
        page_context: str = "",
        stock_context: str = "",
        voice: bool = False,
        load_history_flag: bool = True,
        persist_history: bool = True,
        history_override: list[dict] | None = None,
    ) -> dict:
        if not message or not message.strip():
            return {"error": "message is required"}

        if not self.gateway.available():
            return {"error": "Gemini API key not configured"}

        # Load chat history ONCE — shared between router (for context)
        # and the tool loop below.
        if history_override is not None:
            history = normalize_history_override(history_override)
        else:
            history = load_history(page_context, limit=10) if load_history_flag else []

        prior_intent = _extract_prior_intent(history)

        # Run all pre-execution phases (router sees history + prior_intent)
        ctx = self._orchestrate(
            message, voice, history=history, prior_intent=prior_intent,
        )

        # Clarify branch — router is unsure. Return tap-button payload.
        if ctx.get("requires_clarification"):
            return {
                "requires_clarification": True,
                "candidates": ctx.get("candidates", []),
                "top_intent": ctx.get("intent"),
                "message": "I want to make sure I understood. Did you mean:",
                "response": "",
            }

        system = ctx["system"]
        tools = ctx["tools"]
        model_id = ctx["model_id"]
        tier = ctx["tier"]
        intent = ctx["intent"]
        max_tokens = 300 if voice else ctx["max_tokens"]

        messages: list = history + [{"role": "user", "content": message}]

        # Tool loop
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
                    model_id=model_id,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    tools=tools,
                )

                usage_input += result.input_tokens
                usage_output += result.output_tokens

                if result.stop_reason == "end_turn":
                    response_text = self._synthesize_final_text(
                        result, model_id=model_id, system=system, messages=messages
                    )
                    break

                if result.stop_reason == "tool_use":
                    tool_result_blocks = []
                    executed = self._dispatch_tool_calls(result.tool_calls, message)
                    for tc, tool_output in executed:
                        if isinstance(tool_output, dict) and tool_output.get("requires_confirmation"):
                            pending_action = tool_output.get("pending_action")

                        tool_results_log.append({
                            "tool": tc.name,
                            "input": tc.input,
                            "ok": not bool((tool_output or {}).get("error")),
                        })
                        if is_action_tool(tc.name):
                            skip_history_reuse = True

                        content_str = json.dumps(to_jsonable(tool_output), default=str, ensure_ascii=False)
                        if len(content_str) > 12000:
                            content_str = content_str[:12000] + '..."}'

                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": content_str,
                        })

                    messages.append({"role": "assistant", "content": result.to_message_content()})
                    messages.append({"role": "user", "content": tool_result_blocks})
                else:
                    response_text = self._synthesize_final_text(
                        result, model_id=model_id, system=system, messages=messages
                    )
                    break
            else:
                response_text = self._synthesize_final_text(
                    result, model_id=model_id, system=system, messages=messages
                )

        except Exception as exc:
            traceback.print_exc()
            return {"error": f"Engine error: {exc}"}

        # Save history + generate summary + extract facts (all async where possible)
        skip_history_reuse = skip_history_reuse or bool(pending_action)
        if response_text and persist_history and not skip_history_reuse:
            save_message("user", message, page_context=page_context, stock_context=stock_context)
            save_message("assistant", response_text, page_context=page_context, stock_context=stock_context)
            # Phase 7: Extract persistent facts (async)
            extract_and_save_facts_async(message, response_text, tool_results_log, self.gateway)
            # Memory summary (async)
            generate_and_save_summary(message, response_text, tool_results_log, page_context)

        return {
            "response": response_text,
            "model": model_id,
            "tier": tier,
            "intent": intent,
            "requires_confirmation": bool(pending_action),
            "pending_action": pending_action,
            "tool_calls": tool_results_log,
            "input_tokens": usage_input,
            "output_tokens": usage_output,
            "skip_history_reuse": skip_history_reuse,
        }

    # ── Streaming ───────────────────────────────────────────────────────

    def query_stream(
        self,
        message: str,
        page_context: str = "",
        stock_context: str = "",
        voice: bool = False,
        load_history_flag: bool = True,
        persist_history: bool = True,
        history_override: list[dict] | None = None,
    ):
        """Generator that yields SSE events for real-time tool-step display."""
        if not message or not message.strip():
            yield _sse({"type": "error", "message": "message is required"})
            return

        if not self.gateway.available():
            yield _sse({"type": "error", "message": "Gemini API key not configured"})
            return

        yield _sse({"type": "status", "step": "routing", "detail": "Analyzing query..."})

        # Load chat history ONCE — shared between router (for context)
        # and the tool loop below.
        if history_override is not None:
            history = normalize_history_override(history_override)
        else:
            history = load_history(page_context, limit=10) if load_history_flag else []

        prior_intent = _extract_prior_intent(history)

        # Run all pre-execution phases (router sees history + prior_intent)
        ctx = self._orchestrate(
            message, voice, history=history, prior_intent=prior_intent,
        )

        # Clarify branch — router is unsure, emit tap-button event and stop
        if ctx.get("requires_clarification"):
            yield _sse({
                "type": "clarify",
                "message": "I want to make sure I understood. Did you mean:",
                "candidates": ctx.get("candidates", []),
                "top_intent": ctx.get("intent"),
            })
            return

        system = ctx["system"]
        tools = ctx["tools"]
        model_id = ctx["model_id"]
        tier = ctx["tier"]
        intent = ctx["intent"]
        max_tokens = 300 if voice else ctx["max_tokens"]
        plan = ctx["plan"]

        yield _sse({
            "type": "status",
            "step": "thinking",
            "detail": f"Routed to {intent} specialist ({tier})",
            "tier": tier,
            "model": model_id,
            "intent": intent,
            "router": ctx.get("router"),
        })

        # Emit plan if we made one
        if plan and plan.get("plan"):
            yield _sse({
                "type": "plan",
                "steps": [{"step": s.get("step"), "action": s.get("action"), "description": s.get("description")} for s in plan["plan"]],
            })

        messages: list = history + [{"role": "user", "content": message}]
        pending_action = None
        result = None
        skip_history_reuse = False
        stream_tool_log: list[dict] = []

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                if _round > 0:
                    yield _sse({"type": "status", "step": "reasoning", "detail": "Analyzing results..."})

                # Stream tokens as Gemini produces them. Deltas forwarded to
                # the client for typewriter effect; final `complete` event
                # carries the full UnifiedResponse for the tool-loop decision.
                result = None
                emitted_deltas = False
                for ev in self.gateway.create_message_stream(
                    model_id=model_id,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    tools=tools,
                ):
                    if ev["type"] == "text_delta":
                        emitted_deltas = True
                        yield _sse({"type": "text_delta", "delta": ev["delta"]})
                    elif ev["type"] == "complete":
                        result = ev["response"]

                if result is None:
                    # Shouldn't happen — stream always yields complete — but
                    # guard anyway so we never loop forever on a bad response.
                    yield _sse({"type": "error", "message": "No response from model"})
                    return

                # If the turn is actually a tool call, discard any preamble
                # text we streamed — the final answer will come in a later round.
                if result.stop_reason == "tool_use" and emitted_deltas:
                    yield _sse({"type": "text_reset"})

                if result.stop_reason == "end_turn":
                    response_text = self._synthesize_final_text(
                        result, model_id=model_id, system=system, messages=messages
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
                        save_message("user", message, page_context=page_context, stock_context=stock_context)
                        save_message("assistant", response_text, page_context=page_context, stock_context=stock_context)
                        # Phase 7: Extract facts async
                        extract_and_save_facts_async(message, response_text, stream_tool_log, self.gateway)
                        generate_and_save_summary(message, response_text, stream_tool_log, page_context)
                    return

                if result.stop_reason == "tool_use":
                    tool_result_blocks = []

                    # Emit tool_start for everything up-front; completions
                    # will arrive out-of-order as parallel reads finish.
                    for tc in result.tool_calls:
                        step_detail = _describe_tool_step(tc.name, tc.input)
                        yield _sse({"type": "tool_start", "tool": tc.name, "detail": step_detail})

                    # done_events is a shared queue the workers push into;
                    # the generator drains it after dispatch completes.
                    done_events: list[dict] = []

                    def _on_done(tc, tool_output):
                        nonlocal pending_action, skip_history_reuse
                        if is_action_tool(tc.name):
                            skip_history_reuse = True
                        if isinstance(tool_output, dict) and tool_output.get("requires_confirmation"):
                            pending_action = tool_output.get("pending_action")
                        ok = not bool((tool_output or {}).get("error"))
                        rows = tool_output.get("count") if isinstance(tool_output, dict) else None
                        retried = bool(tool_output.get("_retried")) if isinstance(tool_output, dict) else False
                        stream_tool_log.append({"tool": tc.name, "ok": ok})
                        done_events.append({
                            "type": "tool_done",
                            "tool": tc.name,
                            "ok": ok,
                            "rows": rows,
                            "retried": retried,
                        })

                    executed = self._dispatch_tool_calls(
                        result.tool_calls, message, on_done=_on_done,
                    )

                    for ev in done_events:
                        yield _sse(ev)

                    for tc, tool_output in executed:
                        content_str = json.dumps(to_jsonable(tool_output), default=str, ensure_ascii=False)
                        if len(content_str) > 12000:
                            content_str = content_str[:12000] + '..."}'

                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": content_str,
                        })

                    messages.append({"role": "assistant", "content": result.to_message_content()})
                    messages.append({"role": "user", "content": tool_result_blocks})
                else:
                    response_text = self._synthesize_final_text(
                        result, model_id=model_id, system=system, messages=messages
                    )
                    yield _sse({"type": "answer", "response": response_text})
                    return

            yield _sse({
                "type": "answer",
                "response": self._synthesize_final_text(
                    result, model_id=model_id, system=system, messages=messages
                ),
            })

        except Exception as exc:
            traceback.print_exc()
            yield _sse({"type": "error", "message": f"Engine error: {exc}"})


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"


def _extract_prior_intent(history: list[dict]) -> str | None:
    """
    Pull the most recent assistant message's intent tag from history.

    Returns None unless an assistant message ends with an intent marker like
    `<!--intent:trades-->`. We don't write these markers yet — this is a hook
    point for later. Until then, the router runs without prior_intent context
    (it still gets the last 3 user messages).
    """
    if not history:
        return None
    import re as _re
    pattern = _re.compile(r"<!--intent:([a-z_]+)-->\s*$")
    for item in reversed(history):
        if item.get("role") != "assistant":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        m = pattern.search(content)
        if m:
            return m.group(1)
    return None


def _describe_tool_step(tool_name: str, tool_input: dict) -> str:
    """Clean, user-friendly step descriptions."""

    if tool_name == "query_trades":
        fys = tool_input.get("fys", [])
        agg = tool_input.get("aggregate", "none")
        symbol = tool_input.get("symbol", "")
        if symbol:
            return f"Looking up {symbol} trades..."
        if fys == ["all"]:
            period = "across all years"
        elif len(fys) == 1:
            period = f"for FY {fys[0]}"
        elif fys:
            period = f"across FY {fys[0]} to {fys[-1]}"
        else:
            period = "for current FY"
        if agg == "yearly":
            return f"Comparing yearly performance {period}..."
        if agg == "monthly":
            return f"Breaking down monthly results {period}..."
        if agg == "sector":
            return f"Analyzing by sector {period}..."
        if agg == "symbol":
            return f"Analyzing by stock {period}..."
        return f"Searching trades {period}..."
    elif tool_name == "query_monthly":
        return "Loading monthly summaries..."
    elif tool_name == "scan_stocks":
        preset = tool_input.get("preset", "near_52w_high")
        labels = {
            "near_52w_high": "stocks near 52-week highs",
            "breakout_candidates": "breakout candidates",
            "ma_crossover": "MA crossover signals",
            "beaten_down": "beaten-down stocks",
        }
        return f"Scanning for {labels.get(preset, preset)}..."
    elif tool_name == "get_watchlist":
        name = tool_input.get("name", "")
        return f"Loading watchlist{f' {name}' if name else 's'}..."
    elif tool_name == "get_journal_insights":
        group = tool_input.get("group_by", "entry_type")
        return f"Analyzing journal by {group.replace('_', ' ')}..."
    elif tool_name == "compare_to_index":
        idx = tool_input.get("index", "Nifty 50")
        return f"Comparing portfolio vs {idx}..."
    elif tool_name == "get_fundamentals":
        sym = tool_input.get("symbol", "")
        return f"Fetching fundamentals for {sym}..."
    elif tool_name == "get_analytics":
        ep = tool_input.get("endpoint", "full")
        return f"Loading {ep} analytics..."
    elif tool_name == "get_equity_curve":
        t = tool_input.get("type", "long-term")
        return "Building equity curve..." if t == "long-term" else "Analyzing drawdowns..."
    elif tool_name == "get_live_market":
        syms = tool_input.get("symbols", [])
        if syms:
            return f"Checking price for {', '.join(syms[:3])}{'...' if len(syms) > 3 else ''}"
        return "Fetching market data..."
    elif tool_name == "get_positions":
        return "Loading portfolio..."
    elif tool_name == "search_stock":
        return f"Looking up {tool_input.get('query', 'stock')}..."
    elif tool_name == "sql_query":
        return "Running custom query..."
    else:
        return f"{tool_name.replace('_', ' ').title()}..."
