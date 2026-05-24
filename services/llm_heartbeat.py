"""
LLM provider heartbeat.

Sends a tiny ping to the configured provider (DeepSeek via V7) to verify
the API key works and the model responds. Returns per-provider status +
latency so an external uptime monitor can alert on outages.

Designed to be cheap: no system prompt, ~5 input tokens, max 4 output tokens.
At 1 ping per minute the cost is negligible (sub-rupee/month).

V2-V6 providers (Gemini Flash, Kimi K2.6) were retired with the V7
consolidation on 2026-05-10 — see Backend/_legacy/LEGACY_VALVO_AI.md.
"""
from __future__ import annotations

import time
from typing import Any


_PING_PROMPT = "Reply with: OK"
_PING_MAX_TOKENS = 4


def _ping_one(label: str, provider, model_id: str) -> dict[str, Any]:
    """Ping a single provider. Returns status dict."""
    start = time.time()
    try:
        result = provider.create_message(
            model_id=model_id,
            max_tokens=_PING_MAX_TOKENS,
            system="You are a health check. Reply with the literal text: OK",
            messages=[{"role": "user", "content": _PING_PROMPT}],
            tools=[],
        )
        latency_ms = int((time.time() - start) * 1000)
        text = (result.text or "").strip()
        ok = bool(text)
        return {
            "provider": label,
            "model": model_id,
            "ok": ok,
            "latency_ms": latency_ms,
            "response_excerpt": text[:80] if text else None,
        }
    except Exception as exc:
        latency_ms = int((time.time() - start) * 1000)
        return {
            "provider": label,
            "model": model_id,
            "ok": False,
            "latency_ms": latency_ms,
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


def check_all() -> dict[str, Any]:
    """Ping every configured provider. Returns aggregate status."""
    results: list[dict[str, Any]] = []

    # DeepSeek (V7 default — only live engine after 2026-05-10)
    try:
        from services.valvo_ai_v7.gateway import DeepSeekProvider, DEFAULT_MODEL
        dp = DeepSeekProvider()
        if dp.available():
            results.append(_ping_one("deepseek", dp, DEFAULT_MODEL))
        else:
            results.append({"provider": "deepseek", "ok": False, "skipped": "missing_api_key"})
    except Exception as exc:
        results.append({"provider": "deepseek", "ok": False, "error": f"init failed: {exc}"})

    all_ok = all(r.get("ok") for r in results if not r.get("skipped"))
    has_active = any(not r.get("skipped") for r in results)

    return {
        "ok": all_ok and has_active,
        "providers": results,
        "checked_at": int(time.time()),
    }
