"""
Regression tests for the deterministic portfolio oracle.

The oracle bypasses the LLM for portfolio math questions. These tests
freeze:
  - intent matching (which messages route to which handler)
  - handler output (P&L numbers, positional answers, empty-book refusals)
so a future "tighten the regex" PR can't silently break the bypass and
let the LLM start hallucinating again.

Run: `python -m unittest Backend.tests.test_portfolio_oracle`
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BACKEND_ROOT = os.path.join(REPO_ROOT, "Backend")
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from Backend.tests._harness import _install_stubs

_install_stubs()

from services.valvo_ai_v5 import portfolio_oracle as oracle  # noqa: E402


def _pos(stock_name, qty, entry, cmp_v, sl=None):
    """Test-only constructor for _Pos so cases stay readable."""
    return oracle._Pos(
        stock_name=stock_name,
        quantity=qty,
        entry_price=entry,
        current_price=cmp_v,
        stop_loss=sl if sl is not None else entry * 0.96,
        pl=(cmp_v - entry) * qty,
        pl_pct=round((cmp_v - entry) / entry * 100, 2) if entry else 0,
        value=cmp_v * qty,
        cost=entry * qty,
    )


# Fixture: the exact 7-position portfolio from the production failure
# the oracle was built to fix.
SEVEN_POSITIONS = [
    _pos("Anlon Healthcare", 30, 15.87, 15.87),
    _pos("Balrampur Chini Mills", 3, 549.50, 540.30),
    _pos("Ather Energy", 4, 938.60, 898.45),
    _pos("Gallantt Ispat", 10, 864.00, 842.50),
    _pos("Garden Reach Shipbuilders", 5, 2667.81, 2879.60),
    _pos("Ramco Industries", 24, 273.16, 269.56),
    _pos("NBCC", 50, 93.15, 93.31),
]


def _call(message, positions=None):
    """Run try_oracle with positions injected via patch — bypasses DB."""
    pos = SEVEN_POSITIONS if positions is None else positions
    with patch.object(oracle, "_fetch_positions", return_value=pos):
        return oracle.try_oracle(message, "test-user")


class IntentMatchTests(unittest.TestCase):
    """Confirm each intent regex catches the natural phrasings users
    actually type, AND rejects the ones it shouldn't."""

    def test_total_pl_phrasings(self):
        for msg in [
            "what is my total p&l",
            "what is my total pnl",
            "show me my unrealized P&L",
            "my total profit and loss",
            "total return on my portfolio",
            "what is my net p&l?",
            "how am i doing",
            "how is my portfolio doing",
        ]:
            with self.subTest(msg=msg):
                self.assertIsNotNone(_call(msg), f"Should match: {msg!r}")

    def test_total_value_phrasings(self):
        for msg in [
            "what is my total portfolio value",
            "total value",
            "what is my book value",
            "portfolio value?",
        ]:
            with self.subTest(msg=msg):
                self.assertIsNotNone(_call(msg), f"Should match: {msg!r}")

    def test_show_positions_phrasings(self):
        for msg in [
            "show my positions",
            "show me my full portfolio",
            "list my active positions",
            "show me my positions",
            "what are my positions?",
            "what are my active holdings?",
        ]:
            with self.subTest(msg=msg):
                self.assertIsNotNone(_call(msg), f"Should match: {msg!r}")

    def test_per_stock_pl_phrasings(self):
        # All these reference stocks that ARE in the fixture portfolio,
        # so the oracle should answer with their P&L.
        for msg in [
            "p&l on Garden Reach Shipbuilders",
            "profit on NBCC",
            "loss on Ather Energy",
            "how much am i up on Garden Reach",
            "how much have i made on Garden Reach",
        ]:
            with self.subTest(msg=msg):
                self.assertIsNotNone(_call(msg), f"Should match: {msg!r}")

    def test_per_stock_pl_falls_through_when_not_held(self):
        # Intent matches, but the user doesn't hold TCS — handler returns
        # None so the LLM can answer ("you don't hold TCS"). This is the
        # correct fall-through behaviour, not a regex miss.
        self.assertIsNone(_call("how much p&l on TCS"))

    def test_biggest_position_phrasings(self):
        for msg in ["biggest position", "show me my largest holding", "biggest stock"]:
            with self.subTest(msg=msg):
                self.assertIsNotNone(_call(msg), f"Should match: {msg!r}")

    def test_winners_losers_phrasings(self):
        self.assertIsNotNone(_call("which of these are in profit?"))
        self.assertIsNotNone(_call("which positions are losing?"))
        self.assertIsNotNone(_call("show my winners"))
        self.assertIsNotNone(_call("any losers?"))

    def test_does_not_match_action_verbs(self):
        # Write actions must reach the LLM so the action tools fire.
        for msg in [
            "buy 10 TCS at 3500",
            "add 5 more Ather at cmp",
            "sell half of Garden Reach",
            "set stop loss on TCS to 3400",
            "close my Anlon position",
            "trail my SL with EMA20",
            "compare TCS and INFY",
            "explain why my pnl is down",
        ]:
            with self.subTest(msg=msg):
                self.assertIsNone(_call(msg), f"Must NOT match: {msg!r}")

    def test_does_not_match_compound_questions(self):
        msg = "what is my total p&l? and what is my biggest position?"
        self.assertIsNone(_call(msg))

    def test_does_not_match_without_user(self):
        with patch.object(oracle, "_fetch_positions", return_value=SEVEN_POSITIONS):
            self.assertIsNone(oracle.try_oracle("what is my total p&l", None))


