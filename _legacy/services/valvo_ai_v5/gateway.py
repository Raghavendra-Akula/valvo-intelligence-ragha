"""
Valvo AI v5 -- Gemini-only gateway (cloned from v3, Anthropic stripped).

Single provider: Gemini (flash default; flash-lite kept in registry for
explicit opt-in and as the in-family fallback target).
Returns a UnifiedResponse so the engine loop remains provider-agnostic in
case a second provider is added back in future.
"""
from __future__ import annotations

import os
import uuid
import json
import traceback
from dataclasses import dataclass, field
from typing import Any


# ═══════════════════════════════════════════════════════════════════════════
#  Unified response types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class UnifiedResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"          # "end_turn" or "tool_use"
    input_tokens: int = 0
    output_tokens: int = 0

    def to_message_content(self) -> list[dict]:
        """Provider-neutral representation of the assistant turn."""
        parts = []
        if self.text:
            parts.append({"type": "text", "text": self.text})
        for tc in self.tool_calls:
            parts.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.input,
            })
        return parts


# ═══════════════════════════════════════════════════════════════════════════
#  Model registry
# ═══════════════════════════════════════════════════════════════════════════

MODEL_REGISTRY = {
    # Gemini only — v5 is Gemini-only by design.
    # flash-lite: cheap fallback. flash: default workhorse for every
    # surface. pro: registered but not currently routed to — reliability
    # comes from the deterministic portfolio_oracle and Flash for the
    # remainder.
    "gemini-flash-lite": {"provider": "gemini", "model_id": "gemini-2.5-flash-lite"},
    "gemini-flash":      {"provider": "gemini", "model_id": "gemini-2.5-flash"},
    "gemini-pro":        {"provider": "gemini", "model_id": "gemini-2.5-pro"},
}

DEFAULT_MODEL = "gemini-flash"


# ═══════════════════════════════════════════════════════════════════════════
#  Gemini Provider
# ═══════════════════════════════════════════════════════════════════════════

