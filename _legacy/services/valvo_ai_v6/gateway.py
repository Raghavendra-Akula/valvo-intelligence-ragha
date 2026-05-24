"""
Valvo AI v6 -- Multi-provider gateway.

Cloned from v3 (so the engine logic stays familiar) and adds Moonshot Kimi
(OpenAI-compatible) as a third provider. Default routes to kimi-k2.6.
Returns a UnifiedResponse so the engine loop is provider-agnostic.
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
    # Kimi K2.6 (thinking mode) returns reasoning_content alongside tool_calls.
    # If we don't echo it back on the next round, Moonshot 400s with
    # "thinking is enabled but reasoning_content is missing in assistant tool
    # call message". Stored here in neutral form; KimiProvider re-attaches it.
    reasoning_content: str = ""

    def to_message_content(self) -> list[dict]:
        """Provider-neutral representation of the assistant turn."""
        parts = []
        if self.reasoning_content:
            # Anthropic-style thinking block. Other providers ignore it; Kimi
            # picks it up in _translate_messages and re-emits as
            # message.reasoning_content.
            parts.append({"type": "thinking", "text": self.reasoning_content})
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
    # Moonshot Kimi (default for v6)
    "kimi-k2.6":         {"provider": "kimi", "model_id": "kimi-k2.6"},
    "kimi-k2.5":         {"provider": "kimi", "model_id": "kimi-k2.5"},
    "kimi-k2-thinking":  {"provider": "kimi", "model_id": "kimi-k2-thinking"},
    # Gemini
    "gemini-flash-lite": {"provider": "gemini", "model_id": "gemini-2.5-flash-lite"},
    "gemini-flash":      {"provider": "gemini", "model_id": "gemini-2.5-flash"},
    # Anthropic
    "haiku":  {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001"},
    "sonnet": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
    "opus":   {"provider": "anthropic", "model_id": "claude-opus-4-6"},
}

DEFAULT_MODEL = "kimi-k2.6"


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

        # Call API with a fresh client (see _make_client docstring for why)
        client = self._make_client()
        response = client.models.generate_content(
            model=model_id,
            contents=contents,
            config=config,
        )

        return self._normalize(response)

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

        if response.candidates:
            candidate = response.candidates[0]
            content = getattr(candidate, "content", None)
            # Gemini returns content=None or parts=None for some function-call
            # responses, blocked safety responses, and empty stops. Guard both
            # to avoid "NoneType is not iterable" crashes in the agent loop.
            parts = getattr(content, "parts", None) if content is not None else None
            for part in (parts or []):
                if hasattr(part, "function_call") and part.function_call:
                    tc_id = f"gemini_{uuid.uuid4().hex[:12]}"
                    name = part.function_call.name
                    args = dict(part.function_call.args) if part.function_call.args else {}
                    self._tool_id_map[tc_id] = name
                    tool_calls.append(ToolCall(id=tc_id, name=name, input=args))
                elif hasattr(part, "text") and part.text:
                    text_parts.append(part.text)

        # Usage
        input_tokens = 0
        output_tokens = 0
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
            output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

        return UnifiedResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Anthropic Provider
# ═══════════════════════════════════════════════════════════════════════════

class AnthropicProvider:
    def __init__(self):
        self._api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    def available(self) -> bool:
        return bool(self._api_key)

    def create_message(
        self,
        model_id: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> UnifiedResponse:
        import anthropic

        client = anthropic.Anthropic(api_key=self._api_key, timeout=120.0)

        # Prompt caching — wrap system as cacheable block
        system_with_cache = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]

        # Anthropic accepts our neutral message format directly
        # (dicts with "type": "text"/"tool_use"/"tool_result" in content lists)
        result = client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=system_with_cache,
            messages=messages,
            tools=tools,
        )

        return self._normalize(result)

    def _normalize(self, result) -> UnifiedResponse:
        """Convert Anthropic response → UnifiedResponse."""
        text_parts = []
        tool_calls = []

        for block in (result.content or []):
            if hasattr(block, "text") and block.text:
                text_parts.append(block.text)
            if block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    input=block.input or {},
                ))

        input_tokens = getattr(result.usage, "input_tokens", 0) if hasattr(result, "usage") else 0
        output_tokens = getattr(result.usage, "output_tokens", 0) if hasattr(result, "usage") else 0

        stop_reason = "end_turn"
        if result.stop_reason == "tool_use":
            stop_reason = "tool_use"

        return UnifiedResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Kimi Provider (Moonshot, OpenAI-compatible REST)
# ═══════════════════════════════════════════════════════════════════════════

class KimiProvider:
    """OpenAI-compatible client for Moonshot Kimi (kimi-k2.6 et al.).

    The Moonshot Platform exposes /v1/chat/completions with the standard OpenAI
    schema. We translate from v6's neutral message format (Anthropic-shaped:
    blocks of {type: "text"|"tool_use"|"tool_result"}) into OpenAI's shape
    (separate role="assistant" with tool_calls + role="tool" with tool_call_id)
    on the way in, and back to UnifiedResponse on the way out.
    """

    DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"

    def __init__(self):
        self._api_key = os.getenv("MOONSHOT_API_KEY", "").strip()
        self._base_url = os.getenv("MOONSHOT_BASE_URL", self.DEFAULT_BASE_URL).rstrip("/")

    def available(self) -> bool:
        return bool(self._api_key)

    def create_message(
        self,
        model_id: str,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> UnifiedResponse:
        from services._llm_retry import post_with_retry

        oa_messages = [{"role": "system", "content": system}] + self._translate_messages(messages)
        oa_tools = self._translate_tools(tools) if tools else None

        body: dict[str, Any] = {
            "model": model_id,
            "messages": oa_messages,
            "max_tokens": max_tokens,
            # Kimi K2.6 (and the K2 reasoning variants) reject anything but
            # temperature=1 — they 400 with "invalid temperature: only 1 is
            # allowed for this model". Older OpenAI-style providers accept any
            # 0–1 value, so 1 is the safe universal default for this gateway.
            "temperature": 1,
        }
        if oa_tools:
            body["tools"] = oa_tools
            body["tool_choice"] = "auto"

        resp = post_with_retry(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json_body=body,
            timeout=180,
            provider_label="kimi",
        )
        if not resp.ok:
            # Surface Moonshot's actual error body — a bare HTTPError says
            # "400 Client Error: Bad Request" with no clue what's wrong.
            try:
                err_body = resp.json()
                err_msg = (
                    err_body.get("error", {}).get("message")
                    or err_body.get("message")
                    or json.dumps(err_body)[:400]
                )
            except (ValueError, AttributeError):
                err_msg = (resp.text or "")[:400]
            print(
                f"[gateway/kimi] {resp.status_code} from {self._base_url} "
                f"model={model_id} body_keys={list(body.keys())} "
                f"oa_messages_count={len(oa_messages)} tools_count={len(oa_tools or [])} "
                f"max_tokens={max_tokens}: {err_msg}"
            )
            raise RuntimeError(f"Kimi API {resp.status_code}: {err_msg}")
        return self._normalize(resp.json())

    # -- Translators --

    def _translate_tools(self, tools: list[dict]) -> list[dict]:
        """Anthropic tool format → OpenAI function tool format."""
        out = []
        for t in tools:
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        return out

    def _translate_messages(self, messages: list[dict]) -> list[dict]:
        """Convert neutral (Anthropic-shaped) messages → OpenAI messages.

        - assistant turn with tool_use blocks → {role: assistant, content, tool_calls: [...]}
        - user turn with tool_result blocks  → series of {role: tool, tool_call_id, content}
        """
        out: list[dict] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            # Plain string content
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue

            if not isinstance(content, list):
                continue

            if role == "assistant":
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                reasoning_parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        txt = block.get("text", "")
                        if txt:
                            text_parts.append(txt)
                    elif btype == "thinking":
                        # Anthropic-style thinking block carries Kimi's
                        # reasoning_content forward across tool-loop rounds.
                        rc = block.get("text") or block.get("thinking") or ""
                        if rc:
                            reasoning_parts.append(rc)
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        })
                msg_out: dict[str, Any] = {"role": "assistant"}
                msg_out["content"] = "\n".join(text_parts) if text_parts else None
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                if reasoning_parts:
                    msg_out["reasoning_content"] = "\n".join(reasoning_parts)
                # Skip empty assistant turns (no text AND no tool calls AND
                # no reasoning) — Moonshot rejects them.
                if msg_out["content"] is None and not tool_calls and not reasoning_parts:
                    continue
                out.append(msg_out)
            else:
                # user turn — may contain tool_result blocks and/or text
                text_parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        if isinstance(block, str):
                            text_parts.append(block)
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        txt = block.get("text", "")
                        if txt:
                            text_parts.append(txt)
                    elif btype == "tool_result":
                        result = block.get("content", "")
                        if not isinstance(result, str):
                            try:
                                result = json.dumps(result)
                            except (TypeError, ValueError):
                                result = str(result)
                        out.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id") or "",
                            "content": result,
                        })
                if text_parts:
                    out.append({"role": "user", "content": "\n".join(text_parts)})
        return out

    def _normalize(self, payload: dict) -> UnifiedResponse:
        """OpenAI chat-completion JSON → UnifiedResponse."""
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"

        text = message.get("content") or ""
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
            except (json.JSONDecodeError, TypeError):
                args = {"_raw_arguments": args_raw}
            tool_calls.append(ToolCall(
                id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                name=fn.get("name") or "",
                input=args if isinstance(args, dict) else {"value": args},
            ))

        # K2.6 thinking mode returns its chain-of-thought as reasoning_content
        # next to content/tool_calls. We must round-trip this back on the next
        # message or Moonshot 400s.
        reasoning_content = message.get("reasoning_content") or ""

        usage = payload.get("usage") or {}
        return UnifiedResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason="tool_use" if (tool_calls or finish_reason == "tool_calls") else "end_turn",
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_content=reasoning_content,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  ModelGateway — main entry point
# ═══════════════════════════════════════════════════════════════════════════

class ModelGateway:
    def __init__(self):
        self._kimi = KimiProvider()
        self._gemini = GeminiProvider()
        self._anthropic = AnthropicProvider()

    def available(self) -> bool:
        return self._kimi.available() or self._gemini.available() or self._anthropic.available()

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
    ) -> UnifiedResponse:
        """Route to the requested provider — no silent fallback.

        v6 is intentionally Kimi-only by default. If Kimi (or any explicitly
        requested provider) fails, we surface the error rather than silently
        switching providers — that way A/B test results from the chat UI are
        always attributable to the requested model.

        Re-enable a fallback chain later by adding except handlers around the
        provider calls below.
        """
        alias = model or DEFAULT_MODEL
        entry = MODEL_REGISTRY.get(alias, {"provider": "kimi", "model_id": alias})
        provider_name = entry["provider"]
        model_id = entry["model_id"]

        if provider_name == "kimi":
            if not self._kimi.available():
                raise RuntimeError(
                    "MOONSHOT_API_KEY not configured — set the env var on Cloud Run "
                    "or pass model='gemini-flash-lite' / 'haiku' explicitly."
                )
            return self._kimi.create_message(
                model_id=model_id, max_tokens=max_tokens,
                system=system, messages=messages, tools=tools,
            )

        if provider_name == "gemini":
            if not self._gemini.available():
                raise RuntimeError("GEMINI / Google api_key not configured")
            return self._gemini.create_message(
                model_id=model_id, max_tokens=max_tokens,
                system=system, messages=messages, tools=tools,
            )

        if provider_name == "anthropic":
            if not self._anthropic.available():
                raise RuntimeError("ANTHROPIC_API_KEY not configured")
            return self._anthropic.create_message(
                model_id=model_id, max_tokens=max_tokens,
                system=system, messages=messages, tools=tools,
            )

        raise RuntimeError(f"Unknown provider '{provider_name}' for model alias '{alias}'")

    def health(self) -> dict:
        return {
            "kimi": "configured" if self._kimi.available() else "missing_api_key",
            "gemini": "configured" if self._gemini.available() else "missing_api_key",
            "anthropic": "configured" if self._anthropic.available() else "missing_api_key",
            "default_model": DEFAULT_MODEL,
        }
