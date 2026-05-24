"""
portfolio_oracle.py — deterministic answers for portfolio math questions.

LLMs are stochastic about arithmetic. Three production failures forced
this module:
  1. "what is my total P&L" → -Rs1,273.74 (actual +Rs577.35; off by Rs1,851)
  2. "how much P&L on Garden Reach" → "I don't have the P&L"
  3. "what is my unrealized p&l" → fabricated portfolio of RELIANCE/TCS/...

For pure-math / lookup questions about positions, we don't need an LLM.
We have the data, we know the formulas. This module:
  - Pattern-matches the user's message against well-known intents.
  - On a match: pulls live positions, computes the answer in Python,
    formats a clean markdown response, and returns it.
  - On a miss: returns None — the engine falls through to the normal
    LLM path.

This bypasses every category of LLM failure for these specific
questions: arithmetic errors, fabrication, refusals, preflight reads.
The cost is ~5ms of pattern matching per turn vs. an LLM round trip.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


# ───────────────────────────────────────────────────────────────────────
# DB helpers — keep self-contained so this module imports cheaply at
# engine boot.
# ───────────────────────────────────────────────────────────────────────

@dataclass
class _Pos:
    stock_name: str
    quantity: int
    entry_price: float
    current_price: float
    stop_loss: float
    pl: float
    pl_pct: float
    value: float
    cost: float


def _fetch_positions(user_id: str) -> list[_Pos]:
    """Pull active positions in the same shape the LIVE STATE block uses,
    plus precomputed per-row metrics. One query, no LLM, no caching —
    we want fresh CMP every turn."""
    from database.database import close_db, get_db

    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT stock_name, quantity, entry_price, current_price, stop_loss,
                   trailing_sl
            FROM positions
            WHERE user_id = %s AND status = 'active'
            ORDER BY stock_name ASC
            """,
            (user_id,),
        )
        out: list[_Pos] = []
        for r in cur.fetchall() or []:
            qty = int(r.get("quantity") or 0)
            entry = float(r.get("entry_price") or 0)
            cmp_v = float(r.get("current_price") or entry)
            sl = float(r.get("trailing_sl") or r.get("stop_loss") or 0)
            value = cmp_v * qty
            cost = entry * qty
            pl = value - cost
            pl_pct = (pl / cost * 100) if cost else 0
            out.append(_Pos(
                stock_name=r["stock_name"],
                quantity=qty,
                entry_price=entry,
                current_price=cmp_v,
                stop_loss=sl,
                pl=pl,
                pl_pct=round(pl_pct, 2),
                value=value,
                cost=cost,
            ))
        return out
    except Exception as exc:
        print(f"[portfolio_oracle] fetch failed: {exc}")
        return []
    finally:
        close_db(conn)


def _agg(positions: list[_Pos]) -> dict:
    total_value = sum(p.value for p in positions)
    total_cost = sum(p.cost for p in positions)
    total_pl = sum(p.pl for p in positions)
    total_pl_pct = (total_pl / total_cost * 100) if total_cost else 0
    return {
        "value": round(total_value, 2),
        "cost": round(total_cost, 2),
        "pl": round(total_pl, 2),
        "pl_pct": round(total_pl_pct, 2),
        "count": len(positions),
    }


# ───────────────────────────────────────────────────────────────────────
# Formatters — Indian rupee numbers with sign, 2dp.
# ───────────────────────────────────────────────────────────────────────

def _rs(n: float, signed: bool = False) -> str:
    fmt = f"{n:+,.2f}" if signed else f"{n:,.2f}"
    return f"Rs{fmt}"


def _pct(n: float) -> str:
    return f"{n:+.2f}%"


# ───────────────────────────────────────────────────────────────────────
# Handlers — each takes (positions, totals, message) and returns a
# markdown string OR None if the handler decides not to answer (e.g.
# stock not found in a per-stock lookup).
# ───────────────────────────────────────────────────────────────────────

