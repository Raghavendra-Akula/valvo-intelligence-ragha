"""
Valvo AI v4 -- Semantic Router (Step 1 redesign).

Three-stage cascade:
  1. Keyword fast-path (classifier.fast_path_intent)  — ~70 percent of queries.
     Zero LLM cost. Returns high-confidence intent.
  2. LLM router (Flash Lite)  — everything else.
     Sees last 3 user messages + prior intent for context.
     Returns intent + confidence (high/med/low) + candidates when low.
  3. Keyword classifier (always-on fallback)  — only if the LLM crashes.
     Returns tier + intent="general". NEVER crashes the engine.

Plus: LRU cache (from the parallel branch) still wraps the expensive path.
Cache key now includes prior_intent so follow-ups don't collide.

Fixes under this module (Step 1 issues):
  #1 scoring / sector / regime intents added
  #3 confidence + candidates returned
  #4 fast-path skips the LLM
  #5 last 3 user messages + prior intent threaded in
  #7 max_tokens bumped to 150 + lenient JSON parse
"""
from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict

from .gateway import FLASH_MODEL, FLASH_LITE_MODEL


# ═══════════════════════════════════════════════════════════════════════════
#  LLM router prompt  —  12 intents, confidence, candidates
# ═══════════════════════════════════════════════════════════════════════════

_ROUTER_SYSTEM = """\
You classify a trading-platform query into ONE category.

CATEGORIES:
- portfolio: current holdings, open positions, unrealized P&L, allocation.
- trades: the user's closed/realized trades, win rate, R-multiples, personal performance.
- fundamentals: listed-stock financials (PE, ROE, margins, EPS, revenue, debt, shareholding).
- market: listed-stock live price, OHLC, 52w high/low, technicals (MA, RSI, ATH).
- screener: filter or scan the whole stock universe by technical criteria.
- watchlist: the user's saved watchlist stocks.
- journal: the user's trade notes, setup tags, self-rating, lessons, plan-followed.
- benchmark: compare user performance to indices (Nifty, Sensex, sector indices).
- action: write operations (create position, record sell, update stoploss, close, delete).
- general: greetings, small talk, help / "what can you do".
- scoring: Valvo scoring engine — grade or rank setups using Valvo's rubric
  (ferocity, linearity, A+/A/B grades, extension, 5MA follower, out-of-base / large-base,
  sector strength as input). Three kinds of scoring queries all map here:
    (a) "what did X score" — lookup past scores from submissions table
    (b) "score X for me" — compute a new score
    (c) "rank these N names" / "pick best among N" — rank candidates
- sector: leading sectors, sector strength, best/worst performing sectors, sector ranking.
- regime: current market regime (bull / sideways / grind-down / sharp-down), regime history.

"COMPARE X AND Y" (most misrouted pattern):
- X and Y are the user's OWN trades / FYs / setups → trades (or journal if about notes).
- X and Y are two LISTED STOCKS, user wants ratios/financials → fundamentals.
- X and Y are two LISTED STOCKS, user wants price/returns → market.
- X and Y are two LISTED STOCKS, user wants SETUP QUALITY or to PICK ONE → scoring.
- One side is the user and the other is an index → benchmark.

COMPLEXITY:
- simple: single lookup, one entity, one metric.
- complex: multi-period, multi-entity comparison, aggregation, or ranking.

CONFIDENCE:
- high: you are sure — exactly one category fits.
- medium: one category is the best fit but another is plausible.
- low: two or more categories are equally plausible. In this case, list the
  top 2-3 in `candidates` (most likely first). We will ask the user to clarify.

OUTPUT ONLY JSON, no prose, no code fence:
{"intent":"<category>","complexity":"<simple|complex>","confidence":"<high|medium|low>","candidates":["<cat>","<cat>"]}

`candidates` may be empty when confidence is high/medium.
"""


# ═══════════════════════════════════════════════════════════════════════════
#  LRU cache (preserved from parallel branch, key extended to include
#  prior_intent so follow-ups like "and what about X" don't collide).
# ═══════════════════════════════════════════════════════════════════════════

_ROUTE_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_ROUTE_CACHE_MAX = 1000
_ROUTE_CACHE_LOCK = threading.Lock()

