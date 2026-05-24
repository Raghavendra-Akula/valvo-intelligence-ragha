"""
Valvo AI v5 -- Main query engine.

V5 is an exact clone of v3's architecture (single-engine, all-tools-available
agentic loop) but runs on Gemini only — no Anthropic provider. Created to
preserve v3's proven composability while isolating future improvements to a
new surface area. Chat history uses the 'valvo-ai-v5' scope so conversations
don't collide with v3 or v4.

Agentic tool-use loop: sends user message to Gemini, executes tool calls,
feeds results back, repeats for up to 6 rounds until a final text response.
"""
from __future__ import annotations

import json
import time
import traceback

from services.valvo_ai_v2.utils import to_jsonable

from .gateway import ModelGateway
from .portfolio_oracle import try_oracle
from .prompts import build_system_prompt
from .tools import execute_tool, get_all_tool_definitions


# ---------------------------------------------------------------------------
# Chat history (re-uses the same chat_messages table with a v5 scope)
# ---------------------------------------------------------------------------

_HISTORY_PREFIX = "valvo-ai-v5"


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
        print(f"[v5-engine] load_history failed: {e}")
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
        print(f"[v5-engine] save_message failed: {e}")
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
        print(f"[v5-engine] clear_history failed: {exc}")
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

# Healthy queries finish in 2-3 rounds. The old 12-round cap only ever
# kicked in when the model was stuck in a loop — which is the bug, not a
# success mode. Six is a safer ceiling: plenty of headroom for legitimate
# cross-ref chains (3 tools + 1 final) and still caps runaway cost.
MAX_TOOL_ROUNDS = 6


# Whole-word keywords that reliably flag a question needing multi-step
# reasoning or composition — Gemini 2.5 Flash's dynamic thinking budget
# helps most here. Simple lookups ("show positions", "current regime",
# "top 5 winners", "best month") skip thinking entirely so cost stays flat.
#
# Word-boundary match is important: "show" must NOT match "how" inside
# other words ('how' as a substring), "analysis" vs. "lysis".
#
# What's DELIBERATELY NOT here (they're lookups, not reasoning):
#   top / best / worst — "top 5 winners", "best month" = ORDER BY LIMIT
#   summary / summariz — "portfolio summary" = aggregate lookup
#   breakdown         — "FY breakdown" = GROUP BY aggregation
#   how               — too ambiguous; "how did I do" is a lookup
# Enabling thinking on these lookups was causing empty responses when the
# combined large prompt + tools + thinking budget pushed the model into
# a failure mode mid-generation (MAX_TOKENS on invisible thinking).
import re as _re

_COMPLEX_INTENT_REGEX = _re.compile(
    r"\b(compare|vs|versus|analyz\w*|explain|why|trend\w*|pattern\w*|"
    r"across|between|which|evaluate)\b",
    _re.IGNORECASE,
)


def _is_complex_intent(message: str) -> bool:
    """Heuristic: does this user turn likely need multi-step reasoning?

    Kept deliberately permissive — false positives just cost a few extra
    thinking tokens on Flash. False negatives (missing a complex question)
    give a worse answer, which is the expensive failure mode.
    """
    if not message:
        return False
    if _COMPLEX_INTENT_REGEX.search(message):
        return True
    # Long messages usually warrant thinking.
    if len(message) > 140:
        return True
    return False


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