class GeminiProvider:
    def __init__(self):
        self._api_key = os.getenv("api_key", "").strip()
        self._available = bool(self._api_key)
        self._tool_id_map: dict[str, str] = {}   # uuid → tool_name (for result mapping)

    def _make_client(self):
        """Create a fresh Gemini client per request.

        IMPORTANT — timeout unit:
        google-genai's HttpOptions.timeout is in MILLISECONDS, not seconds.
        Passing 120 here means 0.12 seconds and every request fails with
        ReadTimeout before the TCP handshake finishes. v4's gateway uses
        180 * 1000 = 180s; we match that.

        IMPORTANT — fork-safety:
        Gunicorn runs with --preload (see Dockerfile), which loads this module
        in the parent process then forks worker processes. A genai.Client
        instantiated at __init__ time would pre-create an httpx connection
        pool that gets duplicated into each forked worker; those child copies
        inherit file descriptors for sockets that become invalid after fork,
        causing httpx to hang forever on reads. Per-request construction
        avoids that — cost is negligible (milliseconds).
        """
        from google import genai
        return genai.Client(
            api_key=self._api_key,
            http_options={"timeout": 180 * 1000},  # 180s (field is milliseconds)
        )

    def available(self) -> bool:
        return self._available

    def create_message(
        self,
        model_id: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict],
        thinking: bool = False,
    ) -> UnifiedResponse:
        from google import genai
        from google.genai import types

        # Build tool declarations
        gemini_tools = self._translate_tools(tools) if tools else None

        # Build message contents
        contents = self._translate_messages(messages)

        # Config
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=0.3,
        )
        if gemini_tools:
            config.tools = gemini_tools

        # Thinking budget — CRITICAL to set EXPLICITLY:
        #   • Gemini 2.5 Flash defaults to dynamic thinking when no config is
        #     given. With 9+ tools and an ~8k-token system prompt, that invisible
        #     thinking can eat the entire max_output_tokens budget BEFORE any
        #     function_call or text is emitted, leaving parts=None and
        #     finish_reason=MAX_TOKENS. That's the actual root cause of
        #     the "Engine error: 'NoneType' object is not iterable" crashes.
        #   • `thinking_budget=0`  → thinking fully OFF (fast, deterministic,
        #     all 8192 tokens available for the actual response). Used for
        #     simple lookups that don't need reasoning.
        #   • `thinking_budget=-1` → dynamic (model decides 0…cap). Used only
        #     for genuinely complex intents (compare / analyze / why / how).
        try:
            config.thinking_config = types.ThinkingConfig(
                thinking_budget=-1 if thinking else 0,
            )
        except Exception as exc:
            # SDK may not expose ThinkingConfig on older versions. The fallback
            # (letting Gemini use its default) re-introduces the MAX_TOKENS
            # risk, but it's better than hard-failing the request.
            print(f"[gateway] thinking_config unavailable: {exc}")

        # Call API with a fresh client (see _make_client docstring for why)
        client = self._make_client()
        response = client.models.generate_content(
            model=model_id,
            contents=contents,
            config=config,
        )

        return self._normalize(response)

    def create_message_stream(
        self,
        model_id: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict],
        thinking: bool = False,
    ):
        """Streaming variant. Yields:
            {"type": "text_delta", "delta": "..."}   — partial tokens
            {"type": "complete",  "response": UnifiedResponse}  — always last

        Tool-call turns don't stream partial function arguments (Gemini
        atomic-emits them), so tool rounds produce zero text_deltas —
        the engine sees a single `complete` event with stop_reason=tool_use
        and proceeds to execute tools. Only end_turn text turns produce
        live streaming content, which is exactly what the chat UI wants.
        """
        from google import genai
        from google.genai import types

        gemini_tools = self._translate_tools(tools) if tools else None
        contents = self._translate_messages(messages)

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=0.3,
        )
        if gemini_tools:
            config.tools = gemini_tools
        try:
            config.thinking_config = types.ThinkingConfig(
                thinking_budget=-1 if thinking else 0,
            )
        except Exception:
            pass

        text_acc: list[str] = []
        tool_calls: list[ToolCall] = []
        input_tokens = 0
        output_tokens = 0

        client = self._make_client()
        try:
            stream = client.models.generate_content_stream(
                model=model_id,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            # Pre-stream failure → fall back to non-streaming once so the
            # engine still has something to feed the tool loop.
            print(f"[gateway] stream init failed ({exc}); falling back to non-streaming")
            resp = self.create_message(
                model_id=model_id, max_tokens=max_tokens,
                system=system, messages=messages, tools=tools, thinking=thinking,
            )
            yield {"type": "complete", "response": resp}
            return

        try:
            for chunk in stream:
                try:
                    candidates = getattr(chunk, "candidates", None) or []
                    if candidates and candidates[0].content and candidates[0].content.parts:
                        for part in candidates[0].content.parts:
                            if hasattr(part, "function_call") and part.function_call:
                                fc = part.function_call
                                tc_id = f"gemini_{uuid.uuid4().hex[:12]}"
                                name = fc.name
                                args = dict(fc.args) if fc.args else {}
                                self._tool_id_map[tc_id] = name
                                tool_calls.append(ToolCall(id=tc_id, name=name, input=args))
                            elif hasattr(part, "text") and part.text:
                                text_acc.append(part.text)
                                yield {"type": "text_delta", "delta": part.text}
                except (IndexError, AttributeError, TypeError) as e:
                    print(f"[gateway] stream parse error on chunk: {e}")

                meta = getattr(chunk, "usage_metadata", None)
                if meta:
                    input_tokens = getattr(meta, "prompt_token_count", input_tokens) or input_tokens
                    output_tokens = getattr(meta, "candidates_token_count", output_tokens) or output_tokens
                    cached = getattr(meta, "cached_content_token_count", 0) or 0
                    if cached:
                        pct = (cached / input_tokens * 100) if input_tokens else 0
                        print(f"[gateway] cache hit: {cached}/{input_tokens} input tokens ({pct:.0f}%)")
        except Exception as e:
            print(f"[gateway] stream iteration failed: {e}")

        yield {
            "type": "complete",
            "response": UnifiedResponse(
                text="".join(text_acc),
                tool_calls=tool_calls,
                stop_reason="tool_use" if tool_calls else "end_turn",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        }

    # -- Translators --

    def _translate_tools(self, tools: list[dict]) -> list:
        """Convert Anthropic tool format → Gemini function declarations."""
        from google.genai import types

        declarations = []
        for tool in tools:
            schema = tool.get("input_schema", {})
            params = self._translate_schema(schema) if schema.get("properties") else None
            declarations.append(types.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters=params,
            ))
        return [types.Tool(function_declarations=declarations)]

    def _translate_schema(self, schema: dict):
        """Recursively convert JSON Schema → Gemini types.Schema."""
        from google.genai import types

        type_map = {
            "string": "STRING",
            "integer": "INTEGER",
            "number": "NUMBER",
            "boolean": "BOOLEAN",
            "array": "ARRAY",
            "object": "OBJECT",
        }

        schema_type = type_map.get(schema.get("type", "string"), "STRING")

        kwargs: dict[str, Any] = {"type": schema_type}

        if "description" in schema:
            kwargs["description"] = schema["description"]
        if "enum" in schema:
            kwargs["enum"] = schema["enum"]

        # Object properties
        if schema.get("properties"):
            props = {}
            for name, prop_schema in schema["properties"].items():
                props[name] = self._translate_schema(prop_schema)
            kwargs["properties"] = props

        if schema.get("required"):
            kwargs["required"] = schema["required"]

        # Array items
        if schema.get("items"):
            kwargs["items"] = self._translate_schema(schema["items"])

        return types.Schema(**kwargs)

    def _translate_messages(self, messages: list[dict]) -> list:
        """Convert neutral message format → Gemini contents."""
        from google.genai import types

        contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            content = msg.get("content")

            if isinstance(content, str):
                contents.append(types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=content)],
                ))
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, str):
                        parts.append(types.Part.from_text(text=block))
                    elif isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "text":
                            txt = block.get("text", "")
                            if txt:
                                parts.append(types.Part.from_text(text=txt))
                        elif btype == "tool_use":
                            parts.append(types.Part.from_function_call(
                                name=block["name"],
                                args=block.get("input", {}),
                            ))
                        elif btype == "tool_result":
                            # Map tool_use_id back to tool name
                            tool_name = self._tool_id_map.get(block.get("tool_use_id", ""), "unknown")
                            result_content = block.get("content", "")
                            try:
                                result_data = json.loads(result_content) if isinstance(result_content, str) else result_content
                            except (json.JSONDecodeError, TypeError):
                                result_data = {"result": result_content}
                            parts.append(types.Part.from_function_response(
                                name=tool_name,
                                response=result_data if isinstance(result_data, dict) else {"result": result_data},
                            ))
                    # Skip any Anthropic SDK objects that might have leaked through
                if parts:
                    contents.append(types.Content(role=role, parts=parts))
            # Skip None or empty content
        return contents

    def _normalize(self, response) -> UnifiedResponse:
        """Convert Gemini response → UnifiedResponse."""
        text_parts = []
        tool_calls = []

        # Gemini can return a candidate with content=None or parts=None when the
        # generation was safety-blocked, hit MAX_TOKENS before producing content,
        # or was a thinking-only turn (thinking tokens live on the candidate but
        # parts is empty). Historically we did:
        #     for part in response.candidates[0].content.parts:
        # which crashed the whole engine with "'NoneType' object is not iterable"
        # on any of those edge cases. Now we defensively probe each layer and
        # log the finish_reason so we can tell WHY a turn came back empty.
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            cand = candidates[0]
            content = getattr(cand, "content", None)
            parts = getattr(content, "parts", None) if content else None
            finish_reason = getattr(cand, "finish_reason", None)

            if parts:
                for part in parts:
                    if hasattr(part, "function_call") and part.function_call:
                        tc_id = f"gemini_{uuid.uuid4().hex[:12]}"
                        name = part.function_call.name
                        args = dict(part.function_call.args) if part.function_call.args else {}
                        self._tool_id_map[tc_id] = name
                        tool_calls.append(ToolCall(id=tc_id, name=name, input=args))
                    elif hasattr(part, "text") and part.text:
                        text_parts.append(part.text)
            else:
                # No usable content. Keep the engine alive, surface a hint in the
                # text so the user isn't staring at "Engine error: NoneType..."
                # and log diagnostics so we can investigate.
                print(f"[gateway] empty response: finish_reason={finish_reason} has_content={content is not None} parts={parts}")
                # Best-effort text from prompt_feedback (safety-block messages)
                pf = getattr(response, "prompt_feedback", None)
                pf_reason = getattr(pf, "block_reason", None) if pf else None
                if pf_reason:
                    text_parts.append(f"The model didn't return a response (blocked: {pf_reason}). Please rephrase your question.")
                elif str(finish_reason) in {"MAX_TOKENS", "FinishReason.MAX_TOKENS"}:
                    text_parts.append("The model ran out of output tokens before producing an answer. Please ask a more focused question.")
                else:
                    text_parts.append("The model returned an empty response. Please try again or rephrase.")

        # Usage
        input_tokens = 0
        output_tokens = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            meta = response.usage_metadata
            input_tokens = getattr(meta, "prompt_token_count", 0) or 0
            output_tokens = getattr(meta, "candidates_token_count", 0) or 0
            cached = getattr(meta, "cached_content_token_count", 0) or 0
            if cached:
                pct = (cached / input_tokens * 100) if input_tokens else 0
                print(f"[gateway] cache hit: {cached}/{input_tokens} input tokens ({pct:.0f}%)")

        return UnifiedResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  ModelGateway — main entry point (Gemini-only in v5)
