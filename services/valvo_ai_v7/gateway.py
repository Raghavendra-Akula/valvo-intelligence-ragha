"""
Valvo AI v7 -- DeepSeek only.

Cloned from v3 (so the engine logic stays familiar) and swaps the Gemini
provider for DeepSeek (OpenAI-compatible REST). Default routes to
deepseek-chat (V3.x). deepseek-reasoner (R1/R2) registered for opt-in.

Single provider, no fallback. If DeepSeek fails, the gateway raises so
the caller sees the real error instead of silently serving a different
model. Returns a UnifiedResponse so the engine loop stays provider-
agnostic.
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
    # DeepSeek-R1 / R2 ("deepseek-reasoner") returns reasoning_content
    # alongside content + tool_calls. Captured here so it can be echoed
    # back on the next round if the provider needs it (same pattern as
    # Kimi K2.6 in v6). deepseek-chat (V3.x) leaves this empty.
    reasoning_content: str = ""

    def to_message_content(self) -> list[dict]:
        """Provider-neutral representation of the assistant turn."""
        parts = []
        if self.reasoning_content:
            # Anthropic-style thinking block. The DeepSeek translator
            # below picks it up and re-emits as message.reasoning_content.
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
    "deepseek-chat":     {"provider": "deepseek", "model_id": "deepseek-chat"},
    "deepseek-reasoner": {"provider": "deepseek", "model_id": "deepseek-reasoner"},
}

DEFAULT_MODEL = "deepseek-chat"


# ═══════════════════════════════════════════════════════════════════════════
#  DeepSeek Provider (OpenAI-compatible REST)
# ═══════════════════════════════════════════════════════════════════════════

class DeepSeekProvider:
    """Talks to DeepSeek's OpenAI-compatible chat-completions endpoint.

    DeepSeek exposes the standard OpenAI chat-completions schema at
    https://api.deepseek.com/v1/chat/completions, so the request/response
    shape is identical to v3's GeminiProvider and v6's KimiProvider.
    Same translator (Anthropic-shaped messages → OpenAI shape), same
    _normalize, same empty-message guard. One mental model across all
    three providers.

    Models in MODEL_REGISTRY:
      deepseek-chat      → DeepSeek-V3.x (general chat, fast, cheap)
      deepseek-reasoner  → DeepSeek-R1/R2 (thinking model, deeper reasoning)

    Env vars:
      DEEPSEEK_API_KEY   — required
      DEEPSEEK_BASE_URL  — optional override (e.g. for staging / regional host)
    """

    DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

    def __init__(self):
        self._api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self._base_url = os.getenv("DEEPSEEK_BASE_URL", self.DEFAULT_BASE_URL).rstrip("/")

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
            "temperature": 0.3,
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
            provider_label="deepseek",
        )
        if not resp.ok:
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
                f"[gateway/deepseek] {resp.status_code} from {self._base_url} "
                f"model={model_id} oa_messages={len(oa_messages)} "
                f"tools={len(oa_tools or [])} max_tokens={max_tokens}: {err_msg}"
            )
            raise RuntimeError(f"DeepSeek API {resp.status_code}: {err_msg}")
        return self._normalize(resp.json())

    # -- Translators (identical to KimiProvider / GeminiProvider) --

    def _translate_tools(self, tools: list[dict]) -> list[dict]:
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
        """Convert neutral (Anthropic-shaped) messages → OpenAI messages."""
        out: list[dict] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

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
                if msg_out["content"] is None and not tool_calls and not reasoning_parts:
                    continue
                out.append(msg_out)
            else:
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

        # DeepSeek-R1 / R2 ("deepseek-reasoner") returns reasoning_content
        # alongside tool_calls in thinking mode. Capture it so the engine
        # can round-trip it on the next round (same pattern as Kimi K2.6).
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
        self._deepseek = DeepSeekProvider()

    def available(self) -> bool:
        return self._deepseek.available()

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
        """Route to DeepSeek — no fallback. Failures raise."""
        alias = model or DEFAULT_MODEL
        entry = MODEL_REGISTRY.get(alias, {"provider": "deepseek", "model_id": alias})
        if entry["provider"] != "deepseek":
            raise RuntimeError(
                f"v7 is DeepSeek-only — model alias '{alias}' (provider={entry['provider']}) is not supported."
            )
        if not self._deepseek.available():
            raise RuntimeError(
                "DEEPSEEK_API_KEY not configured — set the env var on Cloud Run."
            )
        return self._deepseek.create_message(
            model_id=entry["model_id"], max_tokens=max_tokens,
            system=system, messages=messages, tools=tools,
        )

    def health(self) -> dict:
        return {
            "deepseek": "configured" if self._deepseek.available() else "missing_api_key",
            "default_model": DEFAULT_MODEL,
        }