# Intent-carrying words — keep these during cache-key normalization
_ROUTE_KEYWORDS = {
    "trades", "trade", "positions", "position", "portfolio", "watchlist",
    "journal", "fundamentals", "fundamental", "screener", "scanner",
    "price", "ltp", "cmp", "compare", "versus", "vs", "nifty", "sensex",
    "benchmark", "setup", "rating", "sector", "win", "rate", "fy",
    "my", "i", "me", "last", "this", "year", "month", "all", "best",
    "worst", "top", "how", "what", "which", "show", "get", "find",
    "above", "below", "near", "52w", "52", "week", "ma200", "ma50",
    "breakout", "crossover", "roe", "roce", "debt", "eps", "revenue",
    "profit", "margin", "dividend", "promoter", "fii", "dii",
    "ferocity", "linearity", "extension", "regime", "bull", "bear",
    "sideways", "grind", "sharp", "leading", "rank", "pick", "score",
    "scored", "a+", "b+", "c+",
}


def _normalize_query(msg: str) -> str:
    msg = msg.lower().strip()
    msg = re.sub(r"\s+", " ", msg)
    if len(msg) > 120:
        return msg
    tokens = re.findall(r"[a-z0-9+]+|[^\sa-z0-9]", msg)
    normalized = []
    for tok in tokens:
        if tok.isdigit():
            normalized.append("N")
        elif tok.replace("+", "").isalnum() and tok not in _ROUTE_KEYWORDS and len(tok) >= 2:
            normalized.append("X")
        else:
            normalized.append(tok)
    return " ".join(normalized)


def _cache_key(norm_msg: str, prior_intent: str | None) -> str:
    """Cache key includes prior_intent so follow-ups route correctly."""
    return f"{prior_intent or 'none'}|{norm_msg}"


def _cache_get(key: str) -> dict | None:
    with _ROUTE_CACHE_LOCK:
        cached = _ROUTE_CACHE.get(key)
        if cached is not None:
            _ROUTE_CACHE.move_to_end(key)
            return dict(cached)
        return None


def _cache_put(key: str, value: dict) -> None:
    with _ROUTE_CACHE_LOCK:
        _ROUTE_CACHE[key] = dict(value)
        _ROUTE_CACHE.move_to_end(key)
        while len(_ROUTE_CACHE) > _ROUTE_CACHE_MAX:
            _ROUTE_CACHE.popitem(last=False)


# ═══════════════════════════════════════════════════════════════════════════
#  Gateway singleton
# ═══════════════════════════════════════════════════════════════════════════

_gateway = None


def _get_gateway():
    global _gateway
    if _gateway is None:
        from .gateway import GeminiFlashGateway
        _gateway = GeminiFlashGateway()
    return _gateway


# ═══════════════════════════════════════════════════════════════════════════
#  Lenient JSON extraction
# ═══════════════════════════════════════════════════════════════════════════

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    """Pull the first {...} block from arbitrary LLM output and parse it."""
    if not text:
        return None
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    # Try direct parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Try to find a {...} block
    match = _JSON_BLOCK.search(text)
    if match:
        try:
            return json.loads(match.group(0))
        except (json.JSONDecodeError, TypeError):
            return None
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Confidence → clarify decision
#
#  Rule (Tier B γ): ask only when candidates span DIFFERENT data sources.
#  Two intents that read similar tables (trades vs journal, market vs
#  screener) don't force a clarify — let the LLM continue and the tool loop
#  figure it out.
# ═══════════════════════════════════════════════════════════════════════════

# Groups of intents that read similar data. If candidates fall inside one
# group, no clarify needed. If they span groups, we ask.
#
# Scoring is its own group because it's a *different kind of question* —
# fundamentals/market show data, scoring grades/ranks setups. Users asking
# ambiguously between these genuinely want different answers.
_DATA_GROUPS = {
    "personal_trades": {"trades", "journal", "portfolio", "benchmark"},
    "stock_info":      {"market", "fundamentals", "screener"},
    "scoring":         {"scoring"},
    "market_context":  {"sector", "regime"},
    "user_data":       {"watchlist"},
    "chrome":          {"general", "action"},
}


def _group_of(intent: str) -> str:
    for grp, members in _DATA_GROUPS.items():
        if intent in members:
            return grp
    return "unknown"


def _should_clarify(intent: str, candidates: list[str]) -> bool:
    """Return True only when intent+candidates span more than one data group."""
    pool = [intent] + list(candidates or [])
    # Need at least 2 distinct non-empty entries
    pool = [p for p in pool if p]
    if len(pool) < 2:
        return False
    groups = {_group_of(p) for p in pool}
    return len(groups) >= 2


# ═══════════════════════════════════════════════════════════════════════════
#  Public entry point
# ═══════════════════════════════════════════════════════════════════════════

