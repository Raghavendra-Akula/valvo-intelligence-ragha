"""
Valvo AI v4 -- Smart query classifier.

Zero LLM calls. <1 ms. Two jobs:

1. ``fast_path_intent(message)`` — the keyword-bias fast path (Step 1 redesign).
   Returns ``{intent, tier, confidence, ...}`` when a conservative unambiguous
   keyword is present. Returns ``None`` otherwise. The semantic router calls
   this first and only falls through to Flash Lite if we return None.
   Covers all 12 intents with hand-curated keyword lists.

2. ``classify_query(message)`` — legacy simple/complex classifier.
   Kept for backward compatibility. Used as the always-on fallback when the
   LLM router fails completely. Tier C reordering applied: analytical keywords
   now override the layer-1 simple patterns so "show me my best positions"
   is correctly labeled complex.
"""
from __future__ import annotations

import re

from .gateway import FLASH_MODEL, FLASH_LITE_MODEL


# ═══════════════════════════════════════════════════════════════════════════
#  Fast-path keyword lists (all 12 intents)
#
#  RULE: every keyword here must be UNAMBIGUOUS. If you'd reasonably expect
#  the word to appear in more than one intent's context, do NOT add it here —
#  let the LLM router handle it. False positives here = wrong specialist,
#  wrong answer, and the user never knows why.
# ═══════════════════════════════════════════════════════════════════════════

# Scoring — uses Valvo-specific vocabulary. Very low false-positive risk.
_SCORING_KEYWORDS = [
    "ferocity", "linearity", "5ma follower", "10ma follower", "20ma follower",
    "out of base", "out of the base", "large base", "large-base",
    "a+ grade", "a+ setup", "setup quality", "institutional participation",
    "extension from base", "extension from the base",
    "base breakout", "base depth", "setup grade",
    "score of", "scored ", "final score", "scoring",
    "rank these", "rank the", "rank my", "rank these stocks", "rank the stocks",
    "pick the best setup", "best setup among",
    "which setup", "setup type",
]

# Sector — "sector" word + a direction/quality modifier
_SECTOR_KEYWORDS = [
    "leading sector", "leading sectors", "sector strength",
    "best performing sector", "worst performing sector",
    "sector performance", "sector rank", "sector ranking",
    "which sectors are leading", "which sector is leading",
    "sectoral strength",
]

# Regime — narrow vocabulary; confusion-proof
_REGIME_KEYWORDS = [
    "market regime", "current regime", "regime history",
    "bull market", "bear market", "sideways market",
    "grind down", "sharp down", "market trend",
    "what regime", "which regime",
]

# Portfolio — clearly about current holdings. Actions are separated below.
_PORTFOLIO_KEYWORDS = [
    "show my positions", "show positions", "my positions",
    "active positions", "open positions", "current positions",
    "my holdings", "current holdings", "show holdings",
    "show my portfolio", "my portfolio", "current portfolio",
    "defensive status", "positions status",
]

# Trades — historical / closed / analytical
_TRADES_KEYWORDS = [
    "win rate", "win-rate", "winrate",
    "my trades", "closed trades", "past trades", "all my trades",
    "best trade", "worst trade", "best performing trade",
    "biggest winner", "biggest loser",
    "profit factor", "r multiple", "r-multiple",
    "trades in fy", "fy 20-21", "fy 21-22", "fy 22-23", "fy 23-24",
    "fy 24-25", "fy 25-26", "fy 26-27",
    "monthly pl", "monthly p&l", "monthly pnl",
    "top winners", "top losers", "top 5 winners", "top 10 winners",
    "top 5 losers", "top 10 losers", "top 20 winners",
]

# Fundamentals — ratios / financials. Very stable vocabulary.
_FUNDAMENTALS_KEYWORDS = [
    "roe", "roce", "debt to equity", "debt-to-equity",
    "shareholding pattern", "promoter holding", "promoter stake",
    "fii holding", "fii stake", "dii holding", "dii stake",
    "quarterly result", "quarterly results", "annual result", "annual results",
    "revenue growth", "profit growth", "eps growth",
    "interest coverage", "current ratio", "book value",
    "dividend per share", "dividend history",
    "cash flow", "free cashflow", "operating cashflow",
    "peer comparison", "industry peer",
]

# Market — price / technicals / 52w
_MARKET_KEYWORDS = [
    "price of ", "current price", "live price",
    "52 week high", "52w high", "52-week high",
    "52 week low", "52w low", "52-week low",
    "ath ", "all time high", "all-time high",
    "above ma50", "above ma200", "above 50 ma", "above 200 ma",
    "ma crossover", "ema crossover",
]

# Screener — scan / filter the universe
_SCREENER_KEYWORDS = [
    "screener", "scanner",
    "scan stocks", "screen stocks", "scan for",
    "breakout candidate", "breakout candidates",
    "near 52w high", "near 52-week high",
    "beaten down", "oversold",
]