def _h_total_pl(positions, totals, msg):
    if not positions:
        return "You don't have any active positions right now."
    return (
        f"**Total unrealized P&L:** {_rs(totals['pl'], signed=True)} "
        f"({_pct(totals['pl_pct'])} on a {_rs(totals['cost'])} cost basis).\n\n"
        f"Across {totals['count']} active positions, total value is "
        f"{_rs(totals['value'])}."
    )


def _h_total_value(positions, totals, msg):
    if not positions:
        return "You don't have any active positions right now."
    return (
        f"**Total portfolio value:** {_rs(totals['value'])} across "
        f"{totals['count']} active positions. "
        f"P&L on cost: {_rs(totals['pl'], signed=True)} ({_pct(totals['pl_pct'])})."
    )


def _h_biggest(positions, totals, msg):
    if not positions:
        return "You don't have any active positions right now."
    p = max(positions, key=lambda x: x.value)
    return (
        f"**Biggest position:** {p.stock_name} at {_rs(p.value)} "
        f"({p.quantity} shares × {_rs(p.current_price)} CMP). "
        f"P&L: {_rs(p.pl, signed=True)} ({_pct(p.pl_pct)})."
    )


def _h_smallest(positions, totals, msg):
    if not positions:
        return "You don't have any active positions right now."
    p = min(positions, key=lambda x: x.value)
    return (
        f"**Smallest position:** {p.stock_name} at {_rs(p.value)} "
        f"({p.quantity} shares × {_rs(p.current_price)} CMP). "
        f"P&L: {_rs(p.pl, signed=True)} ({_pct(p.pl_pct)})."
    )


def _h_best_performer(positions, totals, msg):
    if not positions:
        return "You don't have any active positions right now."
    p = max(positions, key=lambda x: x.pl_pct)
    return (
        f"**Best performer:** {p.stock_name} at {_pct(p.pl_pct)} "
        f"({_rs(p.pl, signed=True)} on {p.quantity} shares — "
        f"entry {_rs(p.entry_price)}, CMP {_rs(p.current_price)})."
    )


def _h_worst_performer(positions, totals, msg):
    if not positions:
        return "You don't have any active positions right now."
    p = min(positions, key=lambda x: x.pl_pct)
    return (
        f"**Worst performer:** {p.stock_name} at {_pct(p.pl_pct)} "
        f"({_rs(p.pl, signed=True)} on {p.quantity} shares — "
        f"entry {_rs(p.entry_price)}, CMP {_rs(p.current_price)})."
    )


def _h_winners(positions, totals, msg):
    if not positions:
        return "You don't have any active positions right now."
    winners = sorted([p for p in positions if p.pl > 0], key=lambda x: -x.pl_pct)
    if not winners:
        return "None of your active positions are in profit right now."
    lines = [f"**{len(winners)} of {len(positions)} positions in profit:**", ""]
    lines.append("| Stock | P&L | % |")
    lines.append("|---|---:|---:|")
    for p in winners:
        lines.append(f"| {p.stock_name} | {_rs(p.pl, signed=True)} | {_pct(p.pl_pct)} |")
    return "\n".join(lines)


def _h_losers(positions, totals, msg):
    if not positions:
        return "You don't have any active positions right now."
    losers = sorted([p for p in positions if p.pl < 0], key=lambda x: x.pl_pct)
    if not losers:
        return "None of your active positions are in loss right now."
    lines = [f"**{len(losers)} of {len(positions)} positions in loss:**", ""]
    lines.append("| Stock | P&L | % |")
    lines.append("|---|---:|---:|")
    for p in losers:
        lines.append(f"| {p.stock_name} | {_rs(p.pl, signed=True)} | {_pct(p.pl_pct)} |")
    return "\n".join(lines)