def route_query(
    message: str,
    history: list[dict] | None = None,
    prior_intent: str | None = None,
) -> dict:
    """
    Classify a query.

    Parameters
    ----------
    message : str
        The incoming user message.
    history : list[dict] | None
        Prior conversation turns (as {"role", "content"} dicts). We use up
        to the last 3 USER messages for context. Pass None to disable.
    prior_intent : str | None
        The intent classified on the previous turn, if any. Threaded into
        the LLM prompt so follow-ups like "and what about X" route correctly.

    Returns
    -------
    dict with keys:
      tier, model_id, max_tokens, use_full_tools, intent, confidence, router
      and optionally: candidates, requires_clarification, matched_keyword
    """
    # ── Stage 1: Keyword fast-path (free, <1 ms) ──
    from .classifier import fast_path_intent
    fp = fast_path_intent(message)
    if fp is not None:
        return fp

    # ── Stage 2: LLM router (with context + cache) ──
    norm_key = _normalize_query(message)
    cache_key = _cache_key(norm_key, prior_intent)
    cached = _cache_get(cache_key)
    if cached is not None:
        # Don't serve cached clarifications — they're interactive turns
        if not cached.get("requires_clarification"):
            out = dict(cached)
            out["router"] = f"{out.get('router', 'unknown')}+cache"
            return out

    result = _llm_route(message, history=history, prior_intent=prior_intent)

    # Cache only non-clarify, non-unknown results
    if (
        result.get("intent")
        and result.get("intent") != "unknown"
        and not result.get("requires_clarification")
    ):
        _cache_put(cache_key, result)

    return result


# ═══════════════════════════════════════════════════════════════════════════
#  LLM routing with context
# ═══════════════════════════════════════════════════════════════════════════

def _llm_route(
    message: str,
    history: list[dict] | None,
    prior_intent: str | None,
) -> dict:
    """Call Flash Lite for intent classification. Falls back to keywords."""
    try:
        gw = _get_gateway()
        if not gw.available():
            return _keyword_fallback(message)

        # Build a single user-turn prompt that includes context.
        # We do NOT send raw chat history — the classifier only needs
        # the last few USER messages to resolve follow-ups.
        user_prompt_parts: list[str] = []
        if prior_intent:
            user_prompt_parts.append(
                f"Previous turn's intent: {prior_intent}\n"
                "If the current message is a follow-up on the same topic, "
                "prefer the same intent unless the topic clearly shifted."
            )

        recent_user_messages = _last_user_messages(history or [], n=3)
        if len(recent_user_messages) > 1:
            preceding = recent_user_messages[:-1]
            user_prompt_parts.append(
                "Recent context (earlier user turns, oldest first):\n"
                + "\n".join(f"- {m}" for m in preceding)
            )

        user_prompt_parts.append(f'Current message: "{message}"')
        user_prompt_parts.append("Classify and output JSON only.")
        user_prompt = "\n\n".join(user_prompt_parts)

        result = gw.create_message(
            model_id=FLASH_MODEL,
            max_tokens=150,  # Tier C fix: was 30, now enough room for full JSON
            system=_ROUTER_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            tools=None,
        )

        parsed = _extract_json(result.text or "")
        if not parsed or "intent" not in parsed:
            return _keyword_fallback(message)

        intent = str(parsed.get("intent", "general")).strip()
        complexity = str(parsed.get("complexity", "simple")).strip()
        confidence = str(parsed.get("confidence", "medium")).strip()
        candidates_raw = parsed.get("candidates") or []
        candidates = [
            str(c).strip()
            for c in candidates_raw
            if isinstance(c, str) and c.strip()
        ]

        tier = "complex" if complexity == "complex" else "simple"

        out = {
            "tier": tier,
            # Both tiers run on flash. Previously simple/fast turns dropped
            # to flash-lite for cost — but flash-lite was the source of
            # most reliability complaints, so we always pay the small flash
            # premium for consistent answers.
            "model_id": FLASH_MODEL,
            "max_tokens": 8192 if tier == "complex" else 4096,
            "use_full_tools": tier == "complex",
            "intent": intent,
            "confidence": confidence,
            "candidates": candidates,
            "router": "llm",
        }

        # Clarify only when candidates span different data groups
        if confidence == "low" and _should_clarify(intent, candidates):
            out["requires_clarification"] = True

        return out

    except Exception as e:
        print(f"[router] LLM failed, using keywords: {e}")
        return _keyword_fallback(message)


def _last_user_messages(history: list[dict], n: int = 3) -> list[str]:
    """Pick the last N user messages from history, oldest first among them."""
    picked: list[str] = []
    for item in reversed(history):
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            picked.append(content.strip())
            if len(picked) >= n:
                break
    picked.reverse()
    return picked


def _keyword_fallback(message: str) -> dict:
    """Final safety net — keyword classifier with intent=general."""
    from .classifier import classify_query
    base = classify_query(message)
    base["intent"] = "general"
    base["confidence"] = "low"
    base["candidates"] = []
    base["router"] = "keyword_fallback"
    return base