def _friendly_engine_error(exc: Exception) -> str:
    """Translate raw provider exceptions into a user-facing one-liner.

    Raw 429 / 5xx blobs from Gemini look like
        429 RESOURCE_EXHAUSTED. {'error': {'code': 429, …, 'status': …}}
    which we used to surface verbatim. Users see all that JSON, can't
    tell whether it's a billing cap, a transient throttle, or a code
    bug, and the support load goes up. Pattern-match on the parts we
    can reliably detect and emit something the chat UI can display
    cleanly.
    """
    text = str(exc) or exc.__class__.__name__
    lower = text.lower()
    if "spending cap" in lower or "monthly spending" in lower:
        return ("AI usage hit your Google AI Studio spending cap for this month. "
                "Raise it at https://ai.studio/spend, or wait until the next "
                "billing cycle.")
    if "resource_exhausted" in lower or " 429" in text or text.startswith("429"):
        return ("AI is temporarily rate-limited. Try again in a few seconds — "
                "if it persists, your project quota may be exhausted.")
    if "permission_denied" in lower or "403" in text:
        return "AI provider rejected the request (permissions / API key issue)."
    if "deadline" in lower or "timeout" in lower or "timed out" in lower:
        return "AI request timed out. Try a more focused question or retry."
    if "unavailable" in lower or " 503" in text or "internal error" in lower or " 500" in text:
        return "AI service is temporarily unavailable. Please retry in a moment."
    # Last resort — strip the long JSON blob if Gemini's exception text
    # included one, keep just the human-readable prefix.
    short = text.split("{", 1)[0].strip().rstrip(".") or text
    return f"AI engine error: {short}"


def _is_action_tool(tool_name: str) -> bool:
    if tool_name in {"confirm_action", "cancel_action"}:
        return True
    from services.valvo_ai_v2.actions import ACTIONS as V2_ACTIONS

    return tool_name in V2_ACTIONS


