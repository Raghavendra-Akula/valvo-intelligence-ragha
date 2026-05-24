"""
eval_ai_live.py — Run the live tool-call eval against the real Gemini gateway.

Why: the offline unittest harness freezes the *logic* layer (structured
errors, prevalidation, playbook content). It can't catch "LLM picks the
wrong tool even though the prompt is correct" — that needs real model
calls. This script plugs scripted conversations straight into the
gateway and asserts the chosen tool + key args.

Scope: single-turn, one LLM call per case. No tool execution happens;
we only check which tool the model *would* call. That keeps the run
cheap (~$0.005 per case on Gemini Flash-Lite) and deterministic enough
to diff run-to-run.

Usage
─────
From the repo root, with DEEPSEEK_API_KEY in env:

    PYTHONPATH=Backend python3 Backend/scripts/eval_ai_live.py

Exit code 0 on full pass, 1 on any failure — wire into CI when ready.

Add a case
──────────
Edit Backend/tests/ai_live_cases.py — each EvalCase is a user_message
plus optional positions/history plus the expected tool + args. Keep
the case list to ≤30 entries so a full run stays under a minute and
a few cents.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.abspath(os.path.join(HERE, ".."))
REPO_ROOT = os.path.abspath(os.path.join(BACKEND_ROOT, ".."))
for p in (BACKEND_ROOT, REPO_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# Flask isn't required for the gateway, but prompts.py imports from it
# indirectly via the memory module. Stub it if it's not installed.
try:
    import flask  # noqa: F401
except ImportError:
    import types
    flask_stub = types.ModuleType("flask")

    class _G:
        user_id = None

    flask_stub.g = _G()
    sys.modules["flask"] = flask_stub


# ───────────────────────────────────────────────────────────────────────
# Synthetic LIVE STATE builder — mirrors prompts._build_live_state_block
# but driven by test fixtures instead of a DB query. Keeping it here (not
# importing the prod helper) means cases can assert behavior independent
# of whatever's actually in Supabase at eval time.
# ───────────────────────────────────────────────────────────────────────

def _fake_live_state(positions: list[dict]) -> str:
    if not positions:
        return (
            "LIVE STATE (ground truth — use this before calling get_positions):\n"
            "- Active positions: none\n"
            "Rule: if the user names a stock that matches an active position above, "
            "treat it as that exact position. Never substitute a different stock.\n"
        )
    lines = [
        "LIVE STATE (ground truth — use this before calling get_positions):",
        f"- Active positions ({len(positions)}):",
    ]
    for p in positions:
        entry = float(p["entry_price"])
        cmp_v = float(p.get("current_price") or entry)
        sl = float(p.get("stop_loss") or entry * 0.96)
        lines.append(
            f"  · {p['stock_name']}: qty {int(p['quantity'])}, "
            f"entry {entry:.2f}, CMP {cmp_v:.2f}, SL {sl:.2f} (custom)"
        )
    lines.append(
        "Rule: if the user names a stock that matches an active position above, "
        "treat it as that exact position. Never substitute a different stock."
    )
    return "\n".join(lines) + "\n"


def _build_eval_system_prompt(positions: list[dict]) -> str:
    """Re-use the real static prefix + playbook; splice in our fake state.

    We intentionally call build_system_prompt with user_id=None (so it
    emits an empty LIVE STATE) and then prepend our synthetic state
    instead. This way the prompt we exercise here is byte-identical to
    production except for the live state — which is the variable we want
    to control.
    """
    from services.valvo_ai_v7.prompts import build_system_prompt
    base = build_system_prompt(user_id=None)

    # build_system_prompt already inserted a (probably empty) live-state
    # block. Replace it with our fixture-driven one so the eval doesn't
    # depend on whatever's in the real DB.
    marker = "LIVE STATE"
    idx = base.find(marker)
    if idx < 0:
        return base + "\n" + _fake_live_state(positions)

    # Find the end of the original LIVE STATE block — it terminates at
    # a blank line or the next uppercase header ("ACTION PLAYBOOK" /
    # "CURRENT CONTEXT"). Replace up to whichever we hit first.
    tail_markers = ["ACTION PLAYBOOK", "CURRENT CONTEXT"]
    end = len(base)
    for m in tail_markers:
        p = base.find(m, idx)
        if p > 0:
            end = min(end, p)
    return base[:idx] + _fake_live_state(positions) + base[end:]


# ───────────────────────────────────────────────────────────────────────
# Runner
# ───────────────────────────────────────────────────────────────────────

@dataclass
class EvalResult:
    name: str
    passed: bool
    detail: str
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _args_match(expected: dict, actual: dict) -> tuple[bool, str]:
    """Subset match with ±2% tolerance for numeric values. LLMs round
    prices, so strict equality is too brittle."""
    for key, want in expected.items():
        got = actual.get(key)
        if isinstance(want, (int, float)) and isinstance(got, (int, float)):
            denom = max(abs(float(want)), 1.0)
            if abs(float(got) - float(want)) / denom > 0.02:
                return False, f"arg {key!r}: expected ~{want}, got {got}"
        else:
            if str(got).strip().lower() != str(want).strip().lower():
                return False, f"arg {key!r}: expected {want!r}, got {got!r}"
    return True, ""


def _run_case(case, gateway, tools) -> EvalResult:
    system = _build_eval_system_prompt(case.positions or [])
    messages = list(case.history or []) + [
        {"role": "user", "content": case.user_message}
    ]

    t0 = time.time()
    try:
        resp = gateway.create_message(
            model=None,
            max_tokens=2000,
            system=system,
            messages=messages,
            tools=tools,
        )
    except Exception as exc:
        return EvalResult(case.name, False, f"gateway error: {exc}", int((time.time() - t0) * 1000))

    ms = int((time.time() - t0) * 1000)

    # Expected an answer (no tool call) — e.g. a clarifying question.
    if case.expected_tool is None:
        if resp.stop_reason == "tool_use":
            tn = resp.tool_calls[0].name if resp.tool_calls else "?"
            return EvalResult(case.name, False,
                              f"expected end_turn, got tool_use ({tn})",
                              ms, resp.input_tokens, resp.output_tokens)
        text = (resp.text or "").lower()
        want = case.expected_text_contains
        if want:
            # Accept a str (single required phrase) or list (any-of).
            alternatives = [want] if isinstance(want, str) else list(want)
            if not any(alt.lower() in text for alt in alternatives):
                return EvalResult(
                    case.name, False,
                    f"text missing any of {alternatives!r}: {text[:120]}",
                    ms, resp.input_tokens, resp.output_tokens,
                )
        return EvalResult(case.name, True, "ok (text)", ms, resp.input_tokens, resp.output_tokens)

    # Expected a tool call.
    if resp.stop_reason != "tool_use" or not resp.tool_calls:
        return EvalResult(case.name, False,
                          f"expected tool {case.expected_tool}, got end_turn (text: {resp.text[:120]!r})",
                          ms, resp.input_tokens, resp.output_tokens)

    tc = resp.tool_calls[0]
    if tc.name != case.expected_tool:
        return EvalResult(case.name, False,
                          f"expected tool {case.expected_tool}, got {tc.name}",
                          ms, resp.input_tokens, resp.output_tokens)

    ok, why = _args_match(case.expected_args_subset or {}, tc.input or {})
    if not ok:
        return EvalResult(case.name, False, why,
                          ms, resp.input_tokens, resp.output_tokens)

    return EvalResult(case.name, True, f"ok ({tc.name})",
                      ms, resp.input_tokens, resp.output_tokens)


def main() -> int:
    from services.valvo_ai_v7.gateway import ModelGateway
    from services.valvo_ai_v7.tools import get_all_tool_definitions
    from Backend.tests.ai_live_cases import CASES

    gateway = ModelGateway()
    if not gateway.available():
        print("ERROR: DeepSeek gateway not available — set DEEPSEEK_API_KEY env")
        return 2

    tools = get_all_tool_definitions()
    print(f"Running {len(CASES)} live eval cases against gateway…")
    print(f"Tools available: {len(tools)}\n")

    results: list[EvalResult] = []
    for case in CASES:
        r = _run_case(case, gateway, tools)
        results.append(r)
        flag = "PASS" if r.passed else "FAIL"
        print(
            f"  [{flag}] {r.name:60s}  {r.duration_ms:5d}ms  "
            f"in={r.input_tokens:5d} out={r.output_tokens:4d}"
        )
        if not r.passed:
            print(f"         → {r.detail}")

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    total_in = sum(r.input_tokens for r in results)
    total_out = sum(r.output_tokens for r in results)

    # Flash-Lite rough cost (rupee-equivalents): $0.10/M in, $0.40/M out.
    # Back-of-envelope only — real numbers depend on model + cache.
    est_cost_usd = (total_in / 1_000_000) * 0.10 + (total_out / 1_000_000) * 0.40

    print("\n" + "=" * 72)
    print(f"PASS: {passed}   FAIL: {failed}   TOTAL: {len(results)}")
    print(f"Tokens: in={total_in:,}  out={total_out:,}  est cost ≈ ${est_cost_usd:.4f}")
    print("=" * 72)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
