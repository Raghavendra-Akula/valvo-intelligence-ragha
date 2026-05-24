"""
Regression cases for catalog.resolve_stock_reference_strict.

The Jain Irrigation Systems case (DVR + regular equity under identical
company_name) surfaced in the stock_universe audit — resolver silently
picked the shorter name, so log_trade would have bound a position
to the wrong security_id. Strict resolver now returns an ambiguity
signal instead, and _log_trade surfaces it as
ambiguous_stock_reference so the LLM asks the user by symbol.

Run: `python -m unittest Backend.tests.test_resolver_strict` from the
     repo root.
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

from services.valvo_ai_v2.catalog import resolve_stock_reference_strict  # noqa: E402


def _row(security_id, symbol, company_name):
    return {
        "security_id": security_id,
        "symbol": symbol,
        "company_name": company_name,
    }


class StrictResolverTests(unittest.TestCase):
    def test_exact_symbol_returns_single_row(self):
        # execute #1: exact symbol match → returns a row
        cur = MockCursor(rowsets=[[_row("SEC-TCS", "TCS", "Tata Consultancy Services Ltd")]])
        out = resolve_stock_reference_strict(cur, symbol_hint="TCS")
        self.assertIsNotNone(out)
        self.assertEqual(out.get("security_id"), "SEC-TCS")

    def test_single_fuzzy_match_returns_that_row(self):
        # execute #1: exact empty (no rows), execute #2: fuzzy returns 1
        cur = MockCursor(rowsets=[
            [],  # exact
            [_row("SEC-RIL", "RELIANCE", "Reliance Industries Ltd")],  # fuzzy
        ])
        out = resolve_stock_reference_strict(cur, symbol_hint="Reliance Industries")
        self.assertIsNotNone(out)
        self.assertEqual(out.get("symbol"), "RELIANCE")

    def test_dvr_vs_regular_prefers_regular_equity(self):
        # The Jain Irrigation case — JISLJALEQS (regular) and JISLDVREQS (DVR)
        # both match. We must pick the regular share silently; the DVR is
        # rarely what a retail user means.
        cur = MockCursor(rowsets=[
            [],  # exact match query returns nothing
            [
                _row("SEC-JISLJAL", "JISLJALEQS", "Jain Irrigation Systems"),
                _row("SEC-JISLDVR", "JISLDVREQS", "Jain Irrigation Systems"),
            ],
        ])
        out = resolve_stock_reference_strict(cur, symbol_hint="Jain Irrigation Systems")
        self.assertIsNotNone(out)
        self.assertFalse(out.get("ambiguous"), f"expected a single pick, got: {out}")
        self.assertEqual(out.get("symbol"), "JISLJALEQS")

    def test_exact_dvr_symbol_still_resolves_to_dvr(self):
        # If the user specifically passes the DVR symbol, the exact-match
        # path fires and skips the preference logic entirely.
        cur = MockCursor(rowsets=[
            [_row("SEC-JISLDVR", "JISLDVREQS", "Jain Irrigation Systems")],
        ])
        out = resolve_stock_reference_strict(cur, symbol_hint="JISLDVREQS")
        self.assertEqual(out.get("symbol"), "JISLDVREQS")

    def test_two_distinct_regular_matches_still_raises_ambiguous(self):
        # If two *regular-equity* listings both match the hint, the
        # preference can't break the tie and we must raise ambiguous.
        cur = MockCursor(rowsets=[
            [],  # exact empty
            [
                _row("SEC-A", "AETHER", "Aether Limited"),
                _row("SEC-B", "AETHR", "Aether Products Ltd"),
            ],
        ])
        out = resolve_stock_reference_strict(cur, symbol_hint="Aether")
        self.assertIsNotNone(out)
        self.assertTrue(out.get("ambiguous"))
        syms = {m.get("symbol") for m in out.get("matches") or []}
        self.assertEqual(syms, {"AETHER", "AETHR"})

    def test_no_match_returns_none(self):
        cur = MockCursor(rowsets=[[], []])  # exact empty, fuzzy empty
        out = resolve_stock_reference_strict(cur, symbol_hint="NonexistentCo")
        self.assertIsNone(out)

    def test_short_hint_skips_fuzzy(self):
        # Hints <3 chars would produce noisy ILIKE matches; the resolver
        # shortcircuits to None rather than returning thousands of rows.
        cur = MockCursor(rowsets=[[]])  # exact empty
        out = resolve_stock_reference_strict(cur, symbol_hint="A")
        self.assertIsNone(out)

    def test_empty_hint_returns_none(self):
        cur = MockCursor(rowsets=[])
        self.assertIsNone(resolve_stock_reference_strict(cur, symbol_hint=""))
        self.assertIsNone(resolve_stock_reference_strict(cur, symbol_hint=None))


if __name__ == "__main__":
    unittest.main()
