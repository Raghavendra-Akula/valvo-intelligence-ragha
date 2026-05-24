"""
Regression tests for the v3/v6 gateway providers and shared retry helper.

Every test here corresponds to a real bug we've fixed in production. If any
of these go red, the bug is back. Run via:

    cd Backend && python -m unittest tests.test_llm_gateway

Tests are self-contained — no DB, no network. Provider HTTP calls are
mocked via unittest.mock.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Make Backend/ importable when running from repo root or Backend/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set dummy keys before importing providers (they read env at construction).
os.environ.setdefault("MOONSHOT_API_KEY", "test-kimi-key")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")


# ─────────────────────────────────────────────────────────────────────────────
# Translator + normalizer tests — KimiProvider and GeminiProvider share the
# same code shape so we run the same suite against both.
# ─────────────────────────────────────────────────────────────────────────────

class _TranslatorTestMixin:
    """Common assertions; subclasses set self.provider in setUp."""

    provider_factory = None  # override in subclasses

    def setUp(self):
        self.provider = self.provider_factory()

    def test_assistant_text_only_translates_clean(self):
        msgs = [{"role": "assistant", "content": [{"type": "text", "text": "Hello."}]}]
        out = self.provider._translate_messages(msgs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "assistant")
        self.assertEqual(out[0]["content"], "Hello.")

    def test_assistant_with_tool_use_emits_tool_calls(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "Pulling data."},
            {"type": "tool_use", "id": "call_1", "name": "get_positions", "input": {"status": "active"}},
        ]}]
        out = self.provider._translate_messages(msgs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "assistant")
        self.assertEqual(out[0]["content"], "Pulling data.")
        self.assertEqual(len(out[0]["tool_calls"]), 1)
        tc = out[0]["tool_calls"][0]
        self.assertEqual(tc["id"], "call_1")
        self.assertEqual(tc["function"]["name"], "get_positions")
        self.assertEqual(json.loads(tc["function"]["arguments"]), {"status": "active"})

    def test_empty_assistant_turn_skipped(self):
        """Bug we hit: assistant turn with no text and no tool_use was sent
        as {role:assistant, content:null} — providers 400. Now skipped."""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": []},  # empty
            {"role": "user", "content": "follow up"},
        ]
        out = self.provider._translate_messages(msgs)
        # The empty assistant turn must NOT appear in the output.
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["content"], "hi")
        self.assertEqual(out[1]["content"], "follow up")

    def test_user_tool_result_becomes_tool_role(self):
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_xyz", "content": '{"count":0}'},
        ]}]
        out = self.provider._translate_messages(msgs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["role"], "tool")
        self.assertEqual(out[0]["tool_call_id"], "call_xyz")
        self.assertEqual(out[0]["content"], '{"count":0}')

    def test_tool_result_dict_content_stringified(self):
        """Backend tool executors sometimes return dicts. Translator must
        json.dumps them so OpenAI APIs can swallow the string content."""
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_x", "content": {"a": 1, "b": "two"}},
        ]}]
        out = self.provider._translate_messages(msgs)
        self.assertEqual(out[0]["role"], "tool")
        # content should be valid JSON, parsable back to the original dict.
        self.assertEqual(json.loads(out[0]["content"]), {"a": 1, "b": "two"})

    def test_normalize_regular_response(self):
        payload = {
            "choices": [{
                "message": {"role": "assistant", "content": "All good.", "tool_calls": None},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5},
        }
        norm = self.provider._normalize(payload)
        self.assertEqual(norm.text, "All good.")
        self.assertEqual(norm.stop_reason, "end_turn")
        self.assertEqual(norm.tool_calls, [])
        self.assertEqual(norm.input_tokens, 100)
        self.assertEqual(norm.output_tokens, 5)

    def test_normalize_response_with_tool_call(self):
        payload = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_42",
                        "type": "function",
                        "function": {"name": "search_stock", "arguments": '{"query":"INFY"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 200, "completion_tokens": 8},
        }
        norm = self.provider._normalize(payload)
        self.assertEqual(norm.text, "")
        self.assertEqual(norm.stop_reason, "tool_use")
        self.assertEqual(len(norm.tool_calls), 1)
        self.assertEqual(norm.tool_calls[0].name, "search_stock")
        self.assertEqual(norm.tool_calls[0].input, {"query": "INFY"})

    def test_normalize_handles_malformed_arguments(self):
        """If a model returns arguments as something other than valid JSON,
        we shouldn't crash — capture the raw value instead."""
        payload = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "x", "arguments": "not json {"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        norm = self.provider._normalize(payload)
        self.assertEqual(norm.tool_calls[0].input, {"_raw_arguments": "not json {"})

    def test_translate_tools_anthropic_to_openai(self):
        tools = [{
            "name": "get_positions",
            "description": "Fetch positions.",
            "input_schema": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
            },
        }]
        out = self.provider._translate_tools(tools)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "function")
        self.assertEqual(out[0]["function"]["name"], "get_positions")
        self.assertEqual(out[0]["function"]["parameters"]["required"], ["status"])

    def test_string_content_passthrough(self):
        msgs = [{"role": "user", "content": "plain string"}]
        out = self.provider._translate_messages(msgs)
        self.assertEqual(out, [{"role": "user", "content": "plain string"}])