# Watchlist — very narrow vocabulary
_WATCHLIST_KEYWORDS = [
    "watchlist", "watch list", "watchlists",
    "add to watchlist", "remove from watchlist",
]

# Journal — setup / rating / plan analysis
_JOURNAL_KEYWORDS = [
    "self rating", "self-rating", "my rating",
    "plan followed", "exit trigger",
    "growth area", "journal insight", "journal pattern",
    "entry type breakdown",
]

# Benchmark — portfolio vs index
_BENCHMARK_KEYWORDS = [
    "vs nifty", "versus nifty", "against nifty",
    "beat nifty", "beat the market", "beat the index",
    "alpha generation", "my alpha",
    "outperform nifty", "underperform nifty",
]

# Action — writes. Only strong forms; a bare "sell" is too ambiguous.
_ACTION_KEYWORDS = [
    "create position", "add position", "new position",
    "close position", "exit position", "square off",
    "record sell", "book profit",
    "update stoploss", "update sl", "change sl", "change stoploss",
    "delete position",
]

# Ordered: more specific first. When two intents would match, the first wins.
# Put narrower intents (with rarer vocab) before broader ones.
_FAST_PATH_INTENTS = [
    ("scoring", _SCORING_KEYWORDS),
    ("regime", _REGIME_KEYWORDS),
    ("sector", _SECTOR_KEYWORDS),
    ("fundamentals", _FUNDAMENTALS_KEYWORDS),
    ("action", _ACTION_KEYWORDS),
    ("watchlist", _WATCHLIST_KEYWORDS),
    ("screener", _SCREENER_KEYWORDS),
    ("journal", _JOURNAL_KEYWORDS),
    ("benchmark", _BENCHMARK_KEYWORDS),
    ("trades", _TRADES_KEYWORDS),
    ("portfolio", _PORTFOLIO_KEYWORDS),
    ("market", _MARKET_KEYWORDS),
]


# Tier heuristics — how to pick simple vs complex once we have an intent
_COMPLEX_SIGNAL = re.compile(
    r"\b(all|every|across|lifetime|historical|compare|comparison|versus|"
    r"rank|best|worst|top|bottom|biggest|largest|highest|lowest|"
    r"breakdown|distribution|trend|pattern|correlation|analyze|analysis)\b",
    re.IGNORECASE,
)


def fast_path_intent(message: str) -> dict | None:
    """
    Try to classify a message using only conservative keyword matching.

    Returns a dict with the SAME SHAPE as ``route_query()`` (tier, model_id,
    max_tokens, use_full_tools, intent, confidence, router) — or ``None``
    if no keyword matched. Caller treats ``None`` as "need the LLM".
    """
    if not message or not message.strip():
        return None

    msg = message.lower().strip()
    msg = re.sub(r"\s+", " ", msg)  # normalize whitespace

    for intent, keywords in _FAST_PATH_INTENTS:
        for kw in keywords:
            if kw in msg:
                tier = "complex" if _COMPLEX_SIGNAL.search(msg) else "simple"

                # Scoring "rank" queries are always complex — never simple
                if intent == "scoring" and ("rank " in msg or " pick " in msg):
                    tier = "complex"

                return {
                    "tier": tier,
                    "model_id": FLASH_MODEL if tier == "complex" else FLASH_LITE_MODEL,
                    "max_tokens": 8192 if tier == "complex" else 4096,
                    "use_full_tools": tier == "complex",
                    "intent": intent,
                    "confidence": "high",
                    "router": "fast_path",
                    "matched_keyword": kw,
                }
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Legacy simple-only classifier (always-on fallback).
#  Tier C reordering applied: complex signals checked FIRST.
# ═══════════════════════════════════════════════════════════════════════════

_SIMPLE_PATTERNS = [
    r"^(hi|hello|hey|good morning|good evening|thanks|thank you|ok|okay|bye)\b",
    r"^what can you do",
    r"^help\b",
    r"^(show|get|what).{0,20}(position|portfolio)s?\b",
    r"^(price|ltp|cmp|current price) (of|for)\b",
    r"^(show|check|get).{0,10}price",
    r"^how.{0,10}(doing|going)\b",
    r"^clear\b",
]

_SIMPLE_COMPILED = [re.compile(p, re.IGNORECASE) for p in _SIMPLE_PATTERNS]


