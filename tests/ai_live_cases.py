"""
Valvo AI live-eval cases — the scripted conversations the runner replays
against the real LLM gateway. Each case asserts which tool the model
picks and (loosely) which arguments, given a synthetic LIVE STATE
block.

Add a new case here whenever a bug surfaces in prod that *would have
been caught* by one scripted turn. Keep cases small and single-turn
when possible — multi-turn eval takes more tokens and is flakier.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalCase:
    name: str
    user_message: str
    # Positions to inject into the LIVE STATE block. Each dict needs at
    # least stock_name + quantity + entry_price; current_price and
    # stop_loss fall back to entry if missing.
    positions: list = field(default_factory=list)
    # Optional prior conversation (role = user | assistant). Useful for
    # continuity tests like "keep existing SL" after a pyramid prompt.
    history: list = field(default_factory=list)
    # Expected first tool the model calls. Set to None when the case is
    # expected to end_turn with a clarifying question instead of a tool.
    expected_tool: str | None = None
    # Subset of tool arguments we require. Numeric values tolerate ±2%.
    expected_args_subset: dict = field(default_factory=dict)
    # If set, the model's text answer must contain at least one of these
    # substrings (lower-cased match). Accepts a str or a list of strs —
    # use a list when the model can phrase the same intent multiple ways
    # (e.g. "which stock" vs "what stock"). Use when expected_tool is None.
    expected_text_contains: str | list[str] | None = None


def _pos(stock_name, quantity, entry_price, *, current_price=None, stop_loss=None):
    return {
        "stock_name": stock_name,
        "quantity": quantity,
        "entry_price": entry_price,
        "current_price": current_price if current_price is not None else entry_price,
        "stop_loss": stop_loss if stop_loss is not None else entry_price * 0.96,
    }


CASES: list[EvalCase] = [
    # ── Positive paths ────────────────────────────────────────────────
    EvalCase(
        name="new stock → log_trade",
        user_message="buy 10 TCS at 3500",
        expected_tool="log_trade",
        expected_args_subset={"stock_name": "TCS", "quantity": 10, "entry_price": 3500},
    ),
    EvalCase(
        # Same-day round trip — entry + exit in one shot. The AI must call
        # log_trade with both entry_price and exit_price set, NOT save the
        # exit as the stop-loss (the original BUG-3 from the EXIT≠SL fix).
        name="same-day round trip → log_trade with exit_price",
        user_message="bought NALCO at 220 and exited at 225 same day",
        expected_tool="log_trade",
        expected_args_subset={"stock_name": "NALCO", "entry_price": 220, "exit_price": 225},
    ),
    EvalCase(
        name="held stock + 'add' → pyramid_position",
        user_message="add 3 more Ather Energy at 932",
        positions=[_pos("Ather Energy", 5, 900, current_price=932)],
        expected_tool="pyramid_position",
        expected_args_subset={"stock_name": "Ather Energy", "add_qty": 3, "add_price": 932},
    ),
    EvalCase(
        name="held stock + 'buy more' → pyramid_position",
        user_message="buy 2 more TCS at cmp 3520",
        positions=[_pos("TCS", 10, 3500, current_price=3520)],
        expected_tool="pyramid_position",
        expected_args_subset={"stock_name": "TCS", "add_qty": 2},
    ),

    # ── This session's bugs: regressions that must not come back ─────
    EvalCase(
        # The NBCC hallucination: after a pending pyramid, the follow-up
        # "keep existing SL" used to resolve to a completely different
        # stock. With LIVE STATE + history, the AI must stay on Ather.
        name="pyramid follow-up with 'keep existing SL' stays on Ather",
        user_message="keep existing SL",
        positions=[_pos("Ather Energy", 5, 900, current_price=932, stop_loss=864)],
        history=[
            {"role": "user", "content": "add 3 more ather energy at cmp"},
            {"role": "assistant", "content": (
                "You already hold Ather Energy. I'm pyramiding into it — +3 shares "
                "at Rs932. What stop-loss do you want on this pyramid leg? A fixed "
                "level, trailing EMA, or keep the existing SL?"
            )},
        ],
        expected_tool="pyramid_position",
        expected_args_subset={"stock_name": "Ather Energy", "add_qty": 3},
    ),
    EvalCase(
        # User asks to update SL on a stock they hold — should be update_stop_loss,
        # never log_trade.
        name="update SL on held stock → update_stop_loss",
        user_message="trail TCS stop loss with 20 ema",
        positions=[_pos("TCS", 10, 3500, current_price=3620)],
        expected_tool="update_stop_loss",
        expected_args_subset={"stock_name": "TCS", "trailing_mode": "ema20"},
    ),
    EvalCase(
        name="partial sell on held stock → record_sell",
        user_message="sell half of my Ather Energy at 1000",
        positions=[_pos("Ather Energy", 8, 900, current_price=1000)],
        expected_tool="record_sell",
        expected_args_subset={"stock_name": "Ather Energy", "sell_pct": 50, "sell_price": 1000},
    ),
    EvalCase(
        name="close full position → close_position",
        user_message="close out my TCS at 3700",
        positions=[_pos("TCS", 10, 3500, current_price=3700)],
        expected_tool="close_position",
        expected_args_subset={"stock_name": "TCS"},
    ),

    # ── Clarification paths (expected_tool = None) ───────────────────
    EvalCase(
        # When the user gives no stock and the book is empty, the AI must
        # ask for the stock — NOT call a tool with made-up args. The
        # model can phrase the same clarifier multiple ways, so accept
        # any of the common forms.
        name="no stock context → clarifies with user",
        user_message="add 5 shares at cmp",
        positions=[],
        expected_tool=None,
        expected_text_contains=["which stock", "what stock", "name of the stock"],
    ),

    # ── Read-only paths ──────────────────────────────────────────────
    EvalCase(
        name="positions query → get_positions",
        user_message="what are my current positions?",
        positions=[_pos("TCS", 10, 3500)],
        expected_tool="get_positions",
    ),
    EvalCase(
        # Real failure mode: after listing positions, a pronoun-bearing
        # follow-up ("Which of these are in profit?") used to come back
        # empty because the empty-response retry dropped history. The
        # answer is computable from LIVE STATE alone (CMP vs entry).
        name="profit follow-up answers from LIVE STATE without a tool",
        user_message="which of these are in profit?",
        positions=[
            _pos("TCS", 10, 3500, current_price=3700),       # +5.7% — winner
            _pos("Ather Energy", 5, 940, current_price=898),  # -4.5% — loser
            _pos("NBCC", 50, 93.15, current_price=93.31),     # ~flat
        ],
        history=[
            {"role": "user", "content": "show me my full portfolio"},
            {"role": "assistant", "content": (
                "Here are your 3 active positions: TCS (qty 10, entry 3500, CMP 3700), "
                "Ather Energy (qty 5, entry 940, CMP 898), NBCC (qty 50, entry 93.15, "
                "CMP 93.31)."
            )},
        ],
        expected_tool=None,
        expected_text_contains=["tcs", "in profit", "profit"],
    ),
    EvalCase(
        # Real failure mode: AI said "I don't have the P&L" for Garden Reach
        # when entry / CMP / qty were all in LIVE STATE — it just refused
        # arithmetic. With pre-computed P&L on every row, the AI must
        # answer with a number, no tool call.
        name="P&L on a single position answered from LIVE STATE",
        user_message="how much P&L on Garden Reach Shipbuilders?",
        positions=[
            _pos("Garden Reach Shipbuilders", 5, 2667.81, current_price=2879.60),
        ],
        expected_tool=None,
        # P&L = (2879.60 - 2667.81) * 5 = 1058.95. Accept any reasonable
        # rendering of "1058" or "1,058" or just "profit".
        expected_text_contains=["1,058", "1058", "1059"],
    ),
    EvalCase(
        # Worst failure mode we've seen: when LIVE STATE failed to inject,
        # the AI fabricated a portfolio of RELIANCE / TCS / HINDALCO /
        # JSWSTEEL / NALCO / NATIONALUM / ATHER ENERGY (all tickers from
        # the prompt's few-shot examples) and reported a Rs12.74L total.
        # That data was 100% invented. With the empty book here, the AI
        # MUST tell the user there are no positions — never invent.
        name="empty portfolio → AI says 'no positions', does not invent",
        user_message="what is my unrealized p&l?",
        positions=[],
        expected_tool=None,
        expected_text_contains=["no active positions", "no positions", "don't have any", "no holdings"],
    ),
    EvalCase(
        # Real failure mode: with the same 7 active positions, Gemini
        # repeatedly summed the per-row P&L to "-1,273.74" when the
        # actual sum is +577.35. The fix is the pre-computed total in
        # the LIVE STATE header line. AI must copy +577.35 verbatim,
        # never re-sum the rows.
        name="total portfolio P&L copied from LIVE STATE header",
        user_message="what is my total P&L?",
        positions=[
            _pos("Anlon Healthcare", 30, 15.87, current_price=15.87),
            _pos("Balrampur Chini Mills", 3, 549.50, current_price=540.30),
            _pos("Ather Energy", 4, 938.60, current_price=898.45),
            _pos("Gallantt Ispat", 10, 864.00, current_price=842.50),
            _pos("Garden Reach Shipbuilders", 5, 2667.81, current_price=2879.60),
            _pos("Ramco Industries", 24, 273.16, current_price=269.56),
            _pos("NBCC", 50, 93.15, current_price=93.31),
        ],
        expected_tool=None,
        # Sum: 0 + (-27.60) + (-160.60) + (-215) + 1058.95 + (-86.40) + 8 = +577.35
        # Accept multiple legible renderings; reject any answer mentioning
        # "-1,273" or "-1273" since that's the historical wrong sum.
        expected_text_contains=["577", "+577", "1.47%", "+1.47"],
    ),
    EvalCase(
        name="regime query → get_market_regime",
        user_message="what is the current market regime?",
        expected_tool="get_market_regime",
    ),
]
