from __future__ import annotations

import os


MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}


class AnthropicGateway:
    def __init__(self):
        self.api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()

    def available(self):
        return bool(self.api_key)

    def resolve_model(self, model_name: str | None):
        if not model_name:
            return MODEL_ALIASES["sonnet"]
        return MODEL_ALIASES.get(model_name, model_name)

    def create_message(self, *, model: str | None, max_tokens: int, system: str, messages: list, tools: list):
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key, timeout=120.0)
        return client.messages.create(
            model=self.resolve_model(model),
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )
