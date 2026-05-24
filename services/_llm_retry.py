"""
Shared retry + backoff helper for LLM provider HTTP calls.

Used by valvo_ai_v3.gateway.GeminiProvider and valvo_ai_v6.gateway.KimiProvider.
Both providers are OpenAI-compatible REST clients and share the same set of
transient failure modes — 5xx, 429, timeouts, connection resets.

Behavior:
  - Up to 4 attempts (1 initial + 3 retries) with 1s/2s/4s exponential backoff.
  - Retries on 429 / 500 / 502 / 503 / 504 and on connection-level errors.
  - Does NOT retry on other 4xx — those signal a bug in our request, not a
    transient outage. Surfacing them quickly lets us patch.
  - Returns the final response object; caller decides what to do with !ok.
  - Logs each retry attempt with provider label so Cloud Run logs are diagnosable.

Sentry will pick up the eventual exception if all attempts fail.
"""
from __future__ import annotations

import time
from typing import Any

import requests


_RETRY_STATUS = {429, 500, 502, 503, 504}
_BACKOFFS = (1, 2, 4)  # seconds between attempts; 4 total attempts


def post_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: int = 180,
    provider_label: str = "llm",
) -> requests.Response:
    """POST with exponential backoff on transient errors.

    Returns the final Response. Caller checks .ok / .status_code. If every
    attempt raised at the network level (no Response ever received), re-raises
    the last network exception.
    """
    last_response: requests.Response | None = None
    last_exception: Exception | None = None

    for attempt in range(len(_BACKOFFS) + 1):
        try:
            resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            last_exception = exc
            print(
                f"[llm-retry/{provider_label}] attempt {attempt + 1} "
                f"{type(exc).__name__}: {str(exc)[:160]}"
            )
        else:
            # Success or non-retryable status: return immediately.
            if resp.ok or resp.status_code not in _RETRY_STATUS:
                return resp
            last_response = resp
            print(
                f"[llm-retry/{provider_label}] attempt {attempt + 1} "
                f"got HTTP {resp.status_code}, retrying..."
            )

        # If we have more attempts, sleep with backoff.
        if attempt < len(_BACKOFFS):
            time.sleep(_BACKOFFS[attempt])

    # Exhausted attempts.
    if last_response is not None:
        # Last attempt was a retryable status — return it so the caller can
        # surface the real status/body to the user instead of a vague "failed".
        return last_response
    # All attempts raised at the network level.
    raise last_exception or RuntimeError(
        f"{provider_label}: all {len(_BACKOFFS) + 1} attempts failed with no response"
    )