def _h_show_positions(positions, totals, msg):
    if not positions:
        return "You don't have any active positions right now."
    lines = [
        f"**{totals['count']} active positions** — total value "
        f"{_rs(totals['value'])}, P&L {_rs(totals['pl'], signed=True)} "
        f"({_pct(totals['pl_pct'])} on cost).",
        "",
        "| Stock | Qty | Entry | CMP | SL | Value | P&L | % |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for p in sorted(positions, key=lambda x: -x.value):
        lines.append(
            f"| {p.stock_name} | {p.quantity} | {p.entry_price:,.2f} | "
            f"{p.current_price:,.2f} | {p.stop_loss:,.2f} | "
            f"{_rs(p.value)} | {_rs(p.pl, signed=True)} | {_pct(p.pl_pct)} |"
        )
    return "\n".join(lines)


def _h_pl_for_stock(positions, totals, msg):
    """User asked about a specific stock — match by substring."""
    if not positions:
        return None  # Let the LLM say "you don't hold that"
    msg_lower = msg.lower()
    # Find the longest stock_name token that appears in the message.
    best = None
    for p in positions:
        # Match either the full name or any word ≥3 chars from it.
        name = p.stock_name.lower()
        if name in msg_lower:
            if best is None or len(p.stock_name) > len(best.stock_name):
                best = p
            continue
        for word in re.split(r"[^A-Za-z]+", p.stock_name):
            if len(word) >= 3 and word.lower() in msg_lower:
                if best is None or len(p.stock_name) > len(best.stock_name):
                    best = p
                break
    if not best:
        return None  # Falls through to LLM (probably a stock they don't hold)
    return (
        f"**{best.stock_name}** — P&L {_rs(best.pl, signed=True)} ({_pct(best.pl_pct)}).\n\n"
        f"{best.quantity} shares · entry {_rs(best.entry_price)} · "
        f"CMP {_rs(best.current_price)} · SL {_rs(best.stop_loss)} · "
        f"position value {_rs(best.value)}."
    )


# ───────────────────────────────────────────────────────────────────────
# Intent registry — order matters, more specific first. Patterns are
# permissive (case-insensitive substrings) since users phrase the same
# intent dozens of ways.
# ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Intent:
    name: str
    pattern: re.Pattern
    handler: Callable
    needs_per_stock: bool = False


def _re(s: str) -> re.Pattern:
    return re.compile(s, re.IGNORECASE)


# Verbs that imply a write or analysis the LLM should handle.
_NON_LOOKUP_VERBS = re.compile(
    r"\b(?:add|buy|sell|pyramid|create|close|trail|update|set|alert|score|chart|"
    r"compare|why|explain|should|recommend|analyze|analyse|backtest)\b",
    re.IGNORECASE,
)


_INTENTS: list[_Intent] = [
    # Per-stock P&L — runs first so it can short-circuit a phrase like
    # "p&l on tcs" before generic total-p&l matches.
    _Intent(
        name="pl_for_stock",
        pattern=_re(r"(?:p\s*&\s*l|pnl|profit|gain|loss)\s+(?:on|for|in)\s+\w+"),
        handler=_h_pl_for_stock,
        needs_per_stock=True,
    ),
    _Intent(
        name="how_much_on_stock",
        # Allow optional "am i / is / have i" between "much" and the
        # P&L verb so phrasings like "how much am i up on TCS" or
        # "how much have i made on Garden Reach" still route here.
        pattern=_re(
            r"how\s+much\s+"
            r"(?:(?:am\s+i|have\s+i|is|do\s+i)\s+)?"
            r"(?:p\s*&\s*l|pnl|profit|loss|up|down|gain|made|earned|lost)\s+"
            r"(?:on|for|in)\s+\w+"
        ),
        handler=_h_pl_for_stock,
        needs_per_stock=True,
    ),

    # Show / list positions
    _Intent(
        name="show_positions",
        pattern=_re(r"^\s*(?:show|list|display|view|see|tell\s+me)\s+(?:me\s+)?(?:my\s+)?(?:full\s+|all\s+|active\s+)?(?:portfolio|positions?|holdings?|book)\s*[?.!]*\s*$"),
        handler=_h_show_positions,
    ),
    _Intent(
        name="what_are_positions",
        pattern=_re(r"^\s*what\s+are\s+my\s+(?:active\s+)?(?:positions?|holdings?)\s*[?.!]*\s*$"),
        handler=_h_show_positions,
    ),

    # Winners / losers
    _Intent(
        name="winners",
        pattern=_re(r"\b(?:winners?|in\s+profit|profitable|making\s+money|making\s+profit)\b"),
        handler=_h_winners,
    ),
    _Intent(
        name="losers",
        pattern=_re(r"\b(?:losers?|in\s+loss|losing|underwater|down)\b"),
        handler=_h_losers,
    ),

    # Best / worst performer
    _Intent(
        name="best_performer",
        pattern=_re(r"\b(?:best|top)\s+(?:performer|performing|stock|position|gainer)\b"),
        handler=_h_best_performer,
    ),
    _Intent(
        name="worst_performer",
        pattern=_re(r"\b(?:worst|bottom|laggard)\s+(?:performer|performing|stock|position|loser)\b"),
        handler=_h_worst_performer,
    ),

    # Biggest / smallest position
    _Intent(
        name="biggest",
        pattern=_re(r"\b(?:biggest|largest|highest|max(?:imum)?)\s+(?:position|holding|stock)\b"),
        handler=_h_biggest,
    ),
    _Intent(
        name="smallest",
        pattern=_re(r"\b(?:smallest|tiniest|min(?:imum)?|lowest)\s+(?:position|holding|stock)\b"),
        handler=_h_smallest,
    ),

    # Total value
    _Intent(
        name="total_value",
        pattern=_re(r"\b(?:total|portfolio|book)\s+(?:value|worth|exposure|invested)\b"),
        handler=_h_total_value,
    ),

    # Total P&L — broadest, runs last so per-stock matchers win first.
    _Intent(
        name="total_pl",
        pattern=_re(r"\b(?:total|net|overall|unrealized|portfolio)\s+(?:p\s*&\s*l|pnl|profit|loss|return|gain)\b"),
        handler=_h_total_pl,
    ),
    _Intent(
        name="my_pl",
        pattern=_re(r"\bmy\s+(?:p\s*&\s*l|pnl|unrealized|profit\s+(?:and|or)\s+loss)\b"),
        handler=_h_total_pl,
    ),
    _Intent(
        name="how_am_i_doing",
        pattern=_re(r"\bhow\s+am\s+i\s+doing\b|\bhow\s+is\s+my\s+(?:portfolio|book)\b"),
        handler=_h_total_pl,
    ),
]


def try_oracle(user_message: str, user_id: str | None) -> str | None:
    """Pre-LLM intent matcher. Returns a formatted answer if a portfolio
    intent matches; None otherwise (engine falls through to the LLM).

    Refuses to handle:
      - messages without an authenticated user
      - messages mentioning a write/analyze verb (let the LLM route to
        the right action tool)
      - messages with multiple question marks (probably compound)
    """
    if not user_id:
        return None
    msg = (user_message or "").strip()
    if not msg or len(msg) > 500:
        return None
    if _NON_LOOKUP_VERBS.search(msg):
        return None
    if msg.count("?") > 1:
        return None

    matched = next((i for i in _INTENTS if i.pattern.search(msg)), None)
    if not matched:
        return None

    # Pull data once. We *always* fetch — even on empty book — because
    # the empty case is exactly where the LLM was fabricating, and the
    # deterministic "you have no positions" line is what we want there.
    positions = _fetch_positions(user_id)
    totals = _agg(positions)

    answer = matched.handler(positions, totals, msg)
    if answer is None:
        # Handler decided not to answer (e.g. stock_name not held).
        return None
    # Mark deterministic so the engine can tag the response (debug only).
    return answer
