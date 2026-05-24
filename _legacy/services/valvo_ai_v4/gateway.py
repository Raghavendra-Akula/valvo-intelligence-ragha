"""
Valvo AI v4 -- Gemini dual-model gateway.

Flash Lite for simple queries (fast, cheap).
Flash for complex queries (smart, accurate).
Smart router picks the right model — zero extra latency.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any


FLASH_MODEL = "gemini-2.5-flash"
FLASH_LITE_MODEL = "gemini-2.5-flash-lite"


# ═══════════════════════════════════════════════════════════════════════════
#  Unified response types (provider-agnostic)
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
    stop_reason: str = "end_turn"       # "end_turn" or "tool_use"
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
#  Gemini Flash Gateway
# ═══════════════════════════════════════════════════════════════════════════

class GeminiFlashGateway:
    """Dual-model gateway: Flash Lite (simple) + Flash (complex)."""

    def __init__(self):
        from google import genai
        api_key = os.getenv("api_key", "").strip()
        self._available = bool(api_key)
        if self._available:
            # Try HttpOptions object first, fall back to dict
            try:
                from google.genai import types as genai_types
                http_opts = genai_types.HttpOptions(timeout=180 * 1000)
            except (ImportError, TypeError):
                http_opts = {"timeout": 180}
            self._client = genai.Client(api_key=api_key, http_options=http_opts)
        self._tool_id_map: dict[str, str] = {}

    def available(self) -> bool:
        return self._available

    def create_message(
        self,
        *,
        model_id: str | None = None,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ) -> UnifiedResponse:
        from google.genai import types

        # Default to flash (not flash-lite) so v4 quality matches v5.
        actual_model = model_id or FLASH_MODEL
        gemini_tools = self._translate_tools(tools) if tools else None
        contents = self._translate_messages(messages)

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=0.15 if actual_model == FLASH_LITE_MODEL else 0.2,
        )
        if gemini_tools:
            config.tools = gemini_tools

        # Retry once on timeout
        last_err = None
        for attempt in range(2):
            try:
                response = self._client.models.generate_content(
                    model=actual_model,
                    contents=contents,
                    config=config,
                )
                return self._normalize(response)
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if "timed out" in err_str or "timeout" in err_str:
                    if attempt == 0:
                        print(f"[gateway] {actual_model} timed out, retrying...")
                        import time
                        time.sleep(2)
                        continue
                raise
        raise last_err

    def create_message_stream(
        self,
        *,
        model_id: str | None = None,
        max_tokens: int,
        system: str,
        messages: list[dict],
        tools: list[dict],
    ):
        """
        Streaming variant of create_message.

        Yields dict events as Gemini produces them:
          {"type": "text_delta", "delta": "..."}     — partial text token(s)
          {"type": "complete", "response": UnifiedResponse}  — always last

        Tool-call parts arrive atomically in a single chunk (Gemini doesn't
        stream partial function arguments), so tool-using turns may produce
        zero text_delta events — all the action is in the terminal `complete`
        event. Text-only turns stream incrementally as intended.
        """
        from google.genai import types

        # Default to flash (not flash-lite) so v4 quality matches v5.
        actual_model = model_id or FLASH_MODEL
        gemini_tools = self._translate_tools(tools) if tools else None
        contents = self._translate_messages(messages)

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=0.15 if actual_model == FLASH_LITE_MODEL else 0.2,
        )
        if gemini_tools:
            config.tools = gemini_tools

        text_acc: list[str] = []
        tool_calls: list[ToolCall] = []
        input_tokens = 0
        output_tokens = 0

        try:
            stream = self._client.models.generate_content_stream(
                model=actual_model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            # Fall back to non-streaming on any pre-stream failure
            print(f"[gateway] stream init failed ({e}); falling back to non-streaming")
            resp = self.create_message(
                model_id=model_id, max_tokens=max_tokens,
                system=system, messages=messages, tools=tools,
            )
            yield {"type": "complete", "response": resp}
            return

        try:
            for chunk in stream:
                try:
                    candidates = chunk.candidates or []
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

    # ----- Tool translation (Anthropic schema -> Gemini FunctionDeclaration) -----

    def _translate_tools(self, tools: list[dict]) -> list:
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
        if schema.get("properties"):
            props = {}
            for name, prop_schema in schema["properties"].items():
                props[name] = self._translate_schema(prop_schema)
            kwargs["properties"] = props
        if schema.get("required"):
            kwargs["required"] = schema["required"]
        if schema.get("items"):
            kwargs["items"] = self._translate_schema(schema["items"])

        return types.Schema(**kwargs)

    # ----- Message translation (neutral -> Gemini Content) -----

    def _translate_messages(self, messages: list[dict]) -> list:
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
                            tool_name = self._tool_id_map.get(
                                block.get("tool_use_id", ""), "unknown",
                            )
                            result_content = block.get("content", "")
                            try:
                                result_data = (
                                    json.loads(result_content)
                                    if isinstance(result_content, str)
                                    else result_content
                                )
                            except (json.JSONDecodeError, TypeError):
                                result_data = {"result": result_content}
                            parts.append(types.Part.from_function_response(
                                name=tool_name,
                                response=(
                                    result_data
                                    if isinstance(result_data, dict)
                                    else {"result": result_data}
                                ),
                            ))
                if parts:
                    contents.append(types.Content(role=role, parts=parts))
        return contents

    # ----- Response normalization -----

    def _normalize(self, response) -> UnifiedResponse:
        text_parts = []
        tool_calls = []

        try:
            candidates = response.candidates or []
            if candidates and candidates[0].content and candidates[0].content.parts:
                for part in candidates[0].content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        tc_id = f"gemini_{uuid.uuid4().hex[:12]}"
                        name = part.function_call.name
                        args = dict(part.function_call.args) if part.function_call.args else {}
                        self._tool_id_map[tc_id] = name
                        tool_calls.append(ToolCall(id=tc_id, name=name, input=args))
                    elif hasattr(part, "text") and part.text:
                        text_parts.append(part.text)
        except (IndexError, AttributeError, TypeError) as e:
            print(f"[gateway] _normalize parse error: {e}")

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

    def health(self) -> dict:
        return {
            "provider": "gemini",
            "models": {
                "simple": FLASH_LITE_MODEL,
                "complex": FLASH_MODEL,
            },
            "status": "configured" if self._available else "missing_api_key",
        }
