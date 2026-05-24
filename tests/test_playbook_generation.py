"""
Regression cases for the ActionDefinition contract → prompt generator.

If someone adds a new action or changes a contract, these tests assert
the playbook block still renders the expected actions and failure codes.
That's the safety net that keeps the system prompt from quietly losing
guidance when someone edits actions.py.

Run: `python -m unittest Backend.tests.test_playbook_generation` from
     the repo root.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BACKEND_ROOT = os.path.join(REPO_ROOT, "Backend")
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from Backend.tests._harness import _install_stubs

_install_stubs()

from services.valvo_ai_v2.actions import (  # noqa: E402
    ACTIONS,
    build_action_playbook_block,
)


class PlaybookGenerationTests(unittest.TestCase):
    def test_block_contains_the_four_contracted_actions(self):
        block = build_action_playbook_block()
        for action in ("log_trade", "pyramid_position",
                       "update_stop_loss", "record_sell"):
            self.assertIn(f"\n{action}:", block, f"Missing section for {action}")

    def test_log_trade_surfaces_stock_already_held_failure_code(self):
        block = build_action_playbook_block()
        # The error_code strings in the playbook are the exact codes the
        # structured-error path returns. If they drift apart, the LLM's
        # branch-on-code behavior breaks silently.
        self.assertIn("stock_already_held", block)
        self.assertIn("unresolvable_security", block)

    def test_pyramid_position_mentions_cap_and_no_active(self):
        block = build_action_playbook_block()
        self.assertIn("pyramid_cap_reached", block)
        self.assertIn("no_active_position", block)

    def test_actions_without_contracts_are_skipped_gracefully(self):
        # set_fy_base_capital has no preconditions/failures/examples
        # wired up today — the block should omit it instead of rendering
        # an empty header.
        block = build_action_playbook_block()
        self.assertNotIn("\nset_fy_base_capital:", block)

    def test_every_action_in_ACTIONS_dict_has_a_description(self):
        # Catch-all: if someone adds an action without a description,
        # the LLM has no idea what it's for. Cheap guard.
        for name, d in ACTIONS.items():
            self.assertTrue(d.description, f"{name} has no description")


if __name__ == "__main__":
    unittest.main()