_COMPLEX_KEYWORDS = [
    # Multi-year / cross-FY analysis
    "all fy", "all years", "every year", "across all", "across fy",
    "all time", "lifetime", "historical",
    # Comparison
    "compare", "comparison", "versus", " vs ", " vs. ",
    "against nifty", "beat nifty", "beat the market",
    "benchmark", "alpha", "outperform", "underperform",
    # Trend / pattern
    "trend", "pattern", "correlation", "consistency",
    "improving", "declining", "getting better", "getting worse",
    # Journal deep analysis
    "setup type", "entry type", "which setup", "which entry",
    "journal insight", "journal pattern", "self rating",
    "plan followed", "exit trigger", "growth area",
    # Scanner / screener
    "screener", "scanner", "scan stock", "screen stock",
    "52 week", "52w", "breakout candidate", "near high",
    "above ma200", "above ma50", "ma crossover",
    "beaten down", "oversold",
    # Watchlist
    "watchlist", "watch list",
    # Equity curve / drawdown
    "equity curve", "drawdown", "max drawdown", "recovery",
    "streak", "consecutive",
    # Aggregation
    "by sector", "by month", "by stock", "by symbol",
    "sector breakdown", "monthly breakdown", "sector wise",
    "group by", "breakdown",
    # Complex metrics
    "profit factor", "expectancy", "sharpe",
    "risk reward", "r multiple distribution",
    # Superlatives across time
    "best month", "worst month", "best trade", "worst trade",
    "biggest", "largest", "highest", "lowest",
    "top 5", "top 10", "top winners", "top losers",
    # Fundamentals (multi-stock or deep analysis)
    "fundamental", "financials", "balance sheet", "cash flow",
    "quarterly result", "annual result", "revenue growth",
    "debt to equity", "interest coverage", "shareholding pattern",
    "promoter holding", "fii holding", "dii holding",
    "peer comparison", "industry peer", "segment",
    "dividend history", "corporate action", "bonus", "stock split",
    # Ranking / analytical (Tier C — these now override layer-1)
    "best performing", "worst performing", "ranked",
]


_ANALYTICAL_WORDS = re.compile(
    r"\b(analyze|analysis|performance|breakdown|distribution|categorize|"
    r"summarize|summary|overview|deep dive|insight|split|segment|"
    r"which|why|how come|explain|reason|cause)\b",
    re.IGNORECASE,
)


_STOCK_SYMBOLS = {
    "reliance", "tcs", "infy", "infosys", "hdfcbank", "hdfc bank",
    "icicibank", "icici bank", "sbin", "state bank", "bajfinance",
    "bajaj finance", "bhartiartl", "airtel", "tatamotors", "tata motors",
    "itc", "wipro", "hcltech", "hcl tech", "adanient", "adani",
    "tatasteel", "tata steel", "maruti", "sunpharma", "sun pharma",
    "axisbank", "axis bank", "kotakbank", "kotak", "lt", "larsen",
    "ongc", "ntpc", "powergrid", "ultracemco", "titan", "nestleind",
    "asianpaint", "asian paints", "techm", "tech mahindra",
    "hindunilvr", "hul", "drreddy", "cipla", "coalindia", "coal india",
    "bpcl", "grasim", "jswsteel", "jsw steel", "m&m", "mahindra",
    "britannia", "divislab", "eicher", "heromotoco", "hero",
    "indusindbk", "indusind", "upl", "sbilife", "hdfclife",
    "apollohosp", "apollo", "tataconsum",
}


def _count_stock_mentions(msg: str) -> int:
    msg_lower = msg.lower()
    seen = set()
    for sym in _STOCK_SYMBOLS:
        if sym in msg_lower:
            seen.add(sym)
    return len(seen)


def classify_query(message: str) -> dict:
    """
    Legacy simple/complex classifier — used as always-on fallback when the
    LLM router crashes.

    Tier C reordering: complex signals checked FIRST. Fixes "show me my
    best positions" which previously matched layer-1 simple pattern before
    complex keywords got a chance.
    """
    msg = message.lower().strip()

    # Complex signals FIRST (Tier C fix)
    for keyword in _COMPLEX_KEYWORDS:
        if keyword in msg:
            return _complex()

    if _ANALYTICAL_WORDS.search(msg):
        return _complex()

    # Broad ranking/comparison words ("best", "worst", "top", "rank", etc.)
    # Catches "show me my best 5 positions" which none of the phrase lists
    # above cover.
    if _COMPLEX_SIGNAL.search(msg):
        return _complex()

    if _count_stock_mentions(msg) >= 2:
        return _complex()

    if len(re.findall(r"fy\s*\d{2}", msg)) > 1:
        return _complex()

    if len(msg) > 150:
        return _complex()

    if msg.count("?") > 1:
        return _complex()

    if re.search(r"\bever\s*\??$", msg):
        return _complex()

    # Simple patterns LAST (was FIRST in the old version)
    for pattern in _SIMPLE_COMPILED:
        if pattern.search(msg):
            return _simple()

    return _simple()


def _simple() -> dict:
    return {
        "tier": "simple",
        "model_id": FLASH_LITE_MODEL,
        "max_tokens": 4096,
        "use_full_tools": False,
    }


def _complex() -> dict:
    return {
        "tier": "complex",
        "model_id": FLASH_MODEL,
        "max_tokens": 8192,
        "use_full_tools": True,
    }