class TestKimiProvider(_TranslatorTestMixin, unittest.TestCase):
    @staticmethod
    def provider_factory():
        from services.valvo_ai_v6.gateway import KimiProvider
        return KimiProvider()

    def test_kimi_reasoning_content_roundtrip(self):
        """K2.6 thinking mode needs reasoning_content echoed back on next
        round or it 400s. The neutral message format carries it as a
        {"type":"thinking"} block; KimiProvider re-emits it on the assistant
        message."""
        from services.valvo_ai_v6.gateway import KimiProvider, UnifiedResponse, ToolCall
        p = KimiProvider()

        # Simulate Kimi returning content with reasoning_content
        payload = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "Let me check positions first.",
                    "tool_calls": [{
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "get_positions", "arguments": "{}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 10},
        }
        norm = p._normalize(payload)
        self.assertEqual(norm.reasoning_content, "Let me check positions first.")

        # Round-trip: turn it into neutral content, then back to OpenAI shape
        neutral = norm.to_message_content()
        # Should include a thinking block before the tool_use
        thinking_blocks = [b for b in neutral if b.get("type") == "thinking"]
        self.assertEqual(len(thinking_blocks), 1)
        self.assertEqual(thinking_blocks[0]["text"], "Let me check positions first.")

        # Translate back — assistant message should carry reasoning_content
        translated = p._translate_messages([{"role": "assistant", "content": neutral}])
        self.assertEqual(translated[0].get("reasoning_content"),
                         "Let me check positions first.")


class TestGeminiProvider(_TranslatorTestMixin, unittest.TestCase):
    @staticmethod
    def provider_factory():
        from services.valvo_ai_v3.gateway import GeminiProvider
        return GeminiProvider()


# ─────────────────────────────────────────────────────────────────────────────
# Retry helper — must retry transient failures, must NOT retry client errors.
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryHelper(unittest.TestCase):
    def test_success_on_first_try(self):
        from services._llm_retry import post_with_retry

        def fake_post(*a, **kw):
            r = MagicMock()
            r.ok = True
            r.status_code = 200
            return r

        with patch("requests.post", side_effect=fake_post) as m:
            resp = post_with_retry("http://x", headers={}, json_body={}, provider_label="t")
            self.assertTrue(resp.ok)
            self.assertEqual(m.call_count, 1)

    def test_retry_on_503_then_succeeds(self):
        from services._llm_retry import post_with_retry

        attempts = {"n": 0}

        def fake_post(*a, **kw):
            attempts["n"] += 1
            r = MagicMock()
            if attempts["n"] == 1:
                r.ok = False
                r.status_code = 503
            else:
                r.ok = True
                r.status_code = 200
            return r

        with patch("requests.post", side_effect=fake_post), patch("time.sleep"):
            resp = post_with_retry("http://x", headers={}, json_body={}, provider_label="t")
            self.assertTrue(resp.ok)
            self.assertEqual(attempts["n"], 2)

    def test_no_retry_on_400(self):
        """400 = our request is broken. Retrying won't help and just delays
        the user from seeing the actual error message."""
        from services._llm_retry import post_with_retry

        attempts = {"n": 0}

        def fake_post(*a, **kw):
            attempts["n"] += 1
            r = MagicMock()
            r.ok = False
            r.status_code = 400
            return r

        with patch("requests.post", side_effect=fake_post), patch("time.sleep"):
            resp = post_with_retry("http://x", headers={}, json_body={}, provider_label="t")
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(attempts["n"], 1)

    def test_retry_on_connection_error(self):
        import requests as _req
        from services._llm_retry import post_with_retry

        attempts = {"n": 0}

        def fake_post(*a, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise _req.ConnectionError("network blip")
            r = MagicMock()
            r.ok = True
            r.status_code = 200
            return r

        with patch("requests.post", side_effect=fake_post), patch("time.sleep"):
            resp = post_with_retry("http://x", headers={}, json_body={}, provider_label="t")
            self.assertTrue(resp.ok)
            self.assertEqual(attempts["n"], 2)

    def test_all_attempts_fail_with_response_returned(self):
        """If every retry hits a 503, return the last 503 so the caller can
        surface a real error to the user (better than re-raising)."""
        from services._llm_retry import post_with_retry

        def fake_post(*a, **kw):
            r = MagicMock()
            r.ok = False
            r.status_code = 503
            return r

        with patch("requests.post", side_effect=fake_post), patch("time.sleep"):
            resp = post_with_retry("http://x", headers={}, json_body={}, provider_label="t")
            self.assertEqual(resp.status_code, 503)


# ─────────────────────────────────────────────────────────────────────────────
# Run via: python -m unittest tests.test_llm_gateway -v
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main()
