"""
Deep Research gateway — frontier models only.

Quality > cost. Routes to one of:
  gemini-3-pro     → Google Gemini 3 Pro (default, GEMINI_API_KEY)
  claude-opus      → Anthropic Claude Opus 4.7 (ANTHROPIC_API_KEY)
  gpt-5-high       → OpenAI GPT-5.5 with high reasoning (OPENAI_API_KEY)

Single-shot completions (no tool loop, no multi-turn). The dossier carries
all the facts; the model's only job is to write the 8-section report.
Returns plain markdown text.

Each provider is its own class with a `complete(system, user) -> CompletionResult`
method, and a `stream_complete(...)` generator that yields typed events:

    {"type": "delta",     "text": "..."}            # partial markdown
    {"type": "citation",  "title": "...", "url": "..."}
    {"type": "web_query", "query": "..."}
    {"type": "done",      "input_tokens": int, "output_tokens": int,
                          "latency_ms": int, "full_text": str,
                          "model_used": str, "citations": [...],
                          "web_queries": [...]}

The route layer wraps each yielded dict in an SSE frame and ships it.
The top-level `DeepResearchGateway.complete()` resolves the alias
and routes. No fallback — if the chosen provider fails, the error surfaces
so the admin sees what went wrong.
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests


# ═══════════════════════════════════════════════════════════════════════════
#  Result type
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CompletionResult:
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model_used: str = ""
    latency_ms: int = 0
    citations: list = field(default_factory=list)  # [{title, url, snippet?}]
    web_queries: list = field(default_factory=list)  # search queries the model issued
    raw: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
#  Model registry
# ═══════════════════════════════════════════════════════════════════════════

MODEL_REGISTRY = {
    # Gemini 3.1 Pro Preview — Google's latest reasoning model with built-in
    # google_search grounding. Released Feb 2026; superseded gemini-3-pro-preview
    # which was discontinued Mar 2026. Default for both Deep Research reports
    # and the new Explore crisp cards (theme thesis / sector thesis / catalysts).
    "gemini-3.1-pro": {
        "provider": "gemini",
        "model_id": "gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro",
    },
    # Kept for backward compat — admin overrides may still pin this. The
    # provider-side model_id is dead, so calls will 404 until Google
    # re-exposes it; treat as legacy.
    "gemini-3-pro": {
        "provider": "gemini",
        "model_id": "gemini-3-pro-preview",
        "label": "Gemini 3 Pro (legacy)",
    },
    "claude-opus": {
        "provider": "anthropic",
        "model_id": "claude-opus-4-7",
        "label": "Claude Opus 4.7",
    },
    "gpt-5-high": {
        "provider": "openai",
        "model_id": "gpt-5.5",
        "reasoning_effort": "high",
        "label": "GPT-5.5 (high reasoning)",
    },
}

DEFAULT_MODEL = "gemini-3.1-pro"


# ═══════════════════════════════════════════════════════════════════════════
#  Gemini provider (native generateContent + Google Search grounding)
# ═══════════════════════════════════════════════════════════════════════════

class GeminiProvider:
    """Native Gemini API with Google Search grounding.

    Uses :generateContent (not the OpenAI-compat shim) because the
    OpenAI-compat layer strips the `tools: [{google_search}]` field.
    With grounding the model actually browses the web for the latest
    filings, news, and quarterly commentary, then we surface the cited
    URLs back through CompletionResult.citations.
    """
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self):
        self._api_key = (os.getenv("GEMINI_API_KEY", "") or os.getenv("api_key", "")).strip()
        self._base_url = os.getenv("GEMINI_BASE_URL_NATIVE", self.DEFAULT_BASE_URL).rstrip("/")

    def available(self) -> bool:
        return bool(self._api_key)

    def complete(
        self, *, model_id: str, system: str, user: str, max_tokens: int,
        thinking_level: str | None = None,
    ) -> CompletionResult:
        from services._llm_retry import post_with_retry
        import time as _time

        # On Gemini 3.x reasoning models the model spends part of its
        # output-token budget on internal "thinking" before writing the
        # final response. Default thinking is roughly MEDIUM (1k–3k
        # thinking tokens), which silently truncates short-task callers
        # like sector-thesis (max_tokens=400 → ~13 actual response
        # tokens, sentence cut off mid-clause, google_search never even
        # fires). Pass `thinking_level="low"` for short structured
        # outputs and the model writes promptly. None preserves prior
        # behaviour for callers that didn't opt in (e.g. Deep Research
        # on legacy `gemini-3-pro-preview`, which doesn't recognise the
        # field — Gemini returns 400 if we send it on incompatible
        # models, so we only attach it when the caller asked).
        gen_config: dict[str, Any] = {
            "temperature": 0.4,
            "maxOutputTokens": max_tokens,
        }
        if thinking_level is not None:
            gen_config["thinkingConfig"] = {
                "thinkingLevel": thinking_level.lower(),
            }

        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": gen_config,
        }

        t0 = _time.time()
        resp = post_with_retry(
            f"{self._base_url}/models/{model_id}:generateContent",
            headers={
                "x-goog-api-key": self._api_key,
                "Content-Type": "application/json",
            },
            json_body=body,
            timeout=600,
            provider_label="deepresearch/gemini",
        )
        latency_ms = int((_time.time() - t0) * 1000)

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
            raise RuntimeError(f"Gemini API {resp.status_code}: {err_msg}")

        payload = resp.json()
        candidate = ((payload.get("candidates") or []) + [{}])[0]
        content = candidate.get("content") or {}
        text_parts: list[str] = []
        for p in content.get("parts") or []:
            t = p.get("text")
            if t:
                text_parts.append(t)
        text = "".join(text_parts)

        # Citations from grounding metadata.
        citations: list[dict] = []
        web_queries: list[str] = []
        gm = candidate.get("groundingMetadata") or {}
        for chunk in gm.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            uri = web.get("uri")
            if uri:
                citations.append({
                    "title": web.get("title") or "",
                    "url": uri,
                })
        for q in gm.get("webSearchQueries") or []:
            if isinstance(q, str) and q.strip():
                web_queries.append(q.strip())

        usage = payload.get("usageMetadata") or {}

        return CompletionResult(
            text=text,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            model_used=model_id,
            latency_ms=latency_ms,
            citations=citations,
            web_queries=web_queries,
            raw=payload,
        )

    def stream_complete(
        self, *, model_id: str, system: str, user: str, max_tokens: int,
    ) -> Iterator[dict]:
        """SSE-style streaming via :streamGenerateContent?alt=sse.

        Each chunk carries a `candidates[0].content.parts[*].text` slice plus
        cumulative `groundingMetadata`. We yield text deltas as they arrive
        and citations whenever a new chunk surfaces them. The final `done`
        event carries usage metadata from the last chunk.
        """
        import time as _time
        body: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": max_tokens,
            },
        }
        url = f"{self._base_url}/models/{model_id}:streamGenerateContent?alt=sse"
        headers = {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        full_text: list[str] = []
        citations: list[dict] = []
        web_queries: list[str] = []
        seen_urls: set[str] = set()
        seen_queries: set[str] = set()
        input_tokens = 0
        output_tokens = 0
        t0 = _time.time()

        with requests.post(url, headers=headers, json=body, stream=True, timeout=900) as resp:
            if not resp.ok:
                try:
                    err = resp.text[:400]
                except Exception:
                    err = ""
                raise RuntimeError(f"Gemini stream API {resp.status_code}: {err}")
            # Gemini's SSE endpoint doesn't always advertise a charset, which
            # makes `requests` fall back to ISO-8859-1 — that mangles ₹ and
            # emoji into mojibake (â¹, ð¢ etc.). Force UTF-8.
            resp.encoding = "utf-8"

            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                if not raw_line.startswith("data:"):
                    continue
                data = raw_line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                candidate = ((payload.get("candidates") or []) + [{}])[0]
                content = candidate.get("content") or {}
                for p in content.get("parts") or []:
                    t = p.get("text")
                    if t:
                        full_text.append(t)
                        yield {"type": "delta", "text": t}
                gm = candidate.get("groundingMetadata") or {}
                for chunk in gm.get("groundingChunks") or []:
                    web = chunk.get("web") or {}
                    uri = web.get("uri")
                    if uri and uri not in seen_urls:
                        seen_urls.add(uri)
                        cite = {"title": web.get("title") or "", "url": uri}
                        citations.append(cite)
                        yield {"type": "citation", **cite}
                for q in gm.get("webSearchQueries") or []:
                    if isinstance(q, str) and q.strip() and q not in seen_queries:
                        seen_queries.add(q)
                        web_queries.append(q.strip())
                        yield {"type": "web_query", "query": q.strip()}
                usage = payload.get("usageMetadata") or {}
                if usage:
                    input_tokens = int(usage.get("promptTokenCount") or input_tokens)
                    output_tokens = int(usage.get("candidatesTokenCount") or output_tokens)

        latency_ms = int((_time.time() - t0) * 1000)
        yield {
            "type": "done",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "full_text": "".join(full_text),
            "model_used": model_id,
            "citations": citations,
            "web_queries": web_queries,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  Anthropic provider (native /v1/messages)
# ═══════════════════════════════════════════════════════════════════════════

class AnthropicProvider:
    DEFAULT_BASE_URL = "https://api.anthropic.com/v1"

    def __init__(self):
        self._api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        self._base_url = os.getenv("ANTHROPIC_BASE_URL", self.DEFAULT_BASE_URL).rstrip("/")

    def available(self) -> bool:
        return bool(self._api_key)

    def complete(self, *, model_id: str, system: str, user: str, max_tokens: int) -> CompletionResult:
        from services._llm_retry import post_with_retry
        import time as _time

        body: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            # Anthropic-managed web search tool. The model decides when to call it.
            "tools": [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8,
            }],
        }

        t0 = _time.time()
        resp = post_with_retry(
            f"{self._base_url}/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json_body=body,
            timeout=900,
            provider_label="deepresearch/anthropic",
        )
        latency_ms = int((_time.time() - t0) * 1000)

        if not resp.ok:
            try:
                err_body = resp.json()
                err_msg = (
                    err_body.get("error", {}).get("message")
                    or json.dumps(err_body)[:400]
                )
            except (ValueError, AttributeError):
                err_msg = (resp.text or "")[:400]
            raise RuntimeError(f"Anthropic API {resp.status_code}: {err_msg}")

        payload = resp.json()
        text_parts: list[str] = []
        citations: list[dict] = []
        web_queries: list[str] = []
        for block in payload.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
                # Inline citations attached to text blocks.
                for c in block.get("citations") or []:
                    url = c.get("url") or c.get("source", {}).get("url") if isinstance(c.get("source"), dict) else None
                    title = c.get("title") or ""
                    if url:
                        citations.append({"title": title, "url": url})
            elif btype == "server_tool_use" and block.get("name") == "web_search":
                q = (block.get("input") or {}).get("query")
                if q:
                    web_queries.append(str(q))
            elif btype == "web_search_tool_result":
                for r in (block.get("content") or []):
                    if isinstance(r, dict) and r.get("type") == "web_search_result":
                        url = r.get("url")
                        if url:
                            citations.append({
                                "title": r.get("title") or "",
                                "url": url,
                            })
        text = "\n".join(text_parts)

        # De-dupe citations on URL.
        seen: set[str] = set()
        deduped: list[dict] = []
        for c in citations:
            u = c.get("url")
            if not u or u in seen:
                continue
            seen.add(u)
            deduped.append(c)

        usage = payload.get("usage") or {}
        return CompletionResult(
            text=text,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model_used=model_id,
            latency_ms=latency_ms,
            citations=deduped,
            web_queries=web_queries,
            raw=payload,
        )

    def stream_complete(
        self, *, model_id: str, system: str, user: str, max_tokens: int,
    ) -> Iterator[dict]:
        """SSE streaming via /v1/messages stream=true.

        Anthropic's stream emits typed events: `message_start`,
        `content_block_start/delta/stop`, `message_delta`, `message_stop`.
        We translate text deltas into our delta events and surface citations
        when web_search_tool_result blocks land.
        """
        import time as _time

        body: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "stream": True,
            "tools": [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8,
            }],
        }

        url = f"{self._base_url}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        full_text: list[str] = []
        citations: list[dict] = []
        web_queries: list[str] = []
        seen_urls: set[str] = set()
        input_tokens = 0
        output_tokens = 0
        # Track per-block context. Anthropic interleaves text blocks with tool
        # blocks; the block index lets us know which one a delta belongs to.
        active_block: dict[int, dict] = {}
        t0 = _time.time()

        with requests.post(url, headers=headers, json=body, stream=True, timeout=900) as resp:
            if not resp.ok:
                try:
                    err = resp.text[:400]
                except Exception:
                    err = ""
                raise RuntimeError(f"Anthropic stream API {resp.status_code}: {err}")
            # text/event-stream without an explicit charset → requests falls
            # back to ISO-8859-1, which renders ₹ and emoji as mojibake. Force
            # UTF-8 so multi-byte Unicode survives intact.
            resp.encoding = "utf-8"

            current_event: str | None = None
            for raw_line in resp.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue
                line = raw_line.strip()
                if not line:
                    current_event = None
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue

                etype = payload.get("type") or current_event
                if etype == "message_start":
                    usage = (payload.get("message") or {}).get("usage") or {}
                    input_tokens = int(usage.get("input_tokens") or 0)
                elif etype == "content_block_start":
                    idx = payload.get("index", 0)
                    block = payload.get("content_block") or {}
                    active_block[idx] = block
                    btype = block.get("type")
                    if btype == "server_tool_use" and block.get("name") == "web_search":
                        q = (block.get("input") or {}).get("query")
                        if q:
                            q = str(q)
                            web_queries.append(q)
                            yield {"type": "web_query", "query": q}
                    elif btype == "web_search_tool_result":
                        for r in block.get("content") or []:
                            if isinstance(r, dict) and r.get("type") == "web_search_result":
                                u = r.get("url")
                                if u and u not in seen_urls:
                                    seen_urls.add(u)
                                    cite = {"title": r.get("title") or "", "url": u}
                                    citations.append(cite)
                                    yield {"type": "citation", **cite}
                elif etype == "content_block_delta":
                    delta = payload.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        t = delta.get("text") or ""
                        if t:
                            full_text.append(t)
                            yield {"type": "delta", "text": t}
                    elif dtype == "input_json_delta":
                        # tool_use input streamed as JSON fragments — we don't
                        # need to surface these as user-visible content.
                        pass
                elif etype == "content_block_stop":
                    idx = payload.get("index", 0)
                    block = active_block.pop(idx, {}) or {}
                    # Citations attached to text blocks may arrive here.
                    if block.get("type") == "text":
                        for c in block.get("citations") or []:
                            u = (c.get("url")
                                 or (isinstance(c.get("source"), dict) and c["source"].get("url")))
                            if u and u not in seen_urls:
                                seen_urls.add(u)
                                cite = {"title": c.get("title") or "", "url": u}
                                citations.append(cite)
                                yield {"type": "citation", **cite}
                elif etype == "message_delta":
                    usage = payload.get("usage") or {}
                    if usage.get("output_tokens") is not None:
                        output_tokens = int(usage["output_tokens"])
                elif etype == "message_stop":
                    break
                elif etype == "error":
                    err = payload.get("error") or {}
                    raise RuntimeError(
                        f"Anthropic stream error: {err.get('type')}: {err.get('message')}"
                    )

        latency_ms = int((_time.time() - t0) * 1000)
        yield {
            "type": "done",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "full_text": "".join(full_text),
            "model_used": model_id,
            "citations": citations,
            "web_queries": web_queries,
        }


# ═══════════════════════════════════════════════════════════════════════════
#  OpenAI provider (Responses API for GPT-5.5 reasoning)
# ═══════════════════════════════════════════════════════════════════════════

class OpenAIProvider:
    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(self):
        self._api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self._base_url = os.getenv("OPENAI_BASE_URL", self.DEFAULT_BASE_URL).rstrip("/")

    def available(self) -> bool:
        return bool(self._api_key)

    def complete(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int,
        reasoning_effort: str = "high",
    ) -> CompletionResult:
        from services._llm_retry import post_with_retry
        import time as _time

        # GPT-5.5 uses Responses API. instructions = system; input = user.
        body: dict[str, Any] = {
            "model": model_id,
            "instructions": system,
            "input": user,
            "max_output_tokens": max_tokens,
            "reasoning": {"effort": reasoning_effort},
        }

        t0 = _time.time()
        resp = post_with_retry(
            f"{self._base_url}/responses",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json_body=body,
            timeout=900,
            provider_label="deepresearch/openai",
        )
        latency_ms = int((_time.time() - t0) * 1000)

        if not resp.ok:
            try:
                err_body = resp.json()
                err_msg = (
                    err_body.get("error", {}).get("message")
                    or json.dumps(err_body)[:400]
                )
            except (ValueError, AttributeError):
                err_msg = (resp.text or "")[:400]
            raise RuntimeError(f"OpenAI API {resp.status_code}: {err_msg}")

        payload = resp.json()
        # Responses API returns output_text top-level, plus structured output array.
        text = payload.get("output_text") or ""
        if not text:
            for item in payload.get("output") or []:
                if item.get("type") == "message":
                    for c in item.get("content") or []:
                        if c.get("type") == "output_text":
                            text += c.get("text", "")

        usage = payload.get("usage") or {}
        return CompletionResult(
            text=text,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            model_used=model_id,
            latency_ms=latency_ms,
            raw=payload,
        )

    def stream_complete(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int,
        reasoning_effort: str = "high",
    ) -> Iterator[dict]:
        """SSE streaming via /v1/responses stream=true.

        OpenAI's Responses API emits typed events. The two we care about are:
          response.output_text.delta  — text fragment in `delta` field
          response.completed          — final payload with usage in `response.usage`
        """
        import time as _time

        body: dict[str, Any] = {
            "model": model_id,
            "instructions": system,
            "input": user,
            "max_output_tokens": max_tokens,
            "reasoning": {"effort": reasoning_effort},
            "stream": True,
        }
        url = f"{self._base_url}/responses"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        full_text: list[str] = []
        input_tokens = 0
        output_tokens = 0
        t0 = _time.time()

        with requests.post(url, headers=headers, json=body, stream=True, timeout=900) as resp:
            if not resp.ok:
                try:
                    err = resp.text[:400]
                except Exception:
                    err = ""
                raise RuntimeError(f"OpenAI stream API {resp.status_code}: {err}")
            # SSE without explicit charset → ISO-8859-1 fallback in requests,
            # which mangles ₹ and emoji. Force UTF-8.
            resp.encoding = "utf-8"

            current_event: str | None = None
            for raw_line in resp.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue
                line = raw_line.strip()
                if not line:
                    current_event = None
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue

                etype = payload.get("type") or current_event
                if etype == "response.output_text.delta":
                    t = payload.get("delta") or ""
                    if t:
                        full_text.append(t)
                        yield {"type": "delta", "text": t}
                elif etype in ("response.completed", "response.done"):
                    response_obj = payload.get("response") or {}
                    usage = response_obj.get("usage") or {}
                    input_tokens = int(usage.get("input_tokens") or input_tokens)
                    output_tokens = int(usage.get("output_tokens") or output_tokens)
                    # Recover full text from the response object as a fallback —
                    # if some deltas were dropped this is still authoritative.
                    if not full_text:
                        full = response_obj.get("output_text") or ""
                        if full:
                            full_text.append(full)
                elif etype == "response.failed":
                    err = (payload.get("response") or {}).get("error") or {}
                    raise RuntimeError(
                        f"OpenAI stream error: {err.get('code')}: {err.get('message')}"
                    )

        latency_ms = int((_time.time() - t0) * 1000)
        yield {
            "type": "done",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "full_text": "".join(full_text),
            "model_used": model_id,
            "citations": [],
            "web_queries": [],
        }


# ═══════════════════════════════════════════════════════════════════════════
#  Top-level gateway
# ═══════════════════════════════════════════════════════════════════════════

class DeepResearchGateway:
    def __init__(self):
        self._gemini = GeminiProvider()
        self._anthropic = AnthropicProvider()
        self._openai = OpenAIProvider()

    def available_models(self) -> list[dict]:
        out = []
        for alias, entry in MODEL_REGISTRY.items():
            provider = entry["provider"]
            if provider == "gemini":
                ok = self._gemini.available()
            elif provider == "anthropic":
                ok = self._anthropic.available()
            elif provider == "openai":
                ok = self._openai.available()
            else:
                ok = False
            out.append({
                "alias": alias,
                "label": entry.get("label", alias),
                "provider": provider,
                "available": ok,
            })
        return out

    def complete(
        self,
        *,
        model: str | None,
        system: str,
        user: str,
        max_tokens: int = 16000,
    ) -> CompletionResult:
        alias = model or DEFAULT_MODEL
        entry = MODEL_REGISTRY.get(alias)
        if not entry:
            raise RuntimeError(f"Unknown model alias '{alias}'.")

        provider = entry["provider"]
        model_id = entry["model_id"]

        if provider == "gemini":
            if not self._gemini.available():
                raise RuntimeError("GEMINI_API_KEY not configured.")
            return self._gemini.complete(
                model_id=model_id, system=system, user=user, max_tokens=max_tokens,
            )

        if provider == "anthropic":
            if not self._anthropic.available():
                raise RuntimeError("ANTHROPIC_API_KEY not configured.")
            return self._anthropic.complete(
                model_id=model_id, system=system, user=user, max_tokens=max_tokens,
            )

        if provider == "openai":
            if not self._openai.available():
                raise RuntimeError("OPENAI_API_KEY not configured.")
            return self._openai.complete(
                model_id=model_id,
                system=system,
                user=user,
                max_tokens=max_tokens,
                reasoning_effort=entry.get("reasoning_effort", "high"),
            )

        raise RuntimeError(f"Unsupported provider '{provider}' for alias '{alias}'.")

    def stream_complete(
        self,
        *,
        model: str | None,
        system: str,
        user: str,
        max_tokens: int = 16000,
    ) -> Iterator[dict]:
        """Route streaming to the right provider.

        Yields dicts of:
          {"type": "delta", "text": "..."}
          {"type": "citation", "title": "...", "url": "..."}
          {"type": "web_query", "query": "..."}
          {"type": "done", "input_tokens": ..., "output_tokens": ...,
                           "latency_ms": ..., "full_text": "...",
                           "citations": [...], "web_queries": [...]}
        """
        alias = model or DEFAULT_MODEL
        entry = MODEL_REGISTRY.get(alias)
        if not entry:
            raise RuntimeError(f"Unknown model alias '{alias}'.")

        provider = entry["provider"]
        model_id = entry["model_id"]

        if provider == "gemini":
            if not self._gemini.available():
                raise RuntimeError("GEMINI_API_KEY not configured.")
            yield from self._gemini.stream_complete(
                model_id=model_id, system=system, user=user, max_tokens=max_tokens,
            )
            return

        if provider == "anthropic":
            if not self._anthropic.available():
                raise RuntimeError("ANTHROPIC_API_KEY not configured.")
            yield from self._anthropic.stream_complete(
                model_id=model_id, system=system, user=user, max_tokens=max_tokens,
            )
            return

        if provider == "openai":
            if not self._openai.available():
                raise RuntimeError("OPENAI_API_KEY not configured.")
            yield from self._openai.stream_complete(
                model_id=model_id,
                system=system,
                user=user,
                max_tokens=max_tokens,
                reasoning_effort=entry.get("reasoning_effort", "high"),
            )
            return

        raise RuntimeError(f"Unsupported provider '{provider}' for alias '{alias}'.")

    def health(self) -> dict:
        return {
            "gemini": "configured" if self._gemini.available() else "missing_api_key",
            "anthropic": "configured" if self._anthropic.available() else "missing_api_key",
            "openai": "configured" if self._openai.available() else "missing_api_key",
            "default_model": DEFAULT_MODEL,
        }