class HandlerOutputTests(unittest.TestCase):
    """Confirm each handler computes correctly from fixture data."""

    def test_total_pl_uses_correct_sum(self):
        # Sum: 0 + (-27.60) + (-160.60) + (-215.00) + 1058.95 + (-86.40) + 8.00 = +577.35
        result = _call("what is my total p&l")
        self.assertIn("+577.35", result)
        # The historical wrong answer must NEVER appear.
        self.assertNotIn("-1,273", result)
        self.assertNotIn("-1273", result)

    def test_per_stock_pl_finds_garden_reach(self):
        result = _call("how much p&l on Garden Reach Shipbuilders")
        self.assertIn("Garden Reach", result)
        self.assertIn("+1,058.95", result)

    def test_biggest_position_picks_garden_reach(self):
        # Garden Reach value = 5 × 2879.60 = 14,398 (largest in fixture)
        result = _call("biggest position")
        self.assertIn("Garden Reach", result)
        self.assertIn("14,398", result)

    def test_winners_lists_only_profitable(self):
        result = _call("which of these are in profit")
        # Garden Reach (+1058.95) and NBCC (+8.00) are the only winners.
        self.assertIn("Garden Reach", result)
        self.assertIn("NBCC", result)
        # Losers must not appear in the winners table.
        self.assertNotIn("Ather Energy", result)
        self.assertNotIn("Balrampur", result)


class EmptyBookTests(unittest.TestCase):
    """The exact failure mode that was fabricating RELIANCE/TCS portfolios.
    Empty book MUST yield a deterministic refusal — never a None that
    falls through to the LLM (the LLM was the one inventing data)."""

    def test_total_pl_on_empty_book_refuses(self):
        result = _call("what is my total p&l", positions=[])
        self.assertIsNotNone(result)
        # Accept either of the two natural phrasings the handler emits.
        self.assertTrue(
            "don't have any active positions" in result.lower()
            or "no active positions" in result.lower(),
            f"unexpected refusal text: {result!r}",
        )

    def test_show_positions_on_empty_book_refuses(self):
        result = _call("show my positions", positions=[])
        self.assertIsNotNone(result)
        # Must NEVER mention any of the fabricated tickers from the
        # original production bug.
        for ticker in ["RELIANCE", "TCS", "HINDALCO", "JSWSTEEL", "NALCO", "INFY"]:
            self.assertNotIn(ticker, result, f"Empty-book answer must not mention {ticker}")


if __name__ == "__main__":
    unittest.main()