# ═══════════════════════════════════════════════════════════════════════════

class ModelGateway:
    """Gemini-only gateway. Flash-Lite is default; Flash is the in-family fallback."""

    def __init__(self):
        self._gemini = GeminiProvider()

    def available(self) -> bool:
        return self._gemini.available()

    def resolve_model(self, model_name: str | None) -> str:
        """Return the actual model ID string."""
        alias = model_name or DEFAULT_MODEL
        entry = MODEL_REGISTRY.get(alias)
        if entry:
            return entry["model_id"]
        return alias  # raw model ID passthrough

    def create_message(
        self,
        *,
        model: str | None,
        max_tokens: int,
        system: str,
        messages: list,
        tools: list,
        thinking: bool = False,
    ) -> UnifiedResponse:
        alias = model or DEFAULT_MODEL
        entry = MODEL_REGISTRY.get(alias, {"provider": "gemini", "model_id": alias})
        model_id = entry["model_id"]

        if not self._gemini.available():
            raise RuntimeError("Gemini API key not configured (env var 'api_key')")

        try:
            return self._gemini.create_message(
                model_id=model_id, max_tokens=max_tokens,
                system=system, messages=messages, tools=tools,
                thinking=thinking,
            )
        except Exception as exc:
            print(f"[gateway] Gemini {model_id} failed: {type(exc).__name__}: {exc}")

            # In-family fallback: flash-lite → flash. No cross-provider fallback.
            if alias == "gemini-flash-lite":
                try:
                    print("[gateway] Falling back to Gemini Flash")
                    return self._gemini.create_message(
                        model_id=MODEL_REGISTRY["gemini-flash"]["model_id"],
                        max_tokens=max_tokens, system=system,
                        messages=messages, tools=tools,
                        thinking=thinking,
                    )
                except Exception as exc2:
                    print(f"[gateway] Gemini Flash also failed: {type(exc2).__name__}: {exc2}")

            raise

    def create_message_stream(
        self,
        *,
        model: str | None,
        max_tokens: int,
        system: str,
        messages: list,
        tools: list,
        thinking: bool = False,
    ):
        """Streaming variant of create_message. Yields text_delta / complete
        events so the engine can forward tokens to the UI as they arrive."""
        alias = model or DEFAULT_MODEL
        entry = MODEL_REGISTRY.get(alias, {"provider": "gemini", "model_id": alias})
        model_id = entry["model_id"]

        if not self._gemini.available():
            raise RuntimeError("Gemini API key not configured (env var 'api_key')")

        yield from self._gemini.create_message_stream(
            model_id=model_id, max_tokens=max_tokens,
            system=system, messages=messages, tools=tools,
            thinking=thinking,
        )

    def health(self) -> dict:
        return {
            "gemini": "configured" if self._gemini.available() else "missing_api_key",
            "default_model": DEFAULT_MODEL,
        }
