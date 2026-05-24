"""
Regression cases for _prevalidate_action + structured ValvoActionError.

Every entry in this file corresponds to a real bug that reached a user.
The point isn't to prove the code works once — it's to freeze the
structured-error shape so the LLM's branch-on-error_code behavior
stays intact as future PRs touch these paths.

Run: `python -m unittest Backend.tests.test_action_prevalidation`
     from the repo root.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BACKEND_ROOT = os.path.join(REPO_ROOT, "Backend")
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from Backend.tests._harness import MockCursor, _install_stubs

_install_stubs()

# Import after stubs are installed so actions.py's top-level imports resolve.
from services.valvo_ai_v2.actions import (  # noqa: E402
    ValvoActionError,
    _prevalidate_action,
)


def _pos(**overrides):
    """Canned active-position row. Override only the fields a test cares about."""
    base = {
        "id": 1,
        "stock_name": "Ather Energy",
        "security_id": "SEC123",
        "entry_price": 900.0,
        "stop_loss": 864.0,
        "quantity": 5,
        "current_price": 932.85,
        "bucket_sold_pct": 0,
        "bucket_cap": 66,
        "sell_history": [],
        "risk_pct": 4.0,
    }
    base.update(overrides)
    return base


class PrevalidateLogTradeTests(unittest.TestCase):
    """`add ather energy N shares at cmp` when Ather Energy is already held."""

    def test_duplicate_redirects_to_pyramid_with_mapped_payload(self):
        cur = MockCursor(rowsets=[[_pos()]])  # _find_active_position finds it
        payload = {
            "stock_name": "Ather Energy",
            "entry_price": 932.85,
            "quantity": 2,
        }
        with self.assertRaises(ValvoActionError) as ctx:
            _prevalidate_action(cur, "log_trade", payload)
        err = ctx.exception
        self.assertEqual(err.error_code, "stock_already_held")
        self.assertEqual(err.suggested_action, "pyramid_position")
        # Payload rewritten with pyramid field names so the LLM can retry cleanly.
        self.assertEqual(err.suggested_payload["stock_name"], "Ather Energy")
        self.assertEqual(err.suggested_payload["add_qty"], 2)
        self.assertEqual(err.suggested_payload["add_price"], 932.85)


class PrevalidatePyramidTests(unittest.TestCase):
    """Pyramid against a stock that isn't in the book."""

    def test_missing_stock_returns_no_active_position_with_log_hint(self):
        # _find_active_position returns [] (no rows), then triggers the
        # "No active position found" branch.
        cur = MockCursor(rowsets=[[]])
        with self.assertRaises(ValvoActionError) as ctx:
            _prevalidate_action(
                cur,
                "pyramid_position",
                {"stock_name": "GHOSTCO", "add_qty": 1, "add_price": 100.0},
            )
        err = ctx.exception
        self.assertEqual(err.error_code, "no_active_position")
        # Pyramid → log_trade suggestion lets the LLM offer creation gracefully.
        self.assertEqual(err.suggested_action, "log_trade")

    def test_ambiguous_match_raises_ambiguous_code(self):
        # Two non-exact-match positions — _find_active_position returns
        # an "Ambiguous" error string. Prevalidate maps that to its own code.
        cur = MockCursor(rowsets=[[
            _pos(stock_name="Aether Corp"),
            _pos(stock_name="Aether Ltd"),
        ]])
        with self.assertRaises(ValvoActionError) as ctx:
            _prevalidate_action(
                cur,
                "pyramid_position",
                {"stock_name": "Aether", "add_qty": 1, "add_price": 100.0},
            )
        self.assertEqual(ctx.exception.error_code, "ambiguous_stock_reference")


class ErrorShapeTests(unittest.TestCase):
    """ValvoActionError.to_dict is the wire contract the LLM branches on.
    Freeze its shape so downstream prompt rules keep working."""

    def test_to_dict_contains_error_code_and_suggested_action(self):
        err = ValvoActionError(
            "X is already an active position.",
            error_code="stock_already_held",
            suggested_action="pyramid_position",
            suggested_payload={"stock_name": "X", "add_qty": 1, "add_price": 100},
        )
        d = err.to_dict()
        self.assertFalse(d["ok"])
        self.assertEqual(d["error_code"], "stock_already_held")
        self.assertEqual(d["suggested_action"], "pyramid_position")
        self.assertIn("suggested_payload", d)


if __name__ == "__main__":
    unittest.main()