class ValvoAIV5Engine:
    """Agentic Valvo AI engine — Gemini + multi-round tool loop (v5)."""

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

    # ------------------------------------------------------------------
    # Proactive session opener
    # ------------------------------------------------------------------
    def generate_opener(self, page_context: str = "") -> dict:
        """Generate 2-3 timely observations about the CURRENT user's book,
        market regime, and leading-sector alignment. Runs the normal agentic
        tool loop under the hood with a seed message instructing Gemini to
        be the opener. The tools the model reaches for (get_positions,
        get_market_regime, get_positions_sector_membership, price_alerts
        query, …) are all user-scoped via Flask g.user_id — so each user
        sees insights grounded in their own portfolio.

        The opener is NOT saved to chat history; it's a one-shot fresh
        every time the conversation starts. No follow-up trailer parsing
        mismatch either — the prompt still asks for FOLLOW_UPS, which the
        frontend renders as clickable chips (the whole point of the opener
        is to nudge the user into the next question).
        """
        if not self.gateway.available():
            return {"error": "No Valvo AI model provider is configured"}

        opener_instruction = (
            "OPENER MODE. You are greeting the user for a new session. DO NOT "
            "wait for a question — proactively surface 2-3 short, timely "
            "observations grounded in THEIR current data. Concretely:\n"
            "  1. Call get_positions(status='active') to see their book.\n"
            "  2. Call get_market_regime() for the current regime.\n"
            "  3. If they have active positions, call "
            "     get_positions_sector_membership() to see which sit in "
            "     leading sectors.\n"
            "  4. Optionally sql_query price_alerts for recent triggers "
            "     (triggered=true AND triggered_at >= NOW() - INTERVAL '48 hours').\n"
            "Then write 2-3 bullet insights the trader would genuinely want "
            "to see right now. Examples of the tone you're going for: "
            "'**BSE** is +16.8R above entry — consider trailing to 20MA.' · "
            "'**Metal** sector broke out this week (+14% 20d); your **NALCO** "
            "is in it.' · 'No active position touched SL in last 48h — book "
            "is stable.'\n"
            "Rules:\n"
            "  • Maximum 3 bullets. Short. No filler.\n"
            "  • Lead each bullet with a bold subject (**SYMBOL** / **SECTOR** / etc.).\n"
            "  • Still append the FOLLOW_UPS trailer at the very end (rule 13).\n"
            "  • Never invent a position, regime, or sector that the tools didn't return.\n"
            "  • If the user has NO active positions, say so in one line and suggest "
            "    they start by scoring or reviewing past trades."
        )

        # Run through the normal query() path but skip both history loading
        # (so no prior context taints the opener) and persistence (so the
        # opener doesn't pollute the history log on the next real turn).
        result = self.query(
            message=opener_instruction,
            page_context=page_context,
            stock_context="",
            voice=False,
            model=None,
            load_history=False,
            persist_history=False,
            history_override=None,
        )
        # Tag the response so the frontend can style it differently.
        if isinstance(result, dict) and not result.get("error"):
            result["is_opener"] = True
        return result

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

        turn_started_at = time.monotonic()

        # 0. Pre-LLM deterministic short-circuit for portfolio math
        # questions. The LLM was unreliable at arithmetic (one production
        # turn produced -Rs1,273.74 for a portfolio that totals
        # +Rs577.35; another fabricated a portfolio of RELIANCE/TCS/etc.
        # when LIVE STATE failed to inject). For any message that
        # pattern-matches a known portfolio intent, we compute the
        # answer in Python and skip the LLM round-trip entirely.
        # try_oracle returns None on a miss, so unrecognised questions
        # fall through to the normal flow below.
        oracle_uid = _get_user_id()
        oracle_answer = try_oracle(message, oracle_uid)
        if oracle_answer is not None:
            if persist_history:
                _save_message("user", message, page_context=page_context, stock_context=stock_context)
                _save_message("assistant", oracle_answer, page_context=page_context, stock_context=stock_context)
            # Episode log so oracle_hit_rate.py can measure how much of
            # real traffic the deterministic specialists are catching.
            try:
                from .episodes import log_turn
                if oracle_uid:
                    log_turn(
                        user_id=oracle_uid,
                        user_message=message,
                        final_answer=oracle_answer,
                        page_context=page_context,
                        rounds=0,
                        total_latency_ms=int((time.monotonic() - turn_started_at) * 1000),
                        model="oracle",
                        intent="oracle",
                        had_error=False,
                        path="oracle",
                    )
            except Exception as exc:
                print(f"[v5-engine] oracle episode log skipped: {exc}")
            return {
                "response": oracle_answer,
                "follow_ups": [],
                "model": "deterministic-oracle",
                "requires_confirmation": False,
                "pending_action": None,
                "tool_calls": [],
                "input_tokens": 0,
                "output_tokens": 0,
                "skip_history_reuse": False,
            }

        # 1. Build system prompt (passes the user message so the lessons
        #    retriever can lexically pre-filter; falls back to recency otherwise).
        system = build_system_prompt(user_id=_get_user_id(), query_text=message)

        # 2. Load chat history
        if history_override is not None:
            history = _normalize_history_override(history_override)
        else:
            history = _load_history(page_context, limit=10) if load_history else []

        # 3. Build messages
        messages: list = history + [{"role": "user", "content": message}]

        # 4. Tool definitions
        tools = get_all_tool_definitions()

        # 5. Token budget
        # 32k is the output cap for non-voice turns. Gemini 2.5 Flash supports
        # up to 65k; 32k is the safe middle. It's a LIMIT not a reservation —
        # billing is per actual token used, so short answers cost the same as
        # before. The extra headroom gives dynamic thinking + long narrative
        # responses room to breathe without hitting MAX_TOKENS and returning
        # empty parts (the root cause of the NoneType crashes we saw earlier).
        max_tokens = 300 if voice else 32768

        # 6. Tool loop
        resolved_model = self.gateway.resolve_model(model)
        response_text = ""
        pending_action = None
        tool_results_log: list[dict] = []
        usage_input = 0
        usage_output = 0
        result = None
        skip_history_reuse = False
        # `thinking` still flips Gemini's internal reasoning budget on
        # complex intents — that's a Flash-side feature, not a model
        # switch. Reliability beyond Flash now comes from the
        # deterministic portfolio_oracle short-circuit, not from
        # routing to Pro.
        thinking = _is_complex_intent(message)

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                result = self.gateway.create_message(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    tools=tools,
                    thinking=thinking,
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

        except Exception as exc:
            traceback.print_exc()
            return {"error": _friendly_engine_error(exc)}

        # 6.5. Empty-response fallback. If the model came back with the
        #      defensive "empty response" placeholder from _normalize (i.e.
        #      parts=None / MAX_TOKENS during thinking OR safety / quota /
        #      transient model issue), run ONE retry with thinking off,
        #      bare history, and a 4-round tool budget. Simpler request =
        #      strictly smaller token footprint = almost always succeeds.
        #      Only triggered on the fresh-fail path; if the retry itself
        #      comes back empty we give up gracefully (no infinite loop).
        #
        #      Note: this used to gate on `and thinking` — but we've seen
        #      empty responses on lookup queries too (e.g. "show my equity
        #      curve") where thinking was already off. The `thinking=False`
        #      retry still helps because it also drops history and shortens
        #      the tool loop.
        if response_text and "empty response" in response_text.lower():
            print("[v5-engine] empty response detected; retrying with simpler context")
            try:
                # Keep the LAST few turns of history. The previous version
                # dropped everything, which made follow-ups like
                # "Which of these are in profit?" fail reliably — the
                # retry had no antecedent for "these". Keep up to 4 prior
                # turns (≈2 user/assistant pairs) so pronouns still
                # resolve, but trim before that to keep the footprint small.
                trimmed_history = (history or [])[-4:] if history else []
                retry_messages = trimmed_history + [{"role": "user", "content": message}]
                retry_result = self.gateway.create_message(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=retry_messages,
                    tools=tools,
                    thinking=False,
                )
                # Walk a fresh (shortened) tool loop for the retry.
                retry_rounds = 0
                while retry_result.stop_reason == "tool_use" and retry_rounds < 4:
                    retry_rounds += 1
                    tool_result_blocks = []
                    for tc in retry_result.tool_calls:
                        tool_output = execute_tool(tc.name, tc.input)
                        tool_results_log.append({
                            "tool": tc.name,
                            "input": tc.input,
                            "ok": not bool((tool_output or {}).get("error")),
                        })
                        content_str = json.dumps(to_jsonable(tool_output), default=str, ensure_ascii=False)
                        if len(content_str) > 12000:
                            content_str = content_str[:12000] + '..."}'
                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": content_str,
                        })
                    retry_messages.append({"role": "assistant", "content": retry_result.to_message_content()})
                    retry_messages.append({"role": "user", "content": tool_result_blocks})
                    retry_result = self.gateway.create_message(
                        model=model, max_tokens=max_tokens,
                        system=system, messages=retry_messages,
                        tools=tools, thinking=False,
                    )
                if retry_result.text and "empty response" not in retry_result.text.lower():
                    response_text = retry_result.text
                    usage_input += retry_result.input_tokens
                    usage_output += retry_result.output_tokens
            except Exception as exc:
                print(f"[v5-engine] retry without thinking also failed: {exc}")

        # 6.6. Peel off the FOLLOW_UPS trailer before anything else sees it
        #      (history, frontend, TTS). Keeps the rendered answer clean and
        #      gives us a structured list for the chip UI.
        response_text, follow_ups = _split_follow_ups(response_text)

        # 7. Save to history (cleaned text, no trailer clutter)
        # skip_history_reuse signals the FRONTEND to drop this turn from the
        # client-side history replay (so a pending action isn't shown twice
        # after a reload). It does NOT mean "don't persist" — server-side
        # history is what gives follow-up turns context like "keep existing
        # sl" after a pyramid prompt. Previously we gated persistence on
        # skip_history_reuse too, which erased the only context a follow-up
        # had and made the AI say "which stock?" after a pending_action.
        skip_history_reuse = skip_history_reuse or bool(pending_action)
        if response_text and persist_history:
            _save_message("user", message, page_context=page_context, stock_context=stock_context)
            _save_message("assistant", response_text, page_context=page_context, stock_context=stock_context)

            # 7a. Persistent memory — bump the turn counter and (on every
            #     Nth turn, or if stale) re-extract durable facts about the
            #     user into user_ai_context. Gated inside the memory module
            #     so the LLM call only fires occasionally. Wrapped in a
            #     broad try so a memory hiccup never breaks a user turn.
            try:
                from .memory import increment_turn_and_maybe_extract
                uid = _get_user_id()
                if uid:
                    increment_turn_and_maybe_extract(
                        user_id=uid,
                        user_message=message,
                        assistant_message=response_text,
                        gateway=self.gateway,
                    )
            except Exception as exc:
                print(f"[v5-engine] memory update skipped: {exc}")

            # 7b. Episodic log — one row per turn for the dream cycle to
            #     cluster on. Cheap (single INSERT, no LLM call); broad-try
            #     so an episode-write failure never breaks the user turn.
            try:
                from .episodes import log_turn
                uid = _get_user_id()
                if uid:
                    # `_round` is the loop variable from the tool loop above —
                    # always defined here because MAX_TOOL_ROUNDS >= 1.
                    log_turn(
                        user_id=uid,
                        user_message=message,
                        final_answer=response_text,
                        tool_calls=tool_results_log,
                        page_context=page_context,
                        rounds=_round + 1,
                        total_latency_ms=int((time.monotonic() - turn_started_at) * 1000),
                        model=resolved_model,
                        input_tokens=usage_input,
                        output_tokens=usage_output,
                        intent="complex" if thinking else "simple",
                        had_error=False,
                        follow_ups=follow_ups,
                    )
            except Exception as exc:
                print(f"[v5-engine] episode log skipped: {exc}")

        # 8. Return
        return {
            "response": response_text,
            "follow_ups": follow_ups,
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
        if not message or not message.strip():
            yield _sse({"type": "error", "message": "message is required"})
            return

        if not self.gateway.available():
            yield _sse({"type": "error", "message": "Anthropic API key not configured"})
            return

        turn_started_at = time.monotonic()

        # Pre-LLM deterministic short-circuit (see engine.query for
        # rationale). On match, emit a single answer event and exit;
        # the frontend renders it identically to a normal LLM answer.
        oracle_uid = _get_user_id()
        oracle_answer = try_oracle(message, oracle_uid)
        if oracle_answer is not None:
            if persist_history:
                _save_message("user", message, page_context=page_context, stock_context=stock_context)
                _save_message("assistant", oracle_answer, page_context=page_context, stock_context=stock_context)
            yield _sse({
                "type": "answer",
                "response": oracle_answer,
                "follow_ups": [],
                "requires_confirmation": False,
                "pending_action": None,
                "skip_history_reuse": False,
            })
            try:
                from .episodes import log_turn
                if oracle_uid:
                    log_turn(
                        user_id=oracle_uid,
                        user_message=message,
                        final_answer=oracle_answer,
                        page_context=page_context,
                        rounds=0,
                        total_latency_ms=int((time.monotonic() - turn_started_at) * 1000),
                        model="oracle",
                        intent="oracle",
                        had_error=False,
                        path="oracle",
                    )
            except Exception as exc:
                print(f"[v5-engine] oracle episode log (stream) skipped: {exc}")
            return

        system = build_system_prompt(user_id=_get_user_id(), query_text=message)
        if history_override is not None:
            history = _normalize_history_override(history_override)
        else:
            history = _load_history(page_context, limit=10) if load_history else []
        messages: list = history + [{"role": "user", "content": message}]
        tools = get_all_tool_definitions()
        max_tokens = 300 if voice else 32768  # see comment in query() for why 32k

        resolved_model = self.gateway.resolve_model(model)
        pending_action = None
        result = None
        skip_history_reuse = False
        thinking = _is_complex_intent(message)

        # Episode-log accumulators (mirror the non-streaming path so the same
        # log_turn call shape works for both surfaces).
        tool_results_log: list[dict] = []
        usage_input = 0
        usage_output = 0

        yield _sse({"type": "status", "step": "thinking", "detail": "Understanding your question..."})

        try:
            for _round in range(MAX_TOOL_ROUNDS):
                if _round == 0:
                    yield _sse({"type": "status", "step": "reasoning", "detail": "Thinking..."})
                else:
                    yield _sse({"type": "status", "step": "reasoning", "detail": "Analyzing results..."})

                # Non-streaming turn. We tried paced streaming (commit
                # 02d5666) but the combination of Gemini's burst-heavy
                # chunking + React re-renders felt flickery even after
                # client-side pacing. The final answer appearing fully-
                # formed (after the tool ticks) felt more polished.
                # The gateway's create_message_stream is kept for a
                # possible future revisit, but the engine no longer uses it.
                result = self.gateway.create_message(
                    model=model, max_tokens=max_tokens,
                    system=system, messages=messages, tools=tools,
                    thinking=thinking,
                )
                usage_input += result.input_tokens
                usage_output += result.output_tokens

                if result.stop_reason == "end_turn":
                    response_text = result.text
                    # Empty-response retry — if the gateway surfaced its
                    # "empty response" placeholder (Gemini sometimes
                    # returns parts=None when thinking chews the budget,
                    # or on a transient hiccup), do ONE retry with
                    # thinking off and a trimmed history. The trimmed
                    # history is critical: a bare retry kills follow-up
                    # questions like "Which of these are in profit?"
                    # because "these" loses its antecedent.
                    if response_text and "empty response" in response_text.lower():
                        print("[v5-engine] (stream) empty response; retrying with thinking=False")
                        try:
                            trimmed_history = (history or [])[-4:] if history else []
                            retry_msgs = trimmed_history + [{"role": "user", "content": message}]
                            retry_result = self.gateway.create_message(
                                model=resolved_model, max_tokens=max_tokens,
                                system=system, messages=retry_msgs, tools=tools,
                                thinking=False,
                            )
                            if (retry_result.stop_reason == "end_turn"
                                    and retry_result.text
                                    and "empty response" not in retry_result.text.lower()):
                                response_text = retry_result.text
                                usage_input += retry_result.input_tokens
                                usage_output += retry_result.output_tokens
                        except Exception as exc:
                            print(f"[v5-engine] stream retry also failed: {exc}")

                    # Strip FOLLOW_UPS trailer before sending to UI / TTS / history
                    response_text, follow_ups = _split_follow_ups(response_text)
                    skip_history_reuse = skip_history_reuse or bool(pending_action)
                    yield _sse({
                        "type": "answer",
                        "response": response_text,
                        "follow_ups": follow_ups,
                        "requires_confirmation": bool(pending_action),
                        "pending_action": pending_action,
                        "skip_history_reuse": skip_history_reuse,
                    })
                    # Persist regardless of skip_history_reuse — see the
                    # matching comment in query(). Server-side history is
                    # what lets follow-ups like "keep existing sl" land in
                    # the right context after a pending-action turn.
                    if persist_history:
                        _save_message("user", message, page_context=page_context, stock_context=stock_context)
                        _save_message("assistant", response_text, page_context=page_context, stock_context=stock_context)
                        # Episode log — same broad-try contract as in query().
                        try:
                            from .episodes import log_turn
                            uid = _get_user_id()
                            if uid:
                                log_turn(
                                    user_id=uid,
                                    user_message=message,
                                    final_answer=response_text,
                                    tool_calls=tool_results_log,
                                    page_context=page_context,
                                    rounds=_round + 1,
                                    total_latency_ms=int((time.monotonic() - turn_started_at) * 1000),
                                    model=resolved_model,
                                    input_tokens=usage_input,
                                    output_tokens=usage_output,
                                    intent="complex" if thinking else "simple",
                                    had_error=False,
                                    follow_ups=follow_ups,
                                )
                        except Exception as exc:
                            print(f"[v5-engine] stream episode log skipped: {exc}")
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

                        ok = not bool((tool_output or {}).get("error"))
                        tool_results_log.append({"tool": tool_name, "input": tool_input, "ok": ok})

                        # Emit completion
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
                    response_text, follow_ups = _split_follow_ups(response_text)
                    yield _sse({"type": "answer", "response": response_text, "follow_ups": follow_ups})
                    return

            _exhaust_text, _exhaust_fu = _split_follow_ups(result.text if result else "Tool loop exhausted.")
            yield _sse({"type": "answer", "response": _exhaust_text, "follow_ups": _exhaust_fu})

        except Exception as exc:
            traceback.print_exc()
            yield _sse({"type": "error", "message": _friendly_engine_error(exc)})


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


# Matches the FOLLOW_UPS block the system prompt instructs the model to append:
#   FOLLOW_UPS: [q1 | q2 | q3]
# Tolerant of surrounding whitespace + newlines; case-insensitive; greedy up to
# the closing ']' on the same "line" (we accept newlines inside via DOTALL so
# long questions won't accidentally truncate).
import re as _re
_FOLLOW_UPS_PATTERN = _re.compile(
    r"\n*\s*FOLLOW_UPS\s*:\s*\[(?P<body>.*?)\]\s*$",
    _re.IGNORECASE | _re.DOTALL,
)

# Same marker, but un-anchored — used in the streaming path to detect the
# start of the trailer in mid-generation so we stop forwarding deltas before
# "FOLLOW_UPS: [...]" text leaks into the user-visible bubble.
_FOLLOW_UPS_MARKER_RE = _re.compile(r"\n*\s*FOLLOW_UPS\s*:\s*", _re.IGNORECASE)


def _split_follow_ups(text: str) -> tuple[str, list[str]]:
    """Strip the FOLLOW_UPS trailer from `text` and return (clean_text, list).

    The prompt asks the model to always end its reply with one of:
        FOLLOW_UPS: [q1 | q2 | q3]
        FOLLOW_UPS: []
    We parse that off so it never shows up in the rendered answer, and hand
    the list back so the frontend can render each item as a clickable chip.
    A missing / malformed block is non-fatal — we just return no follow-ups.
    """
    if not text:
        return text or "", []
    match = _FOLLOW_UPS_PATTERN.search(text)
    if not match:
        return text.rstrip(), []
    body = (match.group("body") or "").strip()
    clean = text[: match.start()].rstrip()
    if not body:
        return clean, []
    parts = [p.strip().strip('"').strip("'") for p in body.split("|")]
    # Drop empties and de-duplicate while preserving order.
    seen = set()
    out: list[str] = []
    for p in parts:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return clean, out[:4]   # cap at 4 just in case the model gets exuberant


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
    elif tool_name == "get_positions_sector_membership":
        return "Cross-referencing positions with leading sectors..."
    elif tool_name == "get_stock_snapshot":
        sym = tool_input.get("symbol", "stock")
        return f"Pulling full snapshot for {sym}..."
    elif tool_name == "get_compare_stocks":
        syms = tool_input.get("symbols") or []
        if isinstance(syms, list) and syms:
            joined = ", ".join(str(s) for s in syms[:4])
            return f"Comparing {joined}..."
        return "Comparing stocks..."
    elif tool_name == "get_index_snapshot":
        sym = tool_input.get("symbol", "index")
        return f"Looking up {sym} index..."
    elif tool_name == "get_market_regime":
        return "Checking current market regime..."
    elif tool_name == "get_leading_sectors":
        return "Checking leading sectors..."
    elif tool_name == "get_fundamentals":
        sym = tool_input.get("symbol", "stock")
        return f"Fetching fundamentals for {sym}..."
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
