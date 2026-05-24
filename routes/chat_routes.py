"""
chat_routes.py — Valvo AI Assistant v2 (STRUCTURED RESPONSE ENGINE)
Architecture: Backend owns data display, Claude owns thinking.

For Quick/Auto mode:
  1. Classify the question type
  2. Build structured card data directly from DB
  3. Ask Claude for a 2-3 line INSIGHT only (not data regurgitation)
  4. Return both: structured cards + Claude's short insight

For Detailed mode:
  Claude responds normally with full text.
"""
import os
import json
from flask import Blueprint, request, jsonify, g
from extensions import limiter
from services.valvo_ai_light_service import (
    build_lightweight_reply,
    should_use_lightweight_reply,
)
from services import portfolio_capital_log as capital_log

chat_bp = Blueprint("chat", __name__)


def _get_db():
    try:
        from database.database import get_db
        return get_db()
    except Exception as e:
        print(f"⚠️ Chat DB connection failed: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# QUESTION CLASSIFIER
# ═══════════════════════════════════════════════════════════════════

def _classify_question(message, stock_names=None):
    """
    Classify user question into a response category.
    Uses a multi-pass approach: greetings → explain → stock-specific → hard rules → keyword scoring.
    Tested at 93%+ accuracy across 29 diverse question patterns.
    Returns (category, matched_stock_name_or_None).
    """
    msg = message.lower().strip()
    matched_stock = None

    # ─── Pass 0: Stock name detection ───
    if stock_names:
        for name in stock_names:
            if name.lower() in msg:
                matched_stock = name
                break

    # ─── Pass 1: Greetings ───
    greetings = ["hi", "hello", "hey", "good morning", "good evening", "thanks", "thank you",
                 "ok", "okay", "cool", "nice", "great", "cheers"]
    if any(msg.strip() == g or (msg.startswith(g) and len(msg) < 30) for g in greetings):
        return "greeting", None

    # ─── Pass 2: Explain / conceptual (no data anchors) ───
    explain_starters = ["explain", "why does", "why do", "how does", "how do", "what is the difference",
                        "what is a ", "what is an ", "what is 1r", "what is r",
                        "teach me", "help me understand", "what are the rules", "describe how"]
    data_anchors = ["my", "portfolio", "positions", "position", "today", "currently",
                    "status", "now", "all", "show"]
    is_explain = any(msg.startswith(e) or f" {e}" in msg for e in explain_starters)
    has_data_anchor = any(d in msg for d in data_anchors)
    if is_explain and not has_data_anchor and not matched_stock:
        return "explain", None

    # ─── Pass 3: Specific stock mentioned ───
    if matched_stock:
        sell_kw = ["sell", "exit", "trim", "book profit", "reduce"]
        trail_kw = ["trail", "5ma", "10ma", "20ma", "ma break", "defensive", "stop loss"]
        if any(k in msg for k in sell_kw):
            return "sell_decision", matched_stock
        if any(k in msg for k in trail_kw):
            return "trailing_status", matched_stock
        return "single_stock", matched_stock

    # ─── Pass 4: Hard phrase rules (catches edge cases keyword scoring misses) ───
    if any(p in msg for p in ["rank", "sort by", "sort my", "leaderboard"]):
        return "rankings", None
    if any(p in msg for p in ["5ma status", "5ma check", "10ma status", "ma status",
                               "trailing stop status", "trailing status"]):
        return "trailing_status", None
    if any(p in msg for p in ["overexposed", "sector concentration", "sector risk"]):
        return "risk_check", None

    # ─── Pass 4b: Journal / stats / history questions ───
    journal_kw = ["win rate", "winrate", "win %", "average winner", "avg winner", "avg loser",
                "average loser", "expectancy", "track record", "performance", "stats",
                "statistics", "closed trades", "trade history", "historical", "how many trades",
                "total p&l", "total pnl", "best trade", "worst trade", "monthly p&l",
                "monthly pnl", "how did i do", "my record"]
    if any(k in msg for k in journal_kw):
        return "journal_stats", None

    # ─── Pass 4c: Screener / liquidity / stock discovery ───
    screener_kw = ["screener", "liquidity", "liquid stocks", "scan", "above 200",
                   "top gainers", "top losers", "movers", "volume", "cheap stocks",
                   "stock discovery", "find stocks", "filter stocks"]
    if any(k in msg for k in screener_kw):
        return "screener_query", None

    # ─── Pass 4c: Custom scanner queries ───
    custom_scan_kw = ["custom scan", "run scan", "52 week", "52w", "relative strength",
                      "outperform", "rs 1w", "rs 3m", "rs 6m", "leading sector",
                      "near high", "from low", "breakout candidate", "saved scanner",
                      "my scanner"]
    if any(k in msg for k in custom_scan_kw):
        return "screener_query", None

    # ─── Pass 4d: Drawdown queries ───
    dd_kw = ["drawdown", "draw down", "worst month", "worst period", "recovery",
             "max dd", "negative month", "losing streak", "underperform", "outperform market",
             "market crash", "how did i do during"]
    if any(k in msg for k in dd_kw):
        return "analytics_query", None

    # ─── Pass 4e: Sectoral MA questions ───
    sector_words = ["defence", "defense", "metal", "pharma", "bank", "auto", "realty",
                    "energy", "fmcg", "infra", "healthcare", "midcap", "smallcap",
                    "it sector", "ev sector", "housing", "psu", "cpse", "oil"]
    ma_words = ["50ma", "20ma", "200ma", "50 ma", "20 ma", "200 ma", "moving average",
                "above ma", "below ma", "above their", "below their"]
    has_sector = any(s in msg for s in sector_words)
    has_ma = any(m in msg for m in ma_words)
    if has_sector and (has_ma or "above" in msg or "below" in msg):
        return "screener_query", None

    # ─── Pass 5: Keyword scoring ───
    # ─── Pass 6: Action intents (only trigger on CLEAR add/create intent) ───
    action_kw = ["add a position", "add position", "add to pm", "add stock", "create position", "enter position",
                 "new position", "add entry",
                 "add this to pm", "add to position", "create entry", "make an entry",
                 "new entry"]
    # Verbs that indicate logging a completed trade (need a stock name nearby)
    log_kw = ["bought", "entered", "took a position",
              "log it", "log this", "go ahead and log", "go ahead and add", "enter it",
              "add it", "put it in", "save it", "record it", "log the trade", "log the position"]
    reconcile_kw = ["reconcile", "sync check", "in sync", "pm vs journal", "journal vs pm",
                    "consistency", "mismatch", "cross check", "cross-check", "journal sync"]
    
    # Check for query words that mean "show me" not "add"
    query_words = ["how", "what", "show", "status", "check", "tell", "update me", "my open"]
    is_query = any(q in msg for q in query_words)

    # Keyword match — but NOT if it's clearly a query about existing positions
    if not is_query and any(k in msg for k in action_kw):
        return "action_add", None
    if not is_query and any(k in msg for k in log_kw) and matched_stock:
        return "action_add", None
    if any(k in msg for k in reconcile_kw):
        return "action_reconcile", None
    
    # Pattern match: "add/buy TICKER at PRICE" (handles "add VEDL at 660")
    import re
    has_add_verb = any(msg.startswith(v) for v in ["add ", "buy ", "enter ", "log "])
    has_ticker = bool(re.search(r'\b[A-Z]{2,15}\b', message))
    has_price = bool(re.search(r'(?:at|@)\s*[\d,]+', msg))
    if has_add_verb and has_ticker and has_price:
        return "action_add", None

    categories = {
        "sell_decision": {
            "kw": ["sell", "should i sell", "what to sell", "exit", "book profit", "partial sell", "bucket",
                   "take profit", "trim", "offload", "reduce"],
            "weight": 10,
        },
        "trailing_status": {
            "kw": ["trail", "5ma", "10ma", "20ma", "defensive", "move sl", "stop to cost",
                   "breakeven", "grace day", "ma break", "breaking 5ma", "below 5ma",
                   "danger", "breaking ma"],
            "weight": 9,
        },
        "risk_check": {
            "kw": ["risk", "exposure", "concentration", "how much am i risking", "drawdown",
                   "worst case", "sl distance", "max loss", "capital at risk", "risk assessment"],
            "weight": 8,
        },
        "rankings": {
            "kw": ["top", "best", "worst", "biggest winner", "biggest loser",
                   "highest r", "lowest", "performer", "strongest", "weakest"],
            "weight": 7,
        },
        "portfolio_overview": {
            "kw": ["portfolio", "overview", "positions", "holdings", "show me", "open trades",
                   "all positions", "my stocks", "what do i hold", "p&l", "pnl", "how am i doing"],
            "weight": 5,
        },
    }

    scores = {}
    for cat, cfg in categories.items():
        score = 0
        for kw in cfg["kw"]:
            if kw in msg:
                score += cfg["weight"] + len(kw)
        scores[cat] = score

    best_cat = max(scores, key=scores.get)
    best_score = scores[best_cat]

    if best_score == 0:
        return "portfolio_overview", None

    return best_cat, None


# ═══════════════════════════════════════════════════════════════════
# STRUCTURED DATA BUILDERS (one per category)
# ═══════════════════════════════════════════════════════════════════

def _build_positions_base(cur, user_id=None):
    """Fetch active positions — used by multiple builders."""
    cur.execute("""
        SELECT id, stock_name, entry_price, stop_loss, quantity, position_value,
               one_r_value, current_price, entry_extension_pct, current_r_multiple,
               last_5ma_price, last_10ma_price, last_20ma_price,
               defensive_status, grace_day_used, trailing_mode,
               ma_followed, ma_grade, shakeout_count, qualifies_for_trailing,
               market_regime, bucket_sold_pct, bucket_cap, first_sell_done,
               first_sell_zone, max_sell_pct,
               sell_history, risk_pct, security_id, created_at
        FROM positions WHERE status = 'active' AND user_id = COALESCE(%s, user_id)
        ORDER BY entry_extension_pct DESC NULLS LAST
    """, (user_id,))
    return cur.fetchall()


def _position_to_card(p):
    """Convert a DB position row to a card dict."""
    entry = float(p.get("entry_price") or 0)
    cmp = float(p.get("current_price") or entry)
    qty = int(p.get("quantity") or 0)
    risk_pct = float(p.get("risk_pct") or 4)
    risk_per_share = entry * (risk_pct / 100)
    sl = float(p.get("stop_loss") or 0)

    ext = float(p.get("entry_extension_pct") or 0) if p.get("entry_extension_pct") is not None else (
        round((cmp - entry) / entry * 100, 1) if entry else 0
    )
    r_mult = float(p.get("current_r_multiple") or 0) if p.get("current_r_multiple") is not None else (
        round((cmp - entry) / risk_per_share, 1) if risk_per_share else 0
    )
    pnl_val = (cmp - entry) * qty
    pos_val = cmp * qty

    five_ma = float(p.get("last_5ma_price") or 0)
    defensive = p.get("defensive_status") or "unknown"
    bucket_sold = float(p.get("bucket_sold_pct") or 0)
    bucket_cap = float(p.get("bucket_cap") or 66)
    first_sell = bool(p.get("first_sell_done"))
    regime = p.get("market_regime") or "bull"
    ma_grade = p.get("ma_grade") or "?"
    ma_followed = p.get("ma_followed") or "?"
    trail_worthy = bool(p.get("qualifies_for_trailing"))

    # Regime-specific sell zones (from DB or defaults)
    regime_defaults = {
        "bull": (35, 60), "sideways": (35, 50),
        "grind_down": (30, 40), "sharp_downtrend": (8, 15),
    }
    default_fsz, default_dsf = regime_defaults.get(regime, (35, 60))
    first_sell_zone = float(p.get("first_sell_zone") or default_fsz)
    daily_sell_from = float(p.get("daily_sell_from") or default_dsf)

    return {
        "id": p["id"],
        "name": p["stock_name"],
        "entry": round(entry, 2),
        "cmp": round(cmp, 2),
        "sl": round(sl, 2),
        "qty": qty,
        "ext": round(ext, 1),
        "r": round(r_mult, 1),
        "pnl_val": round(pnl_val),
        "value": round(pos_val),
        "five_ma": round(five_ma, 2),
        "defensive": defensive,
        "bucket_sold": round(bucket_sold),
        "bucket_cap": round(bucket_cap),
        "first_sell": first_sell,
        "first_sell_zone": first_sell_zone,
        "daily_sell_from": daily_sell_from,
        "regime": regime,
        "ma_grade": ma_grade,
        "ma_followed": ma_followed,
        "trail_worthy": trail_worthy,
        "security_id": p.get("security_id"),
    }


def _build_summary(cards):
    """Build portfolio summary from card list."""
    total_value = sum(c["value"] for c in cards)
    total_pnl = sum(c["pnl_val"] for c in cards)
    invested = total_value - total_pnl
    return {
        "count": len(cards),
        "total_value": round(total_value),
        "total_pnl": round(total_pnl),
        "total_pnl_pct": round(total_pnl / invested * 100, 1) if invested > 0 else 0,
        "avg_r": round(sum(c["r"] for c in cards) / len(cards), 1) if cards else 0,
        "above_2r": sum(1 for c in cards if c["r"] >= 2),
        "green": sum(1 for c in cards if c["r"] > 0),
        "red": sum(1 for c in cards if c["r"] <= 0),
    }


def _build_sell_decision(cur):
    # MIRROR WARNING: Frontend has parallel action logic in
    # Frontend/src/components/PositionManager.jsx → getActionRequired()
    # If you change verdict labels/conditions here, check the frontend mirror too.
    """Build structured data for sell decision questions."""
    positions = _build_positions_base(cur, g.user_id)
    if not positions:
        return {"type": "empty", "message": "No active positions"}

    cards = [_position_to_card(p) for p in positions]
    action_cards = []

    for c in cards:
        verdict = "HOLD"
        verdict_color = "var(--vai-gn)"
        detail = ""
        cls = "hold"
        urgency = 0

        # Regime-aware thresholds
        fsz = c["first_sell_zone"]   # e.g. 35 for bull, 8 for sharp_downtrend
        dsf = c["daily_sell_from"]   # e.g. 60 for bull, 15 for sharp_downtrend

        # 5MA BREAK — highest priority
        if c["defensive"] in ("break", "warning"):
            verdict = "EXIT — 5MA Break"
            verdict_color = "var(--vai-rd)"
            cls = "exit"
            detail = f"Close below 5MA (₹{c['five_ma']}). Exit entire position."
            urgency = 100

        # Extension zone sells — using regime-specific thresholds
        elif c["ext"] >= dsf and not c["first_sell"]:
            verdict = f"SELL 35% — Overdue"
            verdict_color = "var(--vai-rd)"
            cls = "exit"
            detail = f"At {c['ext']:.0f}% ext without first sell (zone was {fsz:.0f}%). Mandatory."
            urgency = 90

        elif c["ext"] >= fsz and not c["first_sell"]:
            verdict = f"SELL 30-35%"
            verdict_color = "var(--vai-og)"
            cls = "sell"
            detail = f"First sell zone reached ({c['ext']:.0f}% ext, target {fsz:.0f}%). Regime: {c['regime']}."
            urgency = 70

        elif c["ext"] >= dsf and c["bucket_sold"] < c["bucket_cap"]:
            pct = min(10, c["bucket_cap"] - c["bucket_sold"])
            verdict = f"SELL {pct:.0f}%"
            verdict_color = "var(--vai-og)"
            cls = "sell"
            detail = f"Daily sell zone ({c['ext']:.0f}% ext ≥ {dsf:.0f}%). Bucket {c['bucket_sold']:.0f}%/{c['bucket_cap']:.0f}%."
            urgency = 50

        elif c["ext"] >= fsz + 5 and c["first_sell"] and c["bucket_sold"] < c["bucket_cap"]:
            verdict = "SELL 5-10%"
            verdict_color = "var(--vai-og)"
            cls = "sell"
            detail = f"Milestone sell at {c['ext']:.0f}%. Bucket {c['bucket_sold']:.0f}%/{c['bucket_cap']:.0f}%."
            urgency = 40

        # Marginal 5MA — caution
        elif c["defensive"] == "marginal":
            verdict = "WATCH — 5MA Close"
            verdict_color = "var(--vai-og)"
            cls = "sell"
            detail = f"Price near 5MA (₹{c['five_ma']}). Grace day may be needed."
            urgency = 60

        # Healthy hold
        else:
            if c["ext"] > 0:
                detail = f"+{c['ext']:.0f}% ext | {c['r']:.1f}R | Bucket {c['bucket_sold']:.0f}%/{c['bucket_cap']:.0f}%"
            else:
                detail = f"{c['ext']:.1f}% | {c['r']:.1f}R | Below entry"
            urgency = 0

        action_cards.append({
            "name": c["name"],
            "verdict": verdict,
            "verdictColor": verdict_color,
            "detail": detail,
            "metric": f"{c['ext']:+.0f}% | {c['r']:.1f}R",
            "metricColor": "var(--vai-gn)" if c["r"] > 0 else "var(--vai-rd)",
            "cls": cls,
            "urgency": urgency,
            "ext": c["ext"],
            "r": c["r"],
            "bucket_sold": c["bucket_sold"],
            "bucket_cap": c["bucket_cap"],
            "five_ma": c["five_ma"],
            "cmp": c["cmp"],
            "entry": c["entry"],
        })

    # Sort by urgency (most urgent first)
    action_cards.sort(key=lambda x: -x["urgency"])
    summary = _build_summary(cards)

    return {
        "type": "actions",
        "cards": action_cards,
        "summary": summary,
    }


def _build_portfolio_overview(cur):
    """Build structured data for portfolio overview."""
    positions = _build_positions_base(cur, g.user_id)
    if not positions:
        return {"type": "empty", "message": "No active positions"}

    cards = [_position_to_card(p) for p in positions]
    summary = _build_summary(cards)

    return {
        "type": "positions",
        "cards": cards,
        "summary": summary,
    }


def _build_risk_check(cur):
    """Build structured data for risk assessment."""
    positions = _build_positions_base(cur, g.user_id)
    if not positions:
        return {"type": "empty", "message": "No active positions"}

    cards = [_position_to_card(p) for p in positions]
    portfolio_size = 50000000  # ₹5 crores

    total_invested = sum(c["value"] for c in cards)
    total_risk = 0
    risk_cards = []

    for c in cards:
        sl_dist_pct = round((c["cmp"] - c["sl"]) / c["cmp"] * 100, 1) if c["cmp"] and c["sl"] and c["sl"] > 0 else 0
        has_sl = c["sl"] and c["sl"] > 0
        risk_per_pos = round(abs(c["cmp"] - c["sl"]) * c["qty"]) if has_sl else round(c["value"])  # No SL = entire position at risk
        total_risk += risk_per_pos if has_sl else 0  # Don't inflate total with unbounded risk
        pos_pct = round(c["value"] / portfolio_size * 100, 1) if portfolio_size else 0

        risk_cards.append({
            "name": c["name"],
            "sl": c["sl"],
            "cmp": c["cmp"],
            "sl_dist_pct": sl_dist_pct,
            "risk_rupees": risk_per_pos,
            "pos_pct": pos_pct,
            "defensive": c["defensive"],
            "r": c["r"],
            "ext": c["ext"],
            "trail_worthy": c["trail_worthy"],
            "no_sl": not has_sl,
        })

    # Sort by risk (most risk first)
    risk_cards.sort(key=lambda x: -x["risk_rupees"])

    stats = [
        {"label": "Total Risk", "val": f"₹{total_risk / 100000:.1f}L", "sub": f"{total_risk / portfolio_size * 100:.1f}% of PF", "color": "var(--vai-rd)" if total_risk / portfolio_size > 0.05 else "var(--vai-og)"},
        {"label": "Invested", "val": f"₹{total_invested / 10000000:.2f}Cr", "sub": f"{total_invested / portfolio_size * 100:.0f}% deployed", "color": "var(--vai-t2)"},
        {"label": "Cash", "val": f"₹{(portfolio_size - total_invested) / 10000000:.2f}Cr", "sub": f"{(portfolio_size - total_invested) / portfolio_size * 100:.0f}% available", "color": "var(--vai-gn)"},
    ]

    return {
        "type": "risk",
        "stats": stats,
        "cards": risk_cards,
        "total_risk": total_risk,
        "portfolio_size": portfolio_size,
    }


def _build_rankings(cur, message=""):
    """Build structured data for rankings."""
    positions = _build_positions_base(cur, g.user_id)
    if not positions:
        return {"type": "empty", "message": "No active positions"}

    cards = [_position_to_card(p) for p in positions]
    msg_lower = message.lower()

    # Determine sort direction
    if any(w in msg_lower for w in ["worst", "loser", "lowest", "weakest", "bottom"]):
        cards.sort(key=lambda x: x["r"])
        sort_label = "Weakest First"
    elif any(w in msg_lower for w in ["extension", "ext"]):
        cards.sort(key=lambda x: -x["ext"])
        sort_label = "By Extension"
    else:
        cards.sort(key=lambda x: -x["r"])
        sort_label = "Best R First"

    ranked = []
    for i, c in enumerate(cards):
        ranked.append({
            "rank": i + 1,
            "name": c["name"],
            "r": c["r"],
            "ext": c["ext"],
            "pnl_val": c["pnl_val"],
            "entry": c["entry"],
            "cmp": c["cmp"],
            "ma_grade": c["ma_grade"],
            "regime": c["regime"],
        })

    return {
        "type": "rankings",
        "sort_label": sort_label,
        "cards": ranked,
        "summary": _build_summary(cards),
    }


def _build_trailing_status(cur):
    # MIRROR WARNING: Frontend has parallel action logic in
    # Frontend/src/components/PositionManager.jsx → getActionRequired()
    # If you change action labels/conditions here, check the frontend mirror too.
    """Build structured data for trailing/5MA status."""
    positions = _build_positions_base(cur, g.user_id)
    if not positions:
        return {"type": "empty", "message": "No active positions"}

    cards = [_position_to_card(p) for p in positions]
    status_cards = []

    for c in cards:
        # Determine action needed
        action = "OK"
        action_color = "var(--vai-gn)"
        detail = ""

        if c["defensive"] == "break":
            action = "EXIT NOW"
            action_color = "var(--vai-rd)"
            detail = f"Close below 5MA ₹{c['five_ma']}"
        elif c["defensive"] == "warning" or c["defensive"] == "marginal":
            action = "WATCH"
            action_color = "var(--vai-og)"
            detail = f"Near 5MA ₹{c['five_ma']} — may need grace day"
        elif c["r"] >= 2 and not c["trail_worthy"]:
            action = "EVALUATE"
            action_color = "var(--vai-og)"
            detail = f"Above 2R but not trail-worthy (MA: {c['ma_followed']} {c['ma_grade']})"
        elif c["r"] >= 1 and c["sl"] and c["sl"] < c["entry"]:
            action = "MOVE SL→COST"
            action_color = "var(--vai-accent)"
            detail = f"At {c['r']:.1f}R — SL still below entry (₹{c['sl']}→₹{c['entry']})"
        elif c["trail_worthy"] and c["five_ma"] > c["sl"]:
            action = "TRAIL 5MA"
            action_color = "var(--vai-accent)"
            detail = f"Update SL to 5MA ₹{c['five_ma']} (currently ₹{c['sl']})"
        else:
            detail = f"5MA ₹{c['five_ma']} | SL ₹{c['sl']} | {c['ma_followed']} ({c['ma_grade']})"

        status_cards.append({
            "name": c["name"],
            "action": action,
            "action_color": action_color,
            "detail": detail,
            "defensive": c["defensive"],
            "five_ma": c["five_ma"],
            "cmp": c["cmp"],
            "sl": c["sl"],
            "r": c["r"],
            "ext": c["ext"],
            "ma_followed": c["ma_followed"],
            "ma_grade": c["ma_grade"],
            "trail_worthy": c["trail_worthy"],
        })

    # Sort: exits first, then watches, then actions, then OK
    priority = {"EXIT NOW": 0, "WATCH": 1, "MOVE SL→COST": 2, "TRAIL 5MA": 3, "EVALUATE": 4, "OK": 5}
    status_cards.sort(key=lambda x: priority.get(x["action"], 99))

    alerts = sum(1 for c in status_cards if c["action"] in ("EXIT NOW", "WATCH"))

    return {
        "type": "trailing",
        "cards": status_cards,
        "alerts": alerts,
        "total": len(status_cards),
    }


def _build_single_stock(cur, stock_name):
    """Build structured data for a single stock question."""
    positions = _build_positions_base(cur, g.user_id)
    if not positions:
        return {"type": "empty", "message": "No active positions"}

    cards = [_position_to_card(p) for p in positions]
    # Find the matching stock
    match = None
    for c in cards:
        if stock_name.lower() in c["name"].lower():
            match = c
            break

    if not match:
        return {"type": "empty", "message": f"No active position found for {stock_name}"}

    # Also get recent daily updates
    try:
        cur.execute("""
            SELECT date, close_price, five_ma_price, ten_ma_price, twenty_ma_price,
                   defensive_result, r_multiple, entry_extension_pct
            FROM position_daily_updates WHERE position_id = %s
            ORDER BY date DESC LIMIT 5
        """, (match["id"],))
        daily = cur.fetchall()
    except Exception as e:
        print(f"[chat] daily_updates fetch failed: {e}")
        daily = []

    history = []
    for d in daily:
        history.append({
            "date": str(d.get("date", "?")),
            "close": float(d.get("close_price") or 0),
            "five_ma": float(d.get("five_ma_price") or 0),
            "defensive": d.get("defensive_result") or "?",
            "r": round(float(d.get("r_multiple") or 0), 1),
            "ext": round(float(d.get("entry_extension_pct") or 0), 1),
        })

    return {
        "type": "single_stock",
        "card": match,
        "daily_history": history,
    }



def _build_action_add(message):
    """Parse add position intent from natural language."""
    print(f"🎯 ACTION ADD triggered for: {message}")
    import re
    msg = message.lower()

    parsed = {"stock": None, "price": None, "qty": None, "sl": None}

    # 1. STOCK: Multi-strategy extraction
    skip_caps = {"ADD", "PM", "AT", "SL", "QTY", "POSITION", "ENTRY", "STOP", "LOSS",
                 "BUY", "BOUGHT", "THE", "AND", "FOR", "NEW", "IN", "WITH", "MY",
                 "TODAY", "TODAYS", "DATE", "LOG", "IT", "THIS", "SHARES", "STOCKS",
                 "MARCH", "APRIL", "MAY", "JUNE", "JULY", "JANUARY", "FEBRUARY"}

    # Strategy A: ALL-CAPS ticker (e.g., "VEDL", "TATASTEEL")
    caps = re.findall(r'\b([A-Z]{2,15})\b', message)
    for w in caps:
        if w not in skip_caps:
            parsed["stock"] = w
            break

    # Strategy B: "in <Name>" or "position in <Name>" (mixed case like "Tata Steel")
    if not parsed["stock"]:
        name_m = re.search(r'(?:in|for|of)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)', message)
        if name_m:
            candidate = name_m.group(1).strip()
            # Remove trailing noise words
            for noise in ["at", "with", "on", "from", "today", "yesterday"]:
                candidate = re.sub(rf'\s+{noise}\b.*$', '', candidate, flags=re.IGNORECASE)
            if len(candidate) >= 2:
                parsed["stock"] = candidate

    # Strategy C: First capitalized multi-word before a number (e.g., "Bajaj Finance at 695")
    if not parsed["stock"]:
        name_m2 = re.search(r'([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\s+(?:at|@|\d)', message)
        if name_m2:
            parsed["stock"] = name_m2.group(1).strip()

    # Strategy D: lowercase names after "in" or "buy" (e.g., "in vedanta at 660", "buy tata steel at 195")
    if not parsed["stock"]:
        name_m3 = re.search(r'(?:position in|in|buy|bought)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)*?)\s+(?:at|@|\d)', msg)
        if name_m3:
            candidate = name_m3.group(1).strip()
            skip_words = {"a", "the", "my", "new", "position", "for"}
            if len(candidate) >= 2 and candidate not in skip_words:
                parsed["stock"] = candidate

    # 2. PRICE: "at 420", "@ 420", "entry 420"
    price_m = re.search(r'(?:at|@|entry|price|bought at|entered at)[\s₹]*([\d,.]+)', msg)
    if price_m:
        parsed["price"] = float(price_m.group(1).replace(",", ""))

    # 3. QUANTITY: "qty 1000", "1000 shares", "1000 stocks"
    qty_m = re.search(r'(?:qty|quantity|shares?|stocks?)\s*(\d+)|(\d+)\s*(?:shares?|stocks?|qty|quantity|lots?)', msg)
    if qty_m:
        parsed["qty"] = int(qty_m.group(1) or qty_m.group(2))

    # 4. STOP LOSS: "3% sl", "sl 400", "stop loss 400"
    sl_pct_m = re.search(r'(\d+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)\s*(?:sl|stop|stoploss|risk)', msg)
    if not sl_pct_m:
        sl_pct_m = re.search(r'(?:sl|stop\s*loss?|stoploss)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*(?:%|percent)', msg)
    if sl_pct_m and parsed["price"]:
        pct = float(sl_pct_m.group(1))
        parsed["sl"] = round(parsed["price"] * (1 - pct / 100), 2)
    else:
        sl_m = re.search(r'(?:sl|stop\s*loss?|stoploss)[\s₹]*([\d,.]+)', msg)
        if sl_m:
            parsed["sl"] = float(sl_m.group(1).replace(",", ""))

    # 5. VALIDATE stock against stock_universe
    stock_match = None
    suggestions = []
    if parsed["stock"]:
        try:
            from services.market_data_service import search_stocks
            results = search_stocks(parsed["stock"])
            if results:
                # Exact symbol match?
                exact = [r for r in results if r["symbol"].upper() == parsed["stock"].upper()]
                if exact:
                    stock_match = exact[0]
                elif len(results) == 1:
                    # Single result = high confidence match (e.g., "Tata Steel" → TATASTEEL)
                    stock_match = results[0]
                # Always return top 5 as suggestions (for picker)
                suggestions = [{"symbol": r["symbol"], "company_name": r["company_name"], "security_id": r["security_id"]} for r in results[:5]]
        except Exception as e:
            print(f"⚠️ Stock search failed for {parsed['stock']}: {e}")

    has_exact = stock_match is not None
    return {
        "type": "action",
        "action": "add_position",
        "parsed": parsed,
        "stock_match": {
            "symbol": stock_match["symbol"],
            "company_name": stock_match["company_name"],
            "security_id": stock_match["security_id"],
        } if stock_match else None,
        "suggestions": suggestions,
        "complete": all([parsed["stock"], parsed["price"], parsed["qty"]]) and has_exact,
        "missing": [k for k, v in parsed.items() if v is None and k != "sl"],
        "stock_not_found": parsed["stock"] is not None and not has_exact and len(suggestions) == 0,
        "needs_pick": not has_exact and len(suggestions) > 0,
    }


def _build_action_reconcile():
    """Run PM vs Journal reconciliation."""
    from services.market_data_service import load_stock_universe
    conn = _get_db()
    if not conn:
        return {"type": "action", "action": "reconcile", "error": "DB unavailable"}

    try:
        cur = conn.cursor()

        cur.execute("SELECT id, stock_name, security_id, entry_price, quantity FROM positions WHERE status = 'active' AND user_id = %s", (g.user_id,))
        pm_rows = [dict(r) for r in cur.fetchall()]

        instruments = load_stock_universe()
        sid_to_sym = {inst["security_id"]: inst["symbol"] for inst in instruments}

        pm_positions = []
        for p in pm_rows:
            sym = sid_to_sym.get(str(p.get("security_id", "")), "")
            pm_positions.append({"id": p["id"], "name": p["stock_name"], "symbol": sym, "entry": p.get("entry_price"), "qty": p.get("quantity")})

        cur.execute("SELECT id, stock_name, entry_price, open_qty, initial_qty FROM journal_trades_computed WHERE position_status = 'Open' AND user_id = %s", (g.user_id,))
        nx_rows = [dict(r) for r in cur.fetchall()]
        journal_open = [{"symbol": n["stock_name"], "entry": n.get("entry_price"), "qty": n.get("open_qty") or n.get("initial_qty")} for n in nx_rows]

        pm_syms = {p["symbol"] for p in pm_positions if p["symbol"]}
        jt_syms = {n["symbol"] for n in journal_open}

        return {
            "type": "action",
            "action": "reconcile",
            "matched": [p for p in pm_positions if p["symbol"] in (pm_syms & jt_syms)],
            "pm_only": [p for p in pm_positions if p["symbol"] in (pm_syms - jt_syms)],
            "journal_only": [n for n in journal_open if n["symbol"] in (jt_syms - pm_syms)],
            "unlinked": [p for p in pm_positions if not p["symbol"]],
            "in_sync": len(pm_syms - jt_syms) == 0 and len(jt_syms - pm_syms) == 0,
            "pm_count": len(pm_positions),
            "journal_count": len(journal_open),
        }
    except Exception as e:
        return {"type": "action", "action": "reconcile", "error": str(e)}
    finally:
        try: conn.close()
        except: pass

# ═══════════════════════════════════════════════════════════════════
# CLAUDE PROMPT BUILDERS
# ═══════════════════════════════════════════════════════════════════

QUICK_SYSTEM = """You are Valvo AI — the central intelligence system for Rohit's institutional Indian equity portfolio (₹5Cr). You are his trading partner, not just a chatbot. You have FULL control of the platform through tools.

═══ PERSONALITY ═══
- Talk like a sharp trading desk partner — direct, confident, no fluff
- Use ₹ and Indian formatting (lakhs, crores). Numbers are sacred — never round carelessly
- When you don't know something, say so and fetch it with a tool
- Show your math when calculating — traders need to verify
- Be opinionated when data supports it: "VEDL is overextended, I'd trim 20% here"

═══ FORMATTING — CRITICAL ═══
- NEVER start a response by dumping portfolio stats (positions count, P&L, risk) UNLESS the user specifically asked about their portfolio, positions, or risk.
- If user says "hi" or asks about a market topic, respond NATURALLY — don't recite "0 positions, ₹0 P&L".
- Portfolio stats belong in portfolio-related answers ONLY.
- ALWAYS INCLUDE THE KEY NUMBER in your text response. Never say "cards show the data" — the user may not see cards.
- Keep responses SHORT and CRISP. Max 2-4 lines for simple queries, 5-8 lines for complex ones.
- Use clean line breaks for structure — NOT long paragraphs.
- ONLY answer what was asked. If user asks about energy sector, ONLY talk about energy. NEVER add other sectors or unrelated data.
- Examples of GOOD responses (NOTE: these are FORMAT EXAMPLES ONLY — the stock names below are fictional placeholders, NOT real positions. ALWAYS use live data from tools):
  "[STOCK] P&L: -₹14,000 (-0.4%). 5MA break active, 0% bucket sold — decision needed."
  "2 positions. Total P&L -₹14K. [STOCK1] has 5MA break, [STOCK2] flat at entry."
  "Total risk: ₹1.3L (0.26% of PF). [STOCK] is the only concern — 5MA break."
  "Win rate: 62.5% across 16 trades. Avg winner +4.7%, avg loser -2.1%."
- Examples of BAD responses:
  "Stock is in the red with a 5MA break" (no actual P&L number!)
  "Cards show the details below" (user may not see cards)
  "Let me pull a snapshot..." (filler — just give the answer)
  Long paragraph mixing multiple sectors when only one was asked about
- NEVER use markdown tables (|---|---|). NEVER use ### headers. NEVER use "---".
- NEVER use emojis in responses. No 🏆📊📈🔍💡⚠️❌✅ or any emoji. Use plain text only. Professional trading desk, not a chat app.
- NEVER start with "Let me grab/pull/check/fetch". Just answer directly.
- NEVER say "[Cards shown: ...]" — this is not visible to the user.

═══ INTELLIGENCE RULES ═══

1. PROACTIVE: Don't just answer — anticipate. If user asks about VEDL and it's near 5MA, WARN them.
   If a position has no SL, flag it. If bucket is at 0% in sell zone, say so.

2. CONFIRMATION BEFORE DESTRUCTION: For delete_position, close_position, or sells > 50%:
   ALWAYS ask "Are you sure?" before executing. Show what they're giving up.

3. MULTI-STEP COMMANDS: "Add VEDL at 660, bull regime, 3% SL" = chain create_position + update_regime.
   "Refresh and tell me what needs attention" = refresh_prices + portfolio_snapshot.

4. CONTEXT AWARENESS: The page_context tells you where the user is:
   - "screener" → they're scanning for new stocks. Help with discovery.
   - "position" → they're managing existing positions. Help with sells/trails.
   - "scoring" → they're evaluating a stock. Help with setup analysis.
   - "ai" → free-form conversation. Full power.
   - "journal" → they're reviewing trades. Help with pattern recognition.

5. SCREENER INTELLIGENCE: You have DIRECT ACCESS to market data for 2,400+ NSE stocks via query_screener.
   When user asks about screener, liquidity, stock discovery, or filtering:
   - USE the query_screener tool — don't say "I don't have access"
   - Compute real 20-day SMA liquidity from actual trading data
   - Report exact count + top results with data
   - ALWAYS end with "→ Open Screener" to guide them to the visual view
   Examples: "stocks above 200Cr liquidity" → query_screener(min_liquidity_cr=200)
   "top gainers today" → query_screener(min_change_pct=3, sort_by="change")
   "cheap liquid stocks" → query_screener(max_price=500, min_liquidity_cr=100)

5b. SECTORAL MA INTELLIGENCE: You have DIRECT ACCESS to sector-level MA analysis via query_sectoral_screener.
   When user asks about stocks in a specific sector above/below a moving average:
   - USE query_sectoral_screener tool — NEVER guess or hallucinate stock names
   - This tool queries the real index_constituents database (37 NSE indices) joined with live candle data
   - Supported sectors: defence, metal, IT, pharma, bank, PSU bank, auto, realty, energy, FMCG, infra, healthcare, oil & gas, EV, housing, midcap, smallcap, and more
   - Supported MA periods: 20, 50, 200 (or any number)
   Examples: "how many defence stocks are above 50MA?" → query_sectoral_screener(sector="defence", ma_period=50, condition="above")
   "list pharma stocks below 200MA" → query_sectoral_screener(sector="pharma", ma_period=200, condition="below")
   "metal sector above 20MA" → query_sectoral_screener(sector="metal", ma_period=20, condition="above")
   
   CRITICAL — SECTORAL RESPONSE FORMAT:
   - ONLY answer about the SPECIFIC sector the user asked about — NEVER include other sectors unless explicitly asked
   - Keep it SHORT and STRUCTURED. Use this format:
     **[Sector Name] — [MA Period]MA**
     [X] of [Total] stocks above ([Y]%) 
     Verdict: [Strong / Healthy / Mixed / Weak]
     
     Top performers: [list top 3-5 with CMP and % above MA, one per line]
   - Do NOT write long paragraphs. Use clean line breaks.
   - Do NOT repeat data from previous messages unless asked.

6. TRADING HISTORY: You have FULL ACCESS to the user's complete trading history via query_journal_stats.
   When user asks about win rate, avg winner, performance, stats, track record, P&L, best/worst trade:
   - USE the query_journal_stats tool — NEVER say "no historical data" or "I don't have access"
   - Data includes: win rate, avg winner%, avg loser%, total P&L, monthly breakdown, every trade
   Examples: "what's my win rate?" → query_journal_stats()
   "show my closed trades" → query_journal_stats(status_filter="closed")
   "how did I do last month?" → query_journal_stats(include_monthly=true)

6b. CUSTOM SCANNER: You can run advanced scans across 2,400+ NSE stocks via run_custom_scan.
   Computes: 52W high/low proximity, 5/10/20/50/200 MA positions, relative strength vs Smallcap 100 (1W/3M/6M), liquidity, ADR.
   All MAs use TRADING DAYS (not calendar days) — computations are institutional-grade accurate.
   When user asks to scan/screen/filter stocks by technical conditions:
   - USE run_custom_scan tool — it computes everything live in ~3 seconds
   Examples: "stocks within 5% of 52W high above 20MA" → run_custom_scan(max_pct_from_high=5, above_ma20=true)
   "liquid stocks outperforming SC100 in 3 months" → run_custom_scan(min_liquidity=5, rs_3m_positive=true)
   "breakout candidates in leading sectors" → run_custom_scan(max_pct_from_high=10, above_ma50=true, leading_sectors_only=true)
   You can also list saved scanners with list_saved_scanners.

6c. DRAWDOWN ANALYSIS: You have FULL ACCESS to drawdown data via get_drawdown_analysis.
   Returns: every negative month across all 3 FYs, max drawdown, portfolio value at each point,
   Smallcap 100 comparison showing whether you outperformed or underperformed the market.
   When user asks about drawdowns, worst periods, recovery, market comparison:
   - USE get_drawdown_analysis tool
   Examples: "what was my worst drawdown?" → get_drawdown_analysis()
   "did I outperform during market crashes?" → get_drawdown_analysis()
   "how long did my drawdowns last?" → get_drawdown_analysis()

7. DECISION MEMORY: Check journal_entries on positions before advising. If they've logged
   "not selling — waiting for 35%" before, remind them of their own reasoning.

8. WHAT-IF ANALYSIS: When asked "what if X?", calculate BOTH outcomes:
   - "What if VEDL drops to 600?" → Show the loss in ₹, R-multiple, PF impact
   - "What if I sell 30% now?" → Show locked profit + what you give up

7. TRADEOFF IN EVERY SELL: Never just say "sell". Always show:
   - What you LOCK (₹ amount)
   - What you RISK giving back if you don't sell
   - The Locked ÷ Gave Back ratio (>50% = comfortable)

8. MORNING BRIEFING: If user says "good morning" or "briefing" or "what's happening":
   Use portfolio_snapshot tool, then summarize: positions needing action, 5MA alerts,
   any positions in sell zones, overall P&L.

═══ PORTFOLIO PARAMETERS ═══
  Portfolio: ₹5,00,00,000 (₹5Cr) | Max position: 20% = ₹1,00,00,000 (₹1Cr)
  1R = ₹4,00,000 (₹4L) | Default risk: 4% of entry price

═══ MATH FORMULAS ═══
  Extension % = (CMP - Entry) / Entry × 100
  R-Multiple = (CMP - Entry) / (Entry × risk% / 100)
  Position Value = CMP × Quantity
  Unrealised P&L (₹) = (CMP - Entry) × Quantity
  Risk per position (₹) = |Entry - SL| × Quantity
  PF Impact % = P&L ÷ 5,00,00,000 × 100
  SL from risk%: SL = Entry × (1 - risk%/100)
  Shares from value: Qty = Position Value ÷ Entry Price
  1R in ₹ = Entry × (risk%/100) × Quantity

═══ SELL ZONES (by regime) ═══
  Bull: first sell at 35% ext (~8.75R). Daily sells from 60%. Bucket cap 66%.
  Sideways: first at 35%, second at 50%. Reversal from 15%. Bucket cap 80%.
  Grind Down: first at 30%. Sell most in 30-40% zone. Bucket cap 66%.
  Sharp Downtrend: rapid sells at 2-4R (8-16% ext). Bucket cap 75%.

═══ TOOLS — 15 TOTAL ═══
You have full platform control. USE tools when user asks for action:
  CREATE: create_position
  READ: search_stock, get_live_price, refresh_prices, portfolio_snapshot
  UPDATE: edit_position, update_stop_loss, update_regime, record_sell
  LOG: add_journal_note
  CLOSE: close_position
  DELETE: delete_position (requires confirmation)
  MARKET DATA: query_screener (today's data), query_sectoral_screener (sector + MA filter), run_custom_scan (advanced scan: 52W/MAs/RS/liquidity), query_journal_stats (trading history), get_drawdown_analysis (DD analysis + market comparison), list_saved_scanners (saved presets), query_watchlist (watchlist stocks with sector/price data)

When cards are shown alongside your response, DON'T repeat the data — give INSIGHT only.

═══ ADVANCED INTELLIGENCE (apply these AUTOMATICALLY) ═══

9. SMART FOLLOW-UPS: At the END of every response, suggest 2-3 contextual next actions.
   Format as: "→ [action phrase]" on new lines.
   Example after showing VEDL at 35% extension:
     → Sell 30% at current price
     → Show tradeoff calculation
     → Log "not selling" with reason
   These should be the MOST LIKELY next things the user would want.

10. ANOMALY DETECTION: When you see data, flag anything unusual:
   - Volume 2x+ above 20-day avg = flag it
   - Position > 25% of portfolio = concentration warning
   - Two positions in same sector = correlation risk
   - Extension > 50% with 0% bucket sold = URGENT flag
   - No SL on any position = ALWAYS flag
   - R-multiple negative but no action taken = flag

11. TIME-AWARE INTELLIGENCE: Adapt behavior based on IST time:
   - 8:00-9:15 AM: Pre-market prep. Lead with "here's what needs attention today"
   - 9:15-3:30 PM: Market hours. Be action-oriented. Prices are LIVE. Use get_live_price.
   - 3:15-3:30 PM: CLOSING BELL. "Any defensive exits needed?" Check all 5MA positions.
   - After 3:30 PM: Review mode. "Here's how the day went." Compare morning plan vs reality.

12. POSITION SIZING BRAIN: When creating positions, ALWAYS check:
   - New position value vs portfolio (>20% = warn)
   - Sector concentration (2+ positions in same sector = warn)
   - Total capital deployed (>80% of 5Cr = warn)
   - Risk per position (>1R = ₹4L = warn)
   Calculate and state: "This would be X% of your portfolio, Y positions in [sector]"

13. COMPARATIVE ANALYSIS: When multiple positions exist, compare them:
   - "VEDL (+35%) is outperforming TATA (+3%) by 32% since entry"
   - "Your best performer is [X], worst is [Y]"
   - When asked "what should I sell first?" — rank by urgency with reasoning

14. PATTERN RECOGNITION: Read journal_entries across ALL positions:
   - "You've logged 'not selling' 3 times for VEDL at similar extensions. 2 of 3 times it pulled back. Consider selling this time."
   - "Your average hold time for winners is 45 days. VEDL is at day 60."
   - Track override patterns: when user ignores sell signals, what happened?

15. PORTFOLIO RISK SCORE: When asked about risk or doing morning briefing, calculate:
   Score 0-100 based on:
   - Positions with no SL (−20 each)
   - 5MA breaks not acted on (−15 each)
   - Extension > 50% with no sells (−10 each)
   - Total risk > 5% of PF (−10)
   - Concentration > 30% in one stock (−10)
   - All positions with SL + healthy 5MA (+10 each)
   Display as: "Portfolio Health: 72/100 — [main concern]"

16. SELL SEQUENCING: When multiple positions need selling, recommend ORDER:
   - 5MA breaks first (defensive exits)
   - Then highest extension with 0% bucket (overdue first sells)
   - Then daily sell territory (60%+)
   - "Sell VEDL first (5MA break), then trim BHEL (overdue at 42%)"

17. LEARNING LOG: After every tool execution that changes data, generate a 1-line log:
   Format: "[DATE] [ACTION] [STOCK] [DETAIL] [RESULT]"
   Example: "20-Mar-2026 SELL 30% VEDL at ₹700 → Bucket 30%, Locked ₹3.3L"
   This helps the user see a clean audit trail.

18. UNDO AWARENESS: If user says "undo" or "revert" or "that was a mistake":
   - For sells: note that sells cannot be auto-undone, but offer to adjust bucket_sold_pct manually
   - For SL changes: offer to revert to previous value
   - For regime changes: offer to change back
   - For position creation: offer to delete
   Always ask what specifically to undo.
"""

CATEGORY_PROMPTS = {
    "sell_decision": "Give the verdict in 1-2 sentences WITH actual numbers: P&L, extension%, R-multiple. What's most urgent and one recommendation.",
    "portfolio_overview": "Summarize in 1-2 sentences WITH actual numbers from tool data: position count, total P&L in ₹, any alerts.",
    "risk_check": "State risk in 1-2 sentences WITH actual numbers from tool data: total risk in ₹, as % of PF, which position has tightest SL.",
    "rankings": "Rank positions in 1-2 sentences WITH actual numbers from tool data.",
    "trailing_status": "State MA status in 1-2 sentences WITH actual numbers from tool data. Flag any 5MA breaks with the stock name and distance.",
    "single_stock": "Give the key stats in 1-2 sentences: CMP, P&L, extension, R-multiple, 5MA status. Then one opinion — trim or hold?",
    "greeting": "Respond naturally and warmly. If the user just says hi, respond with a brief friendly greeting. Do NOT recite portfolio stats unless they specifically ask about their portfolio.",
    "action_add": "If complete: 'Position ready — confirm below.' ONE sentence. If missing fields, ask what's needed.",
    "action_reconcile": "1 sentence: 'All in sync' or 'X mismatches found — details below.'",
    "journal_stats": "Use query_journal_stats tool. Give the exact stat asked for WITH the number.",
    "screener_query": "Use query_screener tool. Report: count + top 3-5 names with prices.",
    "general": "Answer the question directly and naturally. Use tools if needed to fetch data. Do NOT prepend portfolio stats unless relevant to the question.",
}

DETAILED_SYSTEM = """You are the Valvo AI Assistant — a trading intelligence system for Rohit, an institutional-level Indian equity portfolio manager managing ₹5 crores. You have been trained on his exact methodology through extensive case study work.

Respond naturally and thoroughly. Use your full knowledge of the trading system, MA-following, trailing methodology, and case studies. Be educational and collaborative.

Portfolio: ₹5Cr | Max position: 20% (₹1Cr) | 1R = ₹4L | Risk: 4% per trade
MA Colors: White = 5MA, Blue/Pink = 10MA, Orange/Yellow = 20MA

Key principles:
- Only candle CLOSE matters — wicks are irrelevant
- 5MA > 10MA > 20MA (ferocity hierarchy)
- Sell in pieces, max 66% into strength, 33% always trails 5MA
- Every sell shows both sides: upside given up vs mental volatility reduced
- Bull: first sell at 35% ext. Sideways: reversal detection from 15%. Sharp down: dump at 2-4R
"""



def _build_slim_context(structured_data):
    """Build a slim context string from the structured data for Claude's insight."""
    if not structured_data:
        return "\n\nNo structured data available. Use tools to fetch what you need.\n"

    dtype = structured_data.get("type", "")

    if dtype == "empty":
        return "\n\nLIVE DATA: No active positions in Position Manager. BUT trading history may exist in Journal — use query_journal_stats tool to check. Market data available via query_screener tool.\n"

    if dtype == "journal_stats":
        ctx = "\n\nJOURNAL DATA AVAILABLE:\n"
        ctx += f"  Total trades: {structured_data.get('total_trades', 0)} | Closed: {structured_data.get('closed_trades', 0)}\n"
        if structured_data.get("has_analytics"):
            ctx += f"  Win rate: {structured_data.get('win_rate', 0):.1f}% | Total P&L: ₹{structured_data.get('total_pnl', 0):,.0f}\n"
        ctx += "Use query_journal_stats tool for full details.\n"
        return ctx

    if dtype == "screener_query":
        return "\n\nSCREENER: Use query_screener tool to query real market data for 2,400+ NSE stocks.\n"

    ctx = "\n\nLIVE DATA SUMMARY:\n"
    dtype = structured_data["type"]

    if dtype == "action":
        action = structured_data.get("action", "")
        if action == "add_position":
            p = structured_data.get("parsed", {})
            complete = structured_data.get("complete", False)
            ctx += f"ACTION: Add Position\n"
            ctx += f"  Stock: {p.get('stock', '?')} | Price: {p.get('price', '?')} | Qty: {p.get('qty', '?')} | SL: {p.get('sl', 'auto')}\n"
            ctx += f"  Complete: {complete} | Missing: {structured_data.get('missing', [])}\n"
            ctx += "A confirmation card is shown to the user. Just acknowledge briefly.\n"
        elif action == "reconcile":
            ctx += f"ACTION: Reconciliation check\n"
            ctx += f"  PM active: {structured_data.get('pm_count', 0)} | Journal open: {structured_data.get('journal_count', 0)}\n"
            ctx += f"  In sync: {structured_data.get('in_sync', False)}\n"
            pm_only = structured_data.get("pm_only", [])
            nx_only = structured_data.get("journal_only", [])
            if pm_only:
                ctx += f"  Not journaled: {', '.join(p.get('symbol', p.get('name', '?')) for p in pm_only)}\n"
            if nx_only:
                ctx += f"  Not tracked: {', '.join(n.get('symbol', '?') for n in nx_only)}\n"
        return ctx

    if dtype == "actions":
        exits = [c for c in structured_data["cards"] if c["cls"] == "exit"]
        sells = [c for c in structured_data["cards"] if c["cls"] == "sell"]
        holds = [c for c in structured_data["cards"] if c["cls"] == "hold"]
        s = structured_data.get("summary", {})
        ctx += f"Positions: {s.get('count', 0)} | P&L: ₹{s.get('total_pnl', 0):,.0f} | Avg R: {s.get('avg_r', 0)}\n"
        ctx += f"Exits needed: {len(exits)} | Sells triggered: {len(sells)} | Holding: {len(holds)}\n"
        for c in exits + sells:
            ctx += f"  {c['name']}: {c['verdict']} — {c['detail']}\n"

    elif dtype == "positions":
        s = structured_data.get("summary", {})
        ctx += f"Positions: {s.get('count', 0)} | Total P&L: ₹{s.get('total_pnl', 0):,.0f} ({s.get('total_pnl_pct', 0)}%)\n"
        ctx += f"Avg R: {s.get('avg_r', 0)} | Green: {s.get('green', 0)} | Red: {s.get('red', 0)} | Above 2R: {s.get('above_2r', 0)}\n"
        for c in structured_data.get("cards", [])[:5]:
            ctx += f"  {c['name']}: {c['ext']:+.0f}% ext | {c['r']:.1f}R | 5MA: {c['defensive']}\n"

    elif dtype == "risk":
        for st in structured_data.get("stats", []):
            ctx += f"  {st['label']}: {st['val']} ({st['sub']})\n"
        for c in structured_data.get("cards", [])[:5]:
            ctx += f"  {c['name']}: Risk ₹{c['risk_rupees']:,.0f} | SL dist {c['sl_dist_pct']}% | {c['pos_pct']}% of PF\n"

    elif dtype == "rankings":
        for c in structured_data.get("cards", [])[:5]:
            ctx += f"  #{c['rank']} {c['name']}: {c['r']:.1f}R | {c['ext']:+.0f}% | {c['ma_grade']}\n"

    elif dtype == "trailing":
        alerts = [c for c in structured_data.get("cards", []) if c["action"] not in ("OK",)]
        ctx += f"Alerts: {structured_data.get('alerts', 0)} / {structured_data.get('total', 0)} positions\n"
        for c in alerts:
            ctx += f"  {c['name']}: {c['action']} — {c['detail']}\n"

    elif dtype == "single_stock":
        c = structured_data.get("card", {})
        ctx += f"{c.get('name', '?')}: Entry ₹{c.get('entry', 0)} → CMP ₹{c.get('cmp', 0)} | {c.get('ext', 0):+.0f}% | {c.get('r', 0):.1f}R\n"
        ctx += f"5MA: ₹{c.get('five_ma', 0)} ({c.get('defensive', '?')}) | SL: ₹{c.get('sl', 0)} | Bucket: {c.get('bucket_sold', 0)}%/{c.get('bucket_cap', 66)}%\n"
        ctx += f"MA: {c.get('ma_followed', '?')} ({c.get('ma_grade', '?')}) | Trail-worthy: {c.get('trail_worthy', False)} | First sell: {c.get('first_sell', False)}\n"
        for d in structured_data.get("daily_history", [])[:3]:
            ctx += f"  [{d['date']}: ₹{d['close']} 5MA={d['five_ma']} {d['r']}R {d['defensive']}]\n"

    return ctx


def _build_full_context(conn_override=None):
    """Build full context for detailed mode (existing behavior)."""
    conn = conn_override or _get_db()
    should_close = conn_override is None  # Only close if we opened it
    if not conn:
        return "\n\nLIVE DATA: Database unavailable.\n"

    ctx = "\n\nLIVE PORTFOLIO DATA:\n"
    try:
        cur = conn.cursor()

        # Active positions
        try:
            cur.execute("""
                SELECT stock_name, entry_price, stop_loss, quantity, position_value, one_r_value,
                       current_price, entry_extension_pct, current_r_multiple,
                       last_5ma_price, last_10ma_price, last_20ma_price,
                       defensive_status, grace_day_used, trailing_mode,
                       ma_followed, ma_grade, shakeout_count, qualifies_for_trailing,
                       market_regime, bucket_sold_pct, bucket_cap, first_sell_done,
                       sell_history, journal_entries, created_at
                FROM positions WHERE status = 'active' AND user_id = %s
                ORDER BY entry_extension_pct DESC NULLS LAST
            """, (g.user_id,))
            positions = cur.fetchall()
            if positions:
                ctx += f"\nActive Positions ({len(positions)}):\n"
                for p in positions:
                    ext = p.get("entry_extension_pct") or 0
                    r = p.get("current_r_multiple") or 0
                    ctx += f"  {p['stock_name']}: Entry ₹{p['entry_price']} CMP ₹{p.get('current_price','?')} | {ext:+.1f}% | {r:.1f}R"
                    ctx += f" | 5MA ₹{p.get('last_5ma_price','?')} ({p.get('defensive_status','?')})"
                    ctx += f" | {p.get('ma_followed','?')} ({p.get('ma_grade','?')})"
                    ctx += f" | Bucket {p.get('bucket_sold_pct',0):.0f}%/{p.get('bucket_cap',66):.0f}%"
                    ctx += f" | Regime: {p.get('market_regime','?')}\n"
        except Exception as e:
            ctx += f"\nPositions error: {e}\n"

        # Portfolio summary
        try:
            cur.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(position_value),0) as total_val FROM positions WHERE status = 'active' AND user_id = %s", (g.user_id,))
            sm = cur.fetchone()
            invested = sm.get("total_val") or 0
            ctx += f"\nSummary: {sm.get('cnt',0)} active | Invested ₹{invested:,.0f} | Cash ~₹{50000000-invested:,.0f}\n"
        except Exception as e:
            print(f"[chat] portfolio summary failed: {e}")

        return ctx
    except Exception as e:
        return f"\n\nLIVE DATA ERROR: {e}\n"
    finally:
        if should_close:
            try:
                conn.close()
            except:
                pass


# ═══════════════════════════════════════════════════════════════════
# CHAT HISTORY
# ═══════════════════════════════════════════════════════════════════

def _get_chat_history(limit=20):
    conn = _get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT role, content FROM chat_messages WHERE user_id = %s ORDER BY created_at DESC LIMIT %s", (g.user_id, limit,))
        rows = cur.fetchall()
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    except Exception as e:
        print(f"[chat] get_chat_history failed: {e}")
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _save_message(role, content, page_context=None, stock_context=None):
    conn = _get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (role, content, page_context, stock_context, user_id) VALUES (%s, %s, %s, %s, %s)",
            (role, content, page_context, stock_context, g.user_id),
        )
        conn.commit()
    except:
        try:
            conn.rollback()
        except:
            pass
    finally:
        try:
            conn.close()
        except:
            pass


# ═══════════════════════════════════════════════════════════════════
# CLAUDE TOOLS — actions the AI can execute
# ═══════════════════════════════════════════════════════════════════

AI_TOOLS = [
    {
        "name": "update_stop_loss",
        "description": "Update stop loss for a position. Use for: move SL, trail SL, set SL to cost, set SL to 5MA price, custom SL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string", "description": "Stock name or ticker"},
                "new_stop_loss": {"type": "number", "description": "New SL price in ₹"},
                "trailing_mode": {"type": "string", "enum": ["original", "cost", "5ma", "custom"], "description": "Trailing type"}
            },
            "required": ["stock_name", "new_stop_loss"]
        }
    },
    {
        "name": "record_sell",
        "description": "Record a partial sell. Use when user wants to book profit, sell percentage, trim position.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string"},
                "sell_pct": {"type": "number", "description": "% of position to sell (e.g. 30)"},
                "sell_price": {"type": "number", "description": "Sell price in ₹"},
                "trigger": {"type": "string", "enum": ["extension_milestone", "reversal_candle", "5ma_break", "manual", "r_multiple"]}
            },
            "required": ["stock_name", "sell_pct", "sell_price"]
        }
    },
    {
        "name": "add_journal_note",
        "description": "Add journal entry (not selling today, observation, note). Use for any decision logging.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string"},
                "reason": {"type": "string", "description": "The journal note / reason"}
            },
            "required": ["stock_name", "reason"]
        }
    },
    {
        "name": "create_position",
        "description": "Create a new position in Position Manager. Use when user says 'add position', 'bought', 'entered', 'log trade'. Search stock first with search_stock if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string", "description": "Full stock name"},
                "security_id": {"type": "string", "description": "Security ID (get from search_stock)"},
                "entry_price": {"type": "number"},
                "quantity": {"type": "integer"},
                "stop_loss": {"type": "number", "description": "SL price. If not given, auto-set to entry × 0.96"},
                "market_regime": {"type": "string", "enum": ["bull", "sideways", "grind_down", "sharp_downtrend"]},
                "risk_pct": {"type": "number", "description": "Risk % (default 4)"}
            },
            "required": ["stock_name", "entry_price", "quantity"]
        }
    },
    {
        "name": "edit_position",
        "description": "Edit ANY field on an existing position. Use for: change entry price, update quantity, set MA grade, update regime, change any parameter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string"},
                "fields": {"type": "object", "description": "Key-value pairs of fields to update. Valid fields: entry_price, stop_loss, quantity, market_regime, ma_followed, ma_grade, shakeout_count, qualifies_for_trailing, bucket_sold_pct, bucket_cap, first_sell_done, trailing_mode, defensive_status"}
            },
            "required": ["stock_name", "fields"]
        }
    },
    {
        "name": "close_position",
        "description": "Close/exit a position completely. Records exit price and calculates final P&L.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string"},
                "exit_price": {"type": "number", "description": "Exit price. If not given, uses current CMP."}
            },
            "required": ["stock_name"]
        }
    },
    {
        "name": "search_stock",
        "description": "Search for a stock by name or ticker. Returns security_id, symbol, company name. Use BEFORE create_position to get the correct security_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Stock name or ticker to search"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "get_live_price",
        "description": "Get live CMP + OHLC + MAs for any stock by security_id or name. Use to verify prices, check current levels, validate entry prices.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string", "description": "Stock name (searches positions first, then stock_universe)"},
                "security_id": {"type": "string", "description": "Direct security ID if known"}
            }
        }
    },
    {
        "name": "refresh_prices",
        "description": "Refresh live prices for all positions or a specific one.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string", "description": "Optional: specific stock. Empty = all."}
            }
        }
    },
    {
        "name": "update_regime",
        "description": "Change market regime for a position. Auto-updates sell zones, bucket cap, and all regime parameters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string"},
                "regime": {"type": "string", "enum": ["bull", "sideways", "grind_down", "sharp_downtrend"]}
            },
            "required": ["stock_name", "regime"]
        }
    },
    {
        "name": "delete_position",
        "description": "Permanently delete a position from the database. ALWAYS confirm with user before using this. Say 'Are you sure you want to delete [stock]? This cannot be undone.' and only proceed if they confirm.",
        "input_schema": {
            "type": "object",
            "properties": {
                "stock_name": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "Must be true. If user hasn't explicitly confirmed, ask first."}
            },
            "required": ["stock_name", "confirmed"]
        }
    },
    {
        "name": "portfolio_snapshot",
        "description": "Get a complete snapshot of all active positions with live data. Use for portfolio overview, what-if analysis, morning briefing, or when user asks about overall portfolio health.",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_closed": {"type": "boolean", "description": "Include closed positions too. Default false."}
            }
        }
    },
    {
        "name": "query_screener",
        "description": "Query screener stocks from the market database. Can filter by liquidity, % change, price range, or any combination. Use when user asks about screener results, liquidity filters, strongest movers, stocks above/below certain criteria. Returns matching stocks with data + a navigation action to open the screener filtered. Also computes 20-day SMA liquidity from candles_daily.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_liquidity_cr": {"type": "number", "description": "Minimum 20-day SMA liquidity in Crores. E.g. 200 for >₹200Cr/day"},
                "max_liquidity_cr": {"type": "number", "description": "Maximum 20-day SMA liquidity in Crores"},
                "min_change_pct": {"type": "number", "description": "Minimum % change today. E.g. 4 for stocks up ≥4%"},
                "max_change_pct": {"type": "number", "description": "Maximum % change today. E.g. -2 for stocks down ≤-2%"},
                "min_price": {"type": "number", "description": "Minimum close price"},
                "max_price": {"type": "number", "description": "Maximum close price"},
                "sort_by": {"type": "string", "description": "Sort field: 'liquidity', 'change', 'price', 'volume'. Default: liquidity desc"},
                "limit": {"type": "integer", "description": "Max results to return. Default 20"}
            }
        }
    },
    {
        "name": "query_journal_stats",
        "description": "Get historical trading statistics from the Journal — win rate, avg winner%, avg loser%, total P&L, trade count, monthly breakdown, and individual trade details. Use when user asks about their stats, performance, track record, average winner, win rate, best trade, worst trade, P&L history, or any historical trading data. This is the user's complete trading history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_trades": {"type": "boolean", "description": "Include individual trade list. Default true."},
                "status_filter": {"type": "string", "enum": ["all", "open", "closed"], "description": "Filter by trade status. Default 'all'."},
                "include_monthly": {"type": "boolean", "description": "Include monthly P&L breakdown. Default true."}
            }
        }
    },
    {
        "name": "query_sectoral_screener",
        "description": "Query stocks within a specific NSE sector/index and filter by MA conditions. Use when user asks about stocks in a sector (defence, metal, pharma, IT, bank, auto, etc.) that are above or below a moving average (20MA, 50MA, 200MA). Looks up index_constituents joined with real candle data. Examples: 'defence stocks above 50MA', 'how many metal stocks below 200MA', 'list pharma stocks above 20MA'. Sector name is a plain English name like 'defence', 'metal', 'IT', 'pharma', 'bank', 'auto', 'realty', 'energy', 'FMCG', 'infra', 'PSU bank', 'EV', 'housing', etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sector": {"type": "string", "description": "Sector name in plain English. E.g. 'defence', 'metal', 'IT', 'pharma', 'bank', 'auto', 'realty', 'energy', 'FMCG', 'infra', 'PSU bank', 'EV', 'housing', 'midcap', 'smallcap'"},
                "ma_period": {"type": "integer", "description": "Moving average period. Common: 20, 50, 200. Default 20."},
                "condition": {"type": "string", "enum": ["above", "below"], "description": "Filter stocks above or below the MA. Default 'above'."},
                "include_list": {"type": "boolean", "description": "Whether to include the full stock list in the response. Default true."}
            },
            "required": ["sector"]
        }
    },
    {
        "name": "query_trade_analytics",
        "description": "Query trade analytics for any FY. Returns stats like total trades, win rate, P&L, profit factor, best/worst trade, monthly breakdown. Use when user asks about performance, stats, 'how did I do in FY 24-25', 'what was my win rate', 'show me my FY stats'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fy": {"type": "string", "description": "Financial year: '2023-24', '2024-25', '2025-26', '2026-27', or 'all'. Default current FY.", "enum": ["2023-24", "2024-25", "2025-26", "2026-27", "all"]},
            },
            "required": ["fy"]
        }
    },
    {
        "name": "create_journal_trade",
        "description": "Create a trade entry in the Valvo Journal. Use when user says 'log trade in journal', 'add journal entry for X', 'I bought X at Y with SL Z'. This also auto-links to Position Manager if a matching position exists.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock symbol/name (e.g. 'TATAMOTORS', 'HINDALCO')"},
                "entry_price": {"type": "number", "description": "Entry price in ₹"},
                "sl": {"type": "number", "description": "Stop loss price in ₹"},
                "initial_qty": {"type": "integer", "description": "Quantity bought"},
                "entry_type": {"type": "string", "enum": ["BREAKOUT", "ANTICIPATION"], "description": "Entry type. Default BREAKOUT."},
                "trade_date": {"type": "string", "description": "Trade date in YYYY-MM-DD format. Default today."},
                "security_id": {"type": "string", "description": "Security ID if known"},
                "sector": {"type": "string", "description": "Sector name"},
            },
            "required": ["symbol", "entry_price", "initial_qty"]
        }
    },
    {
        "name": "get_fy_summary",
        "description": "Get a quick summary of any FY: total trades, win rate, total P&L, return %, best month, worst month. Use for quick comparisons like 'compare FY 24-25 vs 25-26' or 'which FY was best'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fy": {"type": "string", "description": "Financial year", "enum": ["2023-24", "2024-25", "2025-26", "2026-27", "all"]},
            },
            "required": ["fy"]
        }
    },
    {
        "name": "scan_universe",
        "description": "Scan the entire NSE universe for top gainers/losers over any time period (1-200 days). Can filter by sector. Use when user asks 'top gainers this week' (days=5), 'top gainers this month' (days=20), 'top gainers this quarter' (days=60), 'biggest losers this month', 'strongest momentum stocks', 'top metal sector gainers in 20 days', 'what rallied most in 3 months'. Scans 2400+ stocks from candles_daily.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Lookback period in trading days. E.g. 5, 10, 20, 40, 60, 100, 200."},
                "direction": {"type": "string", "enum": ["gainers", "losers", "both"], "description": "Top gainers, losers, or both. Default 'gainers'."},
                "limit": {"type": "integer", "description": "Number of results. Default 10, max 50."},
                "min_price": {"type": "number", "description": "Minimum current price filter (skip penny stocks). Default 50."},
                "min_volume": {"type": "number", "description": "Minimum avg daily volume. Default 100000."},
                "sector": {"type": "string", "description": "Filter by sector/index (e.g. 'metal', 'defence', 'IT', 'pharma', 'auto'). Uses index_constituents to filter."}
            },
            "required": ["days"]
        }
    },
    {
        "name": "query_scoring_history",
        "description": "Get scored stocks from the Scoring Engine with full parameter breakdown. Use for: 'what did VEDL score', 'VEDL linearity score', 'all stocks above 70', 'scored stocks by sector', 'average score of traded stocks', 'weakest parameter', 'score distribution'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Filter by stock symbol. Omit for recent scores."},
                "limit": {"type": "integer", "description": "Number of results. Default 10."},
                "min_score": {"type": "number", "description": "Min final score filter (e.g. 70)"},
                "max_score": {"type": "number", "description": "Max score filter"},
                "setup_type": {"type": "string", "enum": ["OOB", "LARGE_BASE"], "description": "Filter by setup type"},
                "full_breakdown": {"type": "boolean", "description": "Include all individual parameter scores (linearity, symmetry, etc). Default false."},
                "stats_mode": {"type": "boolean", "description": "Return aggregate stats instead of individual scores. Use for 'average score', 'weakest parameter', 'score distribution'."}
            }
        }
    },
    {
        "name": "query_sector_performance",
        "description": "Get performance of NSE sectoral indices over any period. Use when user asks 'how is Nifty Metal doing', 'best performing sector this month', 'sector performance', 'which sectors are leading'. Uses candles_indices data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Lookback period in days. Default 20."},
                "sector": {"type": "string", "description": "Specific sector name. Omit for all sectors ranked."}
            }
        }
    },
    {
        "name": "query_monthly_pl",
        "description": "Get monthly P&L data for any FY. Returns each month's return %, trades, win rate. Use when user asks 'best month in FY 25-26', 'monthly breakdown', 'worst trading month'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fy": {"type": "string", "description": "Financial year", "enum": ["2023-24", "2024-25", "2025-26"]}
            },
            "required": ["fy"]
        }
    },
    {
        "name": "query_market_regime",
        "description": "Get current market regime and regime history. Use when user asks 'what regime are we in', 'market regime history', 'when did we enter sideways'.",
        "input_schema": { "type": "object", "properties": {} }
    },
    {
        "name": "search_trades",
        "description": "Search trades by symbol, amount, move%, month, or sector across ALL FYs (780+ trades). Use for: 'how many times did I trade VEDL', 'biggest winner ever', 'trades in metal sector', 'trades > ₹5L profit', 'all December trades', 'most traded stocks', 'repeat trades'. The most powerful trade lookup tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock symbol filter (partial match OK). E.g. 'VEDL', 'TATA', 'IRFC'"},
                "fy": {"type": "string", "description": "FY filter or 'all'. Default 'all'.", "enum": ["2023-24", "2024-25", "2025-26", "2026-27", "all"]},
                "min_pl": {"type": "number", "description": "Min P&L in ₹ (e.g. 100000 for > ₹1L profit)"},
                "max_pl": {"type": "number", "description": "Max P&L in ₹ (e.g. -50000 for losses > ₹50K)"},
                "min_move_pct": {"type": "number", "description": "Min move % (e.g. 10 for > 10% moves)"},
                "max_move_pct": {"type": "number", "description": "Max move % (e.g. -3 for > 3% losses)"},
                "month_label": {"type": "string", "description": "Month filter e.g. 'December 2025', 'January 2024'"},
                "winners_only": {"type": "boolean", "description": "Only winning trades"},
                "losers_only": {"type": "boolean", "description": "Only losing trades"},
                "sort_by": {"type": "string", "enum": ["pl_desc", "pl_asc", "move_desc", "move_asc", "symbol"], "description": "Sort order. Default pl_desc."},
                "limit": {"type": "integer", "description": "Max results. Default 20, max 50."},
                "group_by_symbol": {"type": "boolean", "description": "Group results by symbol with per-symbol stats. Use for 'most traded stocks', 'repeat trades'."}
            }
        }
    },
    {
        "name": "query_stock_data",
        "description": "Get historical price data, 52-week high/low, ADR, volume analysis, returns over any period for ANY stock. Use for: 'VEDL 52W high', 'VEDL price on March 15', 'VEDL ADR', 'stocks making new 52W highs', 'VEDL monthly returns'. The most powerful market data tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock symbol or name. Required for single-stock queries."},
                "security_id": {"type": "string", "description": "Security ID if known (faster lookup)"},
                "query_type": {"type": "string", "enum": [
                    "price_at_date", "52w_high_low", "returns", "adr", "volume_analysis",
                    "ma_status", "new_52w_highs", "new_52w_lows", "near_52w_high", "breakout_scan"
                ], "description": "Type of query"},
                "date": {"type": "string", "description": "Date for price_at_date (YYYY-MM-DD)"},
                "days": {"type": "integer", "description": "Lookback days for returns/adr/volume. Default 20."},
                "threshold_pct": {"type": "number", "description": "% threshold for near_52w_high (default 5 = within 5%)"}
            },
            "required": ["query_type"]
        }
    },
    {
        "name": "query_positions_advanced",
        "description": "Advanced position queries: closed positions with P&L, risk analysis, danger zone (near SL), sorting, concentration analysis. Use for: 'show closed positions', 'total realized P&L', 'positions near SL', 'portfolio risk', 'best R-multiple', 'sector concentration'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query_type": {"type": "string", "enum": [
                    "closed_positions", "risk_summary", "danger_zone", "concentration",
                    "sort_active", "sell_system_status", "ma_breakdown"
                ], "description": "Type of query"},
                "sort_by": {"type": "string", "enum": ["pnl", "r_multiple", "extension", "value", "risk", "created_at"], "description": "Sort field for closed/active"},
                "limit": {"type": "integer", "description": "Max results. Default 20."}
            },
            "required": ["query_type"]
        }
    },
    {
        "name": "query_advanced_stats",
        "description": "Compute advanced trading statistics: streaks, drawdown, Sharpe ratio, expectancy, holding period, CAGR, consistency. Use for: 'longest winning streak', 'worst drawdown', 'Sharpe ratio', 'expectancy', 'average holding period', 'am I improving'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fy": {"type": "string", "description": "FY filter or 'all'. Default 'all'.", "enum": ["2023-24", "2024-25", "2025-26", "all"]},
                "stat_type": {"type": "string", "enum": [
                    "streaks", "drawdown", "sharpe", "expectancy", "holding_period",
                    "consistency", "all_stats", "improvement_trend"
                ], "description": "Specific stat or 'all_stats' for comprehensive. Default all_stats."}
            }
        }
    },
    {
        "name": "query_behavior_analysis",
        "description": "Analyze trading behavior patterns from journal trades. Use for: 'breakout vs anticipation win rate', 'plan followed %', '5-star trades performance', 'exit trigger breakdown', 'best performing setup', 'do I trade better in bull or sideways'. Reads journal_trades for self_rating, plan_followed, entry_type, exit_trigger, setup, base_duration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "analysis_type": {"type": "string", "enum": [
                    "entry_type_comparison", "rating_analysis", "plan_followed",
                    "exit_trigger_breakdown", "setup_analysis", "full_behavior"
                ], "description": "Type of analysis. Default 'full_behavior'."}
            }
        }
    },
    {
        "name": "query_sector_deep",
        "description": "Deep sector analysis: market breadth, leading sectors, MA status per sector, sector rotation. Use for: 'market breadth', '% of stocks above 200MA', 'which sectors leading', 'sector rotation', 'breadth by sector'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query_type": {"type": "string", "enum": [
                    "market_breadth", "leading_sectors", "sector_breadth", "sector_rotation"
                ], "description": "Type of analysis"},
                "ma_period": {"type": "integer", "description": "MA period for breadth. Default 20."},
                "days": {"type": "integer", "description": "Lookback for rotation analysis. Default 20."}
            },
            "required": ["query_type"]
        }
    },
    {
        "name": "query_cross_intelligence",
        "description": "Cross-table intelligence: join scoring+trades+positions for correlation analysis. Use for: 'scored high and traded and won', 'score-to-P&L correlation', 'which scoring parameter predicts success', 'regime impact on performance'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query_type": {"type": "string", "enum": [
                    "score_vs_outcome", "scored_and_traded", "regime_performance",
                    "position_size_vs_outcome", "repeat_trade_improvement"
                ], "description": "Type of cross-table analysis"}
            },
            "required": ["query_type"]
        }
    },
    {
        "name": "query_fund_management",
        "description": "Capital flow tracking: additions, withdrawals, running capital, annualized return, NAV equivalent. Use for: 'capital added this FY', 'total withdrawals', 'running capital', 'annualized return', 'projected capital'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Calendar year for fund months. Default current year."}
            }
        }
    },
    {
        "name": "daily_briefing",
        "description": "Generate a comprehensive daily/weekly/monthly briefing. Combines portfolio status, market regime, sector performance, watchlist alerts, and recent trades. Use for: 'morning briefing', 'daily summary', 'weekly recap', 'how am I doing', 'what should I focus on today'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["daily", "weekly", "monthly"], "description": "Briefing scope. Default daily."}
            }
        }
    },
    {
        "name": "run_custom_scan",
        "description": "Run the custom scanner across 2,400+ NSE stocks with precise filters. Computes 52W high/low proximity, MA positions (5/10/20/50/200), relative strength vs Smallcap 100 (1W/3M/6M), liquidity, ADR, sector, and market cap. Use when user asks to SCAN or SCREEN for stocks matching conditions. Examples: 'scan for stocks within 10% of 52W high above 20MA', 'find stocks that outperformed smallcap 100 in last 3 months', 'scan breakout candidates near highs with strong RS', 'liquid stocks above all MAs', 'stocks in leading sectors above 50MA'. Returns matching stocks with ALL computed metrics.",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_pct_from_high": {"type": "number", "description": "Within X% of 52-week high. E.g. 10 means within 10% of high."},
                "min_pct_from_low": {"type": "number", "description": "Moved at least X% from 52-week low. E.g. 40 means up 40%+ from low."},
                "above_ma5": {"type": "boolean", "description": "Stock must be above 5-day MA"},
                "above_ma10": {"type": "boolean", "description": "Stock must be above 10-day MA"},
                "above_ma20": {"type": "boolean", "description": "Stock must be above 20-day MA"},
                "above_ma50": {"type": "boolean", "description": "Stock must be above 50-day MA"},
                "above_ma200": {"type": "boolean", "description": "Stock must be above 200-day MA"},
                "min_liquidity": {"type": "number", "description": "Minimum 20-day avg turnover in Crores. Default 1."},
                "min_mcap": {"type": "number", "description": "Minimum market cap estimate in Crores"},
                "max_mcap": {"type": "number", "description": "Maximum market cap estimate in Crores"},
                "rs_1w_positive": {"type": "boolean", "description": "Stock outperformed Smallcap 100 in last 1 week"},
                "rs_3m_positive": {"type": "boolean", "description": "Stock outperformed Smallcap 100 in last 3 months"},
                "rs_6m_positive": {"type": "boolean", "description": "Stock outperformed Smallcap 100 in last 6 months"},
                "leading_sectors_only": {"type": "boolean", "description": "Only stocks in sectors where the sector index is above its 20MA"},
                "sort_by": {"type": "string", "enum": ["liq_cr", "pct_from_high", "pct_from_low", "rs_1w", "rs_3m", "rs_6m", "adr", "cmp"], "description": "Sort results by. Default liq_cr."},
                "limit": {"type": "integer", "description": "Max results. Default 20."}
            }
        }
    },
    {
        "name": "list_saved_scanners",
        "description": "List all saved scanner presets with their filter configurations and run history. Use when user asks 'show my scanners', 'what scanners do I have', 'my saved scans'. Returns preset names, filters, last run count, and whether pinned.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "query_watchlist",
        "description": "Query the user's watchlists and their stocks. Use when user asks about watchlist, 'what's on my watchlist', 'watchlist stocks', 'show my watchlists', 'which sectors in my watchlist', 'watchlist movers today'. Returns all watchlists with enriched stock data including CMP, change%, sector, industry, 52W range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "watchlist_name": {"type": "string", "description": "Filter by specific watchlist name. Omit to get all watchlists."}
            }
        }
    },
    {
        "name": "get_drawdown_analysis",
        "description": "Get comprehensive drawdown analysis across all FYs. Returns: monthly portfolio values, drawdown periods with contributing trades, Smallcap 100 comparison, recovery metrics, and behavioral insights. Use when user asks about drawdowns, worst periods, recovery speed, market comparison, 'what caused my drawdown', 'how long did it take to recover', 'my worst months'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary_only": {"type": "boolean", "description": "Only return summary stats, not full timeline. Default false."}
            }
        }
    },
]


def _find_position_by_name(stock_name):
    """Find position ID by stock name (fuzzy match)."""
    conn = _get_db()
    if not conn:
        return None, "Database unavailable"
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, stock_name, security_id FROM positions WHERE status = 'active' AND user_id = %s", (g.user_id,))
        rows = cur.fetchall()
        name_lower = stock_name.lower()
        for r in rows:
            if name_lower in r["stock_name"].lower() or r["stock_name"].lower() in name_lower:
                return r, None
        return None, f"No active position found matching '{stock_name}'"
    except Exception as e:
        return None, str(e)
    finally:
        try:
            conn.close()
        except:
            pass


def _execute_tool(tool_name, tool_input):
    """Execute a tool and return the result string."""
    try:
        if tool_name == "update_stop_loss":
            pos, err = _find_position_by_name(tool_input["stock_name"])
            if err:
                return f"Error: {err}"
            import requests
            # Use internal function call instead of HTTP
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                new_sl = tool_input["new_stop_loss"]
                mode = tool_input.get("trailing_mode", "custom")
                # When trailing, update trailing_sl (not stop_loss which is original risk)
                if mode in ("cost", "5ma", "10ma", "20ma", "custom"):
                    cur.execute("UPDATE positions SET trailing_sl = %s, trailing_mode = %s, updated_at = NOW() WHERE id = %s",
                               (new_sl, mode, pos["id"]))
                else:
                    cur.execute("UPDATE positions SET stop_loss = %s, trailing_mode = %s, updated_at = NOW() WHERE id = %s",
                               (new_sl, mode, pos["id"]))
                conn.commit()
                return f"✅ Stop loss for {pos['stock_name']} updated to ₹{new_sl} (mode: {mode})"
            finally:
                conn.close()

        elif tool_name == "record_sell":
            pos, err = _find_position_by_name(tool_input["stock_name"])
            if err:
                return f"Error: {err}"
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                sell_pct = tool_input["sell_pct"]
                sell_price = tool_input["sell_price"]
                trigger = tool_input.get("trigger", "manual")

                # Get current sell history
                cur.execute("SELECT sell_history, bucket_sold_pct, entry_price, first_sell_done FROM positions WHERE id = %s", (pos["id"],))
                p = cur.fetchone()
                sells = p.get("sell_history") or []
                if isinstance(sells, str):
                    sells = json.loads(sells)
                bucket = float(p.get("bucket_sold_pct") or 0)
                entry = float(p.get("entry_price") or 0)
                ext = round((sell_price - entry) / entry * 100, 1) if entry else 0
                r_mult = round((sell_price - entry) / (entry * 0.04), 1) if entry else 0

                # Add sell record
                from datetime import datetime, timezone, timedelta
                IST = timezone(timedelta(hours=5, minutes=30))
                sells.append({
                    "date": datetime.now(IST).strftime("%Y-%m-%d"),
                    "pct": sell_pct, "price": sell_price,
                    "extension": ext, "trigger": trigger,
                    "r_multiple": r_mult,
                })
                new_bucket = min(bucket + sell_pct, 100)

                cur.execute("""UPDATE positions SET sell_history = %s, bucket_sold_pct = %s,
                              first_sell_done = TRUE, updated_at = NOW() WHERE id = %s""",
                           (json.dumps(sells), new_bucket, pos["id"]))
                conn.commit()
                return f"✅ Recorded sell: {sell_pct}% of {pos['stock_name']} at ₹{sell_price} ({trigger}). Bucket now {new_bucket:.0f}%."
            finally:
                conn.close()

        elif tool_name == "add_journal_note":
            pos, err = _find_position_by_name(tool_input["stock_name"])
            if err:
                return f"Error: {err}"
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                cur.execute("SELECT journal_entries, entry_extension_pct, current_r_multiple, bucket_sold_pct FROM positions WHERE id = %s", (pos["id"],))
                p = cur.fetchone()
                entries = p.get("journal_entries") or []
                if isinstance(entries, str):
                    entries = json.loads(entries)

                from datetime import datetime, timezone, timedelta
                IST = timezone(timedelta(hours=5, minutes=30))
                entries.append({
                    "date": datetime.now(IST).strftime("%Y-%m-%d"),
                    "action": "not_selling",
                    "reason": tool_input["reason"],
                    "extension": float(p.get("entry_extension_pct") or 0),
                    "r_multiple": float(p.get("current_r_multiple") or 0),
                    "bucket_status": float(p.get("bucket_sold_pct") or 0),
                })
                cur.execute("UPDATE positions SET journal_entries = %s, updated_at = NOW() WHERE id = %s",
                           (json.dumps(entries), pos["id"]))
                conn.commit()
                return f"✅ Journal entry added for {pos['stock_name']}: '{tool_input['reason']}'"
            finally:
                conn.close()

        elif tool_name == "refresh_prices":
            stock_name = tool_input.get("stock_name", "")
            if stock_name:
                pos, err = _find_position_by_name(stock_name)
                if err:
                    return f"Error: {err}"
                try:
                    from services.market_data_service import refresh_position_data
                    entry = 0
                    risk = 4
                    conn = _get_db()
                    if conn:
                        try:
                            cur = conn.cursor()
                            cur.execute("SELECT entry_price, risk_pct, security_id FROM positions WHERE id = %s", (pos["id"],))
                            p = cur.fetchone()
                            entry = float(p.get("entry_price") or 0)
                            risk = float(p.get("risk_pct") or 4)
                            sec_id = str(p.get("security_id") or "")
                        finally:
                            conn.close()
                    if sec_id:
                        data = refresh_position_data(sec_id, entry, risk)
                        if data:
                            conn2 = _get_db()
                            if conn2:
                                try:
                                    cur2 = conn2.cursor()
                                    cur2.execute("""UPDATE positions SET current_price=%s, five_ma=%s, ten_ma=%s, twenty_ma=%s,
                                        current_r_multiple=%s, entry_extension_pct=%s, updated_at=NOW() WHERE id=%s""",
                                        (data.get("current_price"), data.get("five_ma"), data.get("ten_ma"), data.get("twenty_ma"),
                                         data.get("r_multiple"), data.get("entry_extension_pct"), pos["id"]))
                                    conn2.commit()
                                finally:
                                    conn2.close()
                            return f"✅ {pos['stock_name']}: CMP ₹{data.get('current_price', '?')} | 5MA ₹{data.get('five_ma', '?')} | R:{data.get('r_multiple', '?')} | Ext:{data.get('entry_extension_pct', '?')}%"
                    return f"Could not refresh {pos['stock_name']} — no security ID"
                except Exception as e:
                    return f"Refresh error: {e}"
            else:
                # Refresh all — iterate active positions
                conn = _get_db()
                if not conn:
                    return "Database unavailable"
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT id, stock_name, security_id, entry_price, risk_pct FROM positions WHERE status = 'active' AND security_id IS NOT NULL AND user_id = %s", (g.user_id,))
                    rows = cur.fetchall()
                    count = 0
                    from services.market_data_service import refresh_position_data
                    for r in rows:
                        try:
                            data = refresh_position_data(str(r["security_id"]), float(r.get("entry_price") or 0), float(r.get("risk_pct") or 4))
                            if data and data.get("current_price"):
                                cur.execute("""UPDATE positions SET current_price=%s, five_ma=%s, ten_ma=%s, twenty_ma=%s,
                                    current_r_multiple=%s, entry_extension_pct=%s, updated_at=NOW() WHERE id=%s""",
                                    (data.get("current_price"), data.get("five_ma"), data.get("ten_ma"), data.get("twenty_ma"),
                                     data.get("r_multiple"), data.get("entry_extension_pct"), r["id"]))
                                count += 1
                        except:
                            pass
                    conn.commit()
                    return f"✅ Refreshed {count}/{len(rows)} positions"
                finally:
                    conn.close()

        elif tool_name == "update_regime":
            pos, err = _find_position_by_name(tool_input["stock_name"])
            if err:
                return f"Error: {err}"
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                regime = tool_input["regime"]
                # Apply regime defaults
                regime_params = {
                    "bull": (35.0, 66.0, 33.0, 35.0, 60.0),
                    "sideways": (35.0, 80.0, 20.0, 15.0, 50.0),
                    "grind_down": (30.0, 66.0, 10.0, 30.0, 40.0),
                    "sharp_downtrend": (8.0, 75.0, 25.0, 8.0, 15.0),
                }
                fsz, cap, mintr, rdf, dsf = regime_params.get(regime, regime_params["bull"])
                cur.execute("""UPDATE positions SET market_regime = %s, first_sell_zone = %s,
                              bucket_cap = %s, min_trail_pct = %s, reversal_detect_from = %s,
                              daily_sell_from = %s, updated_at = NOW() WHERE id = %s""",
                           (regime, fsz, cap, mintr, rdf, dsf, pos["id"]))
                conn.commit()
                return f"✅ Regime for {pos['stock_name']} changed to {regime}. Sell zone: {fsz}%, bucket cap: {cap}%."
            finally:
                conn.close()

        elif tool_name == "create_position":
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                name = tool_input["stock_name"]
                entry = float(tool_input["entry_price"])
                qty = int(tool_input["quantity"])
                risk_pct = float(tool_input.get("risk_pct", 4))
                sl = float(tool_input.get("stop_loss") or round(entry * (1 - risk_pct / 100), 2))
                regime = tool_input.get("market_regime", "bull")
                sec_id = tool_input.get("security_id", "")
                pos_val = round(entry * qty)
                one_r = round(pos_val * (risk_pct / 100))

                # Regime params
                regime_params = {
                    "bull": {"fsz": 35, "msp": 66, "mtp": 33, "rdf": 35, "dsf": 60, "bc": 66},
                    "sideways": {"fsz": 35, "msp": 80, "mtp": 20, "rdf": 15, "dsf": 50, "bc": 80},
                    "grind_down": {"fsz": 30, "msp": 66, "mtp": 10, "rdf": 30, "dsf": 40, "bc": 66},
                    "sharp_downtrend": {"fsz": 8, "msp": 75, "mtp": 25, "rdf": 8, "dsf": 15, "bc": 75},
                }
                rp = regime_params.get(regime, regime_params["bull"])

                # Check for duplicates
                cur.execute("SELECT id FROM positions WHERE stock_name = %s AND status = 'active' AND user_id = %s", (name, g.user_id))
                if cur.fetchone():
                    return f"⚠ Position for {name} already exists as active. Use edit_position to modify it."

                cur.execute("""INSERT INTO positions
                    (stock_name, entry_price, stop_loss, quantity, position_value,
                     one_r_value, risk_pct, source, market_regime, regime_source,
                     first_sell_zone, max_sell_pct, min_trail_pct,
                     reversal_detect_from, daily_sell_from, bucket_cap,
                     leg_base_price, valvo_ref_price, current_price, security_id, user_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id""",
                    (name, entry, sl, qty, pos_val,
                     one_r, risk_pct, "valvo_ai", regime, "ai",
                     rp["fsz"], rp["msp"], rp["mtp"],
                     rp["rdf"], rp["dsf"], rp["bc"],
                     entry, entry, entry, sec_id or None, g.user_id))
                new_id = cur.fetchone()["id"]
                conn.commit()
                return f"✅ Position created: {name} — Entry ₹{entry}, {qty} shares, SL ₹{sl}, Value ₹{pos_val:,} ({regime}). 1R = ₹{one_r:,}. ID: {new_id}"
            except Exception as e:
                try:
                    conn.rollback()
                except:
                    pass
                return f"Error creating position: {e}"
            finally:
                conn.close()

        elif tool_name == "edit_position":
            pos, err = _find_position_by_name(tool_input["stock_name"])
            if err:
                return f"Error: {err}"
            fields = tool_input.get("fields", {})
            if not fields:
                return "No fields specified to update."
            # Whitelist of allowed fields
            allowed = {"entry_price", "stop_loss", "quantity", "market_regime", "ma_followed",
                       "ma_grade", "shakeout_count", "qualifies_for_trailing", "bucket_sold_pct",
                       "bucket_cap", "first_sell_done", "trailing_mode", "defensive_status",
                       "risk_pct", "current_price", "leg_base_price", "valvo_ref_price",
                       "position_value", "one_r_value", "entry_extension_pct", "current_r_multiple",
                       "stock_name", "security_id"}
            updates = {k: v for k, v in fields.items() if k in allowed}
            if not updates:
                return f"No valid fields to update. Allowed: {', '.join(sorted(allowed))}"
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()

                # If entry_price is being changed, auto-fix dependent fields
                if "entry_price" in updates:
                    new_entry = float(updates["entry_price"])
                    # Get current data to check SL validity
                    cur.execute("SELECT stop_loss, quantity, risk_pct FROM positions WHERE id = %s", (pos["id"],))
                    current = cur.fetchone()
                    old_sl = float(current.get("stop_loss") or 0)
                    qty_val = int(current.get("quantity") or updates.get("quantity", 0))
                    risk = float(current.get("risk_pct") or 4)

                    # Auto-fix SL if it's clearly invalid for new entry price
                    if "stop_loss" not in updates and (old_sl > new_entry * 1.5 or old_sl < new_entry * 0.5 or old_sl == 0):
                        new_sl = round(new_entry * (1 - risk / 100), 2)
                        updates["stop_loss"] = new_sl

                    # Recalculate position_value and one_r_value
                    if qty_val:
                        updates["position_value"] = round(new_entry * qty_val)
                        updates["one_r_value"] = round(new_entry * (risk / 100) * qty_val)

                    # Clear stale cached values so frontend computes fresh
                    updates["entry_extension_pct"] = None
                    updates["current_r_multiple"] = None

                set_parts = [f"{k} = %s" for k in updates]
                set_parts.append("updated_at = NOW()")
                vals = list(updates.values()) + [pos["id"]]
                cur.execute(f"UPDATE positions SET {', '.join(set_parts)} WHERE id = %s", vals)
                conn.commit()
                changes = ", ".join(f"{k}={v}" for k, v in updates.items() if v is not None)
                return f"✅ Updated {pos['stock_name']}: {changes}"
            finally:
                conn.close()

        elif tool_name == "close_position":
            pos, err = _find_position_by_name(tool_input["stock_name"])
            if err:
                return f"Error: {err}"
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                cur.execute("SELECT entry_price, quantity, current_price FROM positions WHERE id = %s", (pos["id"],))
                p = cur.fetchone()
                entry = float(p.get("entry_price") or 0)
                qty = int(p.get("quantity") or 0)
                exit_price = float(tool_input.get("exit_price") or p.get("current_price") or entry)
                total_pnl = (exit_price - entry) * qty
                total_pnl_pct = round((exit_price - entry) / entry * 100, 2) if entry else 0

                from datetime import datetime, timezone, timedelta
                IST = timezone(timedelta(hours=5, minutes=30))
                cur.execute("""UPDATE positions SET status = 'closed', exit_price = %s,
                    exit_date = %s, total_pnl = %s, total_pnl_pct = %s, updated_at = NOW() WHERE id = %s""",
                    (exit_price, datetime.now(IST), total_pnl, total_pnl_pct, pos["id"]))
                conn.commit()
                return f"✅ Closed {pos['stock_name']} at ₹{exit_price}. P&L: {'+'if total_pnl>=0 else ''}₹{total_pnl:,.0f} ({total_pnl_pct:+.1f}%)"
            finally:
                conn.close()

        elif tool_name == "search_stock":
            query = tool_input["query"]
            try:
                from services.market_data_service import search_stocks
                results = search_stocks(query)
                if not results:
                    return f"No stocks found for '{query}'"
                top = results[:5]
                lines = [f"Found {len(results)} results for '{query}':"]
                for r in top:
                    lines.append(f"  {r['symbol']} — {r['company_name']} (ID: {r['security_id']})")
                return "\n".join(lines)
            except Exception as e:
                return f"Search error: {e}"

        elif tool_name == "get_live_price":
            stock_name = tool_input.get("stock_name", "")
            security_id = tool_input.get("security_id", "")

            # Try to find security_id from positions first
            if stock_name and not security_id:
                pos, _ = _find_position_by_name(stock_name)
                if pos and pos.get("security_id"):
                    security_id = str(pos["security_id"])

            # If still no security_id, search stock universe
            if not security_id and stock_name:
                try:
                    from services.market_data_service import search_stocks
                    results = search_stocks(stock_name)
                    if results:
                        security_id = str(results[0]["security_id"])
                except:
                    pass

            if not security_id:
                return f"Could not find security ID for '{stock_name}'. Use search_stock first."

            result_parts = []
            # 1. Get LTP
            try:
                from services.market_data_service import get_ltp
                ltp_data = get_ltp([{"security_id": security_id, "exchange": "NSE_EQ"}])
                cmp = ltp_data.get(str(security_id))
                if cmp:
                    result_parts.append(f"CMP: ₹{cmp}")
            except Exception as e:
                result_parts.append(f"LTP error: {e}")

            # 2. Get OHLC from live candle
            try:
                from services.market_data_service import get_live_candle
                candle = get_live_candle(security_id)
                if candle:
                    result_parts.append(f"O:{candle.get('open','?')} H:{candle.get('high','?')} L:{candle.get('low','?')} C:{candle.get('close','?')}")
            except:
                pass

            # 3. Get MAs
            try:
                from services.market_data_service import calculate_mas
                mas = calculate_mas(security_id)
                if mas:
                    if mas.get("five_ma"): result_parts.append(f"5MA:₹{round(mas['five_ma'],2)}")
                    if mas.get("ten_ma"): result_parts.append(f"10MA:₹{round(mas['ten_ma'],2)}")
                    if mas.get("twenty_ma"): result_parts.append(f"20MA:₹{round(mas['twenty_ma'],2)}")
            except:
                pass

            return " | ".join(result_parts) if result_parts else f"No data available for security {security_id}"

        elif tool_name == "delete_position":
            if not tool_input.get("confirmed"):
                return "⚠ Deletion requires explicit user confirmation. Ask the user 'Are you sure you want to permanently delete [stock]?' first."
            pos, err = _find_position_by_name(tool_input["stock_name"])
            if err:
                return f"Error: {err}"
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                # Drop equity-curve log rows before the position itself so
                # realized P&L from this deleted trade doesn't keep inflating
                # Portfolio Capital. FK CASCADE covers this too; this matches
                # position_routes.delete_position.
                capital_log.delete_for_position(cur, g.user_id, pos["id"])
                cur.execute("DELETE FROM positions WHERE id = %s", (pos["id"],))
                conn.commit()
                return f"✅ Permanently deleted {pos['stock_name']} from Position Manager."
            finally:
                conn.close()

        elif tool_name == "portfolio_snapshot":
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                # Run 5MA engine first to get fresh prices + MAs
                cur = conn.cursor()
                cur.execute("SELECT id, security_id FROM positions WHERE status = 'active' AND user_id = %s AND security_id IS NOT NULL", (g.user_id,))
                active = cur.fetchall()
                for pos in active:
                    cur.execute("""
                        SELECT close,
                            AVG(close) OVER (ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
                            AVG(close) OVER (ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as ma10,
                            AVG(close) OVER (ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as ma20
                        FROM candles_daily WHERE security_id = %s ORDER BY date DESC LIMIT 1
                    """, (pos["security_id"],))
                    cd = cur.fetchone()
                    if cd and cd["close"]:
                        cp = float(cd["close"])
                        ma5 = round(float(cd["ma5"]), 2) if cd["ma5"] else None
                        ma10 = round(float(cd["ma10"]), 2) if cd["ma10"] else None
                        ma20 = round(float(cd["ma20"]), 2) if cd["ma20"] else None
                        d_status = "safe"
                        if ma5 and ma5 > 0:
                            dist = ((cp - ma5) / ma5) * 100
                            if dist < -1.0: d_status = "break"
                            elif dist < 0: d_status = "marginal"
                        cur.execute("""
                            UPDATE positions SET current_price=%s, last_5ma_price=%s, last_10ma_price=%s,
                                last_20ma_price=%s, defensive_status=%s, updated_at=NOW() WHERE id=%s
                        """, (cp, ma5, ma10, ma20, d_status, pos["id"]))
                conn.commit()

                status_filter = "1=1" if tool_input.get("include_closed") else "status = 'active'"
                cur.execute(f"SELECT * FROM positions WHERE {status_filter} AND user_id = %s ORDER BY created_at DESC", (g.user_id,))
                rows = cur.fetchall()
                if not rows:
                    return "No positions found."

                portfolio = 50000000  # 5Cr
                lines = [f"{len(rows)} positions:"]
                total_pnl = 0
                alerts = []
                for p in rows:
                    entry = float(p.get("entry_price") or 0)
                    cmp = float(p.get("current_price") or entry)
                    qty = int(p.get("quantity") or 0)
                    sl = float(p.get("stop_loss") or 0)
                    ma5 = float(p.get("last_5ma_price") or 0)
                    pnl = (cmp - entry) * qty
                    total_pnl += pnl
                    pnl_pct = ((cmp - entry) / entry * 100) if entry else 0
                    ext_pct = float(p.get("entry_extension_pct") or 0)
                    r_mult = float(p.get("current_r_multiple") or 0)
                    defensive = p.get("defensive_status", "safe")
                    ma5_dist = round(((cmp - ma5) / ma5 * 100), 2) if ma5 > 0 else 0

                    status_icon = "🟢" if defensive == "safe" else "🟡" if defensive == "marginal" else "🔴"
                    line = f"{status_icon} {p['stock_name']}: CMP ₹{cmp:.0f} | Entry ₹{entry:.0f} | P&L {pnl_pct:+.1f}% (₹{pnl:+,.0f}) | {r_mult:+.1f}R | Ext {ext_pct:+.1f}%"
                    if ma5: line += f" | 5MA ₹{ma5:.0f} ({ma5_dist:+.1f}%) [{defensive}]"
                    if sl: line += f" | SL ₹{sl:.0f}"
                    lines.append(line)

                    if defensive == "break":
                        alerts.append(f"🔴 {p['stock_name']} 5MA BREAK ({ma5_dist:.1f}% below)")
                    elif defensive == "marginal":
                        alerts.append(f"🟡 {p['stock_name']} marginal ({ma5_dist:.1f}% below 5MA)")

                pnl_str = f"+₹{total_pnl:,.0f}" if total_pnl >= 0 else f"-₹{abs(total_pnl):,.0f}"
                lines.append(f"Total P&L: {pnl_str} ({total_pnl/portfolio*100:+.2f}% of PF)")
                if alerts:
                    lines.append(f"Alerts: {', '.join(alerts)}")
                else:
                    lines.append("All positions clean — no alerts.")
                return "\n".join(lines)
            finally:
                conn.close()

        elif tool_name == "query_screener":
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()

                # Build the query: compute 20-day SMA liquidity from candles_daily
                min_liq = tool_input.get("min_liquidity_cr")
                max_liq = tool_input.get("max_liquidity_cr")
                min_chg = tool_input.get("min_change_pct")
                max_chg = tool_input.get("max_change_pct")
                min_price = tool_input.get("min_price")
                max_price = tool_input.get("max_price")
                sort_by = tool_input.get("sort_by", "liquidity")
                limit = min(tool_input.get("limit", 20), 50)

                # One powerful query:
                # 1. Get today's close + change% from candles_daily
                # 2. Compute 20-day SMA liquidity (avg volume × avg close / 1Cr)
                cur.execute("""
                    WITH today AS (
                        SELECT c.security_id, u.symbol, u.company_name,
                            c.close, c.volume,
                            CASE WHEN prev.close > 0 THEN ((c.close - prev.close) / prev.close * 100) ELSE 0 END as pchange
                        FROM candles_daily c
                        JOIN stock_universe u ON c.security_id = u.security_id
                        LEFT JOIN LATERAL (
                            SELECT close FROM candles_daily
                            WHERE security_id = c.security_id AND date < CURRENT_DATE
                            ORDER BY date DESC LIMIT 1
                        ) prev ON true
                        WHERE c.date = CURRENT_DATE AND u.is_active = true
                    ),
                    liq AS (
                        SELECT security_id,
                            ROUND((AVG(volume) * AVG(close) / 10000000)::numeric, 2) as liquidity_cr
                        FROM candles_daily
                        WHERE date >= CURRENT_DATE - INTERVAL '30 days'
                        GROUP BY security_id
                        HAVING COUNT(*) >= 5
                    )
                    SELECT t.security_id, t.symbol, t.company_name, t.close, t.volume,
                        ROUND(t.pchange::numeric, 2) as pchange,
                        COALESCE(l.liquidity_cr, 0) as liquidity_cr
                    FROM today t
                    LEFT JOIN liq l ON t.security_id = l.security_id
                    WHERE 1=1
                        {f"AND COALESCE(l.liquidity_cr, 0) >= {float(min_liq)}" if min_liq else ""}
                        {f"AND COALESCE(l.liquidity_cr, 0) <= {float(max_liq)}" if max_liq else ""}
                        {f"AND t.pchange >= {float(min_chg)}" if min_chg is not None else ""}
                        {f"AND t.pchange <= {float(max_chg)}" if max_chg is not None else ""}
                        {f"AND t.close >= {float(min_price)}" if min_price else ""}
                        {f"AND t.close <= {float(max_price)}" if max_price else ""}
                    ORDER BY {
                        "l.liquidity_cr DESC NULLS LAST" if sort_by == "liquidity" else
                        "t.pchange DESC" if sort_by == "change" else
                        "t.close DESC" if sort_by == "price" else
                        "t.volume DESC" if sort_by == "volume" else
                        "l.liquidity_cr DESC NULLS LAST"
                    }
                    LIMIT {limit}
                """)

                rows = cur.fetchall()

                if not rows:
                    filters_desc = []
                    if min_liq: filters_desc.append(f"liquidity ≥₹{min_liq}Cr")
                    if max_liq: filters_desc.append(f"liquidity ≤₹{max_liq}Cr")
                    if min_chg is not None: filters_desc.append(f"change ≥{min_chg}%")
                    if max_chg is not None: filters_desc.append(f"change ≤{max_chg}%")
                    return f"No stocks found matching: {', '.join(filters_desc) if filters_desc else 'no filters'}. Today has {cur.rowcount} stocks with data."

                # Count total matching (not just limited)
                total_matching = len(rows)  # Already limited

                # Get total count without limit for summary
                cur.execute(f"""
                    WITH today AS (
                        SELECT c.security_id, c.close, c.volume,
                            CASE WHEN prev.close > 0 THEN ((c.close - prev.close) / prev.close * 100) ELSE 0 END as pchange
                        FROM candles_daily c
                        JOIN stock_universe u ON c.security_id = u.security_id
                        LEFT JOIN LATERAL (
                            SELECT close FROM candles_daily
                            WHERE security_id = c.security_id AND date < CURRENT_DATE
                            ORDER BY date DESC LIMIT 1
                        ) prev ON true
                        WHERE c.date = CURRENT_DATE AND u.is_active = true
                    ),
                    liq AS (
                        SELECT security_id,
                            ROUND((AVG(volume) * AVG(close) / 10000000)::numeric, 2) as liquidity_cr
                        FROM candles_daily
                        WHERE date >= CURRENT_DATE - INTERVAL '30 days'
                        GROUP BY security_id
                        HAVING COUNT(*) >= 5
                    )
                    SELECT COUNT(*) as cnt FROM today t
                    LEFT JOIN liq l ON t.security_id = l.security_id
                    WHERE 1=1
                        {f"AND COALESCE(l.liquidity_cr, 0) >= {float(min_liq)}" if min_liq else ""}
                        {f"AND COALESCE(l.liquidity_cr, 0) <= {float(max_liq)}" if max_liq else ""}
                        {f"AND t.pchange >= {float(min_chg)}" if min_chg is not None else ""}
                        {f"AND t.pchange <= {float(max_chg)}" if max_chg is not None else ""}
                        {f"AND t.close >= {float(min_price)}" if min_price else ""}
                        {f"AND t.close <= {float(max_price)}" if max_price else ""}
                """)
                total_count = cur.fetchone()["cnt"]

                # Build response
                lines = [f"Found {total_count} stocks matching your criteria:\n"]

                # Summary stats
                liq_values = [float(r["liquidity_cr"]) for r in rows if r["liquidity_cr"]]
                if liq_values:
                    lines.append(f"Liquidity range: ₹{min(liq_values):.0f}Cr — ₹{max(liq_values):.0f}Cr (avg ₹{sum(liq_values)/len(liq_values):.0f}Cr)\n")

                # Top stocks
                for i, r in enumerate(rows[:20], 1):
                    lines.append(f"  {i}. {r['symbol']} — ₹{float(r['close']):,.1f} | {float(r['pchange']):+.1f}% | Liq ₹{float(r['liquidity_cr']):.0f}Cr")

                if total_count > 20:
                    lines.append(f"\n  ... and {total_count - 20} more")

                # Navigation action
                filter_params = []
                if min_liq: filter_params.append(f"min_liq={min_liq}")
                lines.append(f"\n→ Open Screener with this filter to see all {total_count} stocks with charts")

                # Build structured stock data for frontend card rendering
                stocks_data = []
                for r in rows[:20]:
                    stocks_data.append({
                        "symbol": r["symbol"],
                        "name": r["company_name"],
                        "security_id": str(r["security_id"]),
                        "price": float(r["close"]),
                        "change": float(r["pchange"]),
                        "liquidity_cr": float(r["liquidity_cr"]),
                    })

                return {
                    "text": "\n".join(lines),
                    "type": "screener_results",
                    "total_count": total_count,
                    "shown_count": len(stocks_data),
                    "stocks": stocks_data,
                    "filters": {
                        "min_liquidity_cr": min_liq,
                        "max_liquidity_cr": max_liq,
                        "min_change_pct": min_chg,
                        "max_change_pct": max_chg,
                        "min_price": min_price,
                        "max_price": max_price,
                        "sort_by": sort_by,
                    },
                }
            except Exception as e:
                import traceback
                traceback.print_exc()
                return f"Screener query error: {str(e)}"
            finally:
                conn.close()

        elif tool_name == "query_journal_stats":
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                lines = []

                # Analytics (win rate, avg winner, etc.) — per-user. The
                # old `WHERE id = 1` was single-tenant leftover that would
                # return whichever user's row existed first and leak their
                # aggregates to the wrong caller.
                uid = getattr(g, "user_id", None)
                if not uid:
                    return "Not authenticated"
                cur.execute(
                    "SELECT data FROM nexus_analytics WHERE user_id = %s "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (uid,),
                )
                arow = cur.fetchone()
                if arow:
                    import json as _json
                    a = arow["data"] if isinstance(arow["data"], dict) else _json.loads(arow["data"])
                    lines.append("═══ TRADING STATISTICS ═══")
                    lines.append(f"Total trades: {a.get('total_trades', 0)} | Winners: {a.get('win_count', 0)} | Losers: {a.get('loss_count', 0)}")
                    lines.append(f"Win rate: {a.get('win_rate', 0):.1f}%")
                    lines.append(f"Avg winner: +{a.get('avg_winner_pct', 0):.1f}% (₹{a.get('avg_winner_pl', 0):+,.0f}) | Avg loser: {a.get('avg_loser_pct', 0):.1f}% (₹{a.get('avg_loser_pl', 0):+,.0f})")
                    lines.append(f"Best trade: {a.get('best_trade_name', '?')} → P&L ₹{a.get('best_trade_pl', 0):+,.0f} (+{a.get('avg_winner_pct', 0):.1f}%)")
                    lines.append(f"Worst trade: {a.get('worst_trade_name', '?')} → P&L ₹{a.get('worst_trade_pl', 0):+,.0f}")
                    lines.append(f"Total P&L: ₹{a.get('total_pl', 0):,.0f}")
                    if a.get('avg_winner_r'):
                        lines.append(f"Avg winner R: {a['avg_winner_r']:.1f}R | Avg loser R: {a.get('avg_loser_r', 0):.1f}R")
                else:
                    lines.append("No analytics data — run journal sync first.")

                # Monthly breakdown
                include_monthly = tool_input.get("include_monthly", True)
                if include_monthly:
                    cur.execute(
                        "SELECT * FROM nexus_monthly WHERE user_id = %s "
                        "ORDER BY year DESC, month DESC",
                        (uid,),
                    )
                    months = cur.fetchall()
                    if months:
                        lines.append("\n═══ MONTHLY P&L ═══")
                        for m in months:
                            lines.append(f"  {m.get('year')}-{str(m.get('month')).zfill(2)}: {m.get('trades', 0)} trades | P&L ₹{m.get('pl', 0):,.0f} | Capital ₹{m.get('capital', 0):,.0f}")

                # Individual trades
                include_trades = tool_input.get("include_trades", True)
                status = tool_input.get("status_filter", "all")
                if include_trades:
                    if status == "open":
                        cur.execute("SELECT * FROM journal_trades_computed WHERE position_status = 'Open' AND user_id = %s ORDER BY trade_no DESC", (g.user_id,))
                    elif status == "closed":
                        cur.execute("SELECT * FROM journal_trades_computed WHERE position_status = 'Closed' AND user_id = %s ORDER BY trade_no DESC", (g.user_id,))
                    else:
                        cur.execute("SELECT * FROM journal_trades_computed WHERE user_id = %s ORDER BY trade_no DESC", (g.user_id,))
                    trades = cur.fetchall()
                    if trades:
                        open_t = [t for t in trades if t.get("position_status") == "Open"]
                        closed_t = [t for t in trades if t.get("position_status") == "Closed"]
                        if open_t:
                            lines.append(f"\n═══ OPEN POSITIONS ({len(open_t)}) ═══")
                            for t in open_t:
                                lines.append(f"  {t['stock_name']}: Entry ₹{t.get('entry_price', 0)} | Qty {t.get('open_qty', 0)} | Move {t.get('stock_move_pct', 0):+.1f}%")
                        if closed_t:
                            # Sort by P&L descending for easy "biggest winner" answers
                            closed_t.sort(key=lambda x: float(x.get('pl', 0) or 0), reverse=True)
                            lines.append(f"\n═══ CLOSED TRADES ({len(closed_t)}) — sorted by P&L ═══")
                            for t in closed_t[:15]:
                                pl = float(t.get('pl', 0) or 0)
                                rr = float(t.get('reward_risk', 0) or 0)
                                move = float(t.get('stock_move_pct', 0) or 0)
                                lines.append(f"  {t['stock_name']}: P&L ₹{pl:+,.0f} | {move:+.1f}% | {rr:+.1f}R | Exit ₹{t.get('avg_exit_price', 0)} | {t.get('holding_days', 0)}d")
                            if len(closed_t) > 15:
                                lines.append(f"  ... and {len(closed_t) - 15} more")

                return {
                    "text": "\n".join(lines) if lines else "No journal data available.",
                    "type": "journal_stats",
                    "stats": {
                        "total_trades": a.get("total_trades", 0) if arow else 0,
                        "win_count": a.get("win_count", 0) if arow else 0,
                        "loss_count": a.get("loss_count", 0) if arow else 0,
                        "win_rate": round(float(a.get("win_rate", 0)), 1) if arow else 0,
                        "avg_winner_pct": round(float(a.get("avg_winner_pct", 0)), 1) if arow else 0,
                        "avg_loser_pct": round(float(a.get("avg_loser_pct", 0)), 1) if arow else 0,
                        "total_pl": round(float(a.get("total_pl", 0))) if arow else 0,
                        "best_trade": a.get("best_trade_name", "") if arow else "",
                        "best_trade_pl": round(float(a.get("best_trade_pl", 0))) if arow else 0,
                        "worst_trade": a.get("worst_trade_name", "") if arow else "",
                        "worst_trade_pl": round(float(a.get("worst_trade_pl", 0))) if arow else 0,
                        "avg_winner_r": round(float(a.get("avg_winner_r", 0)), 1) if arow and a.get("avg_winner_r") else 0,
                        "avg_loser_r": round(float(a.get("avg_loser_r", 0)), 1) if arow and a.get("avg_loser_r") else 0,
                    } if arow else None,
                }
            except Exception as e:
                import traceback
                traceback.print_exc()
                return f"Journal stats query error: {str(e)}"
            finally:
                conn.close()

        elif tool_name == "query_sectoral_screener":
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                sector_input = (tool_input.get("sector") or "").lower().strip()
                ma_period = int(tool_input.get("ma_period") or 20)
                condition = (tool_input.get("condition") or "above").lower()
                include_list = tool_input.get("include_list", True)

                # Map plain English sector names → index_symbol in DB
                SECTOR_MAP = {
                    "defence": "NIFTY IND DEFENCE",
                    "defense": "NIFTY IND DEFENCE",
                    "metal": "NIFTY METAL",
                    "metals": "NIFTY METAL",
                    "it": "NIFTYIT",
                    "tech": "NIFTYIT",
                    "technology": "NIFTYIT",
                    "pharma": "NIFTY PHARMA",
                    "pharmaceutical": "NIFTY PHARMA",
                    "bank": "BANKNIFTY",
                    "banking": "BANKNIFTY",
                    "banks": "BANKNIFTY",
                    "psu bank": "NIFTY PSU BANK",
                    "psu banks": "NIFTY PSU BANK",
                    "pvt bank": "NIFTY PVT BANK",
                    "private bank": "NIFTY PVT BANK",
                    "auto": "NIFTY AUTO",
                    "automobile": "NIFTY AUTO",
                    "realty": "NIFTY REALTY",
                    "real estate": "NIFTY REALTY",
                    "energy": "NIFTY ENERGY",
                    "fmcg": "NIFTY FMCG",
                    "media": "NIFTY MEDIA",
                    "commodities": "NIFTY COMMODITIES",
                    "commodity": "NIFTY COMMODITIES",
                    "healthcare": "NIFTY HEALTHCARE",
                    "health": "NIFTY HEALTHCARE",
                    "oil": "NIFTY OIL AND GAS",
                    "oil and gas": "NIFTY OIL AND GAS",
                    "gas": "NIFTY OIL AND GAS",
                    "consumer durables": "NIFTY CONSR DURBL",
                    "durables": "NIFTY CONSR DURBL",
                    "consumption": "NIFTY CONSUMPTION",
                    "infra": "NIFTYINFRA",
                    "infrastructure": "NIFTYINFRA",
                    "cpse": "NIFTYCPSE",
                    "pse": "NIFTYPSE",
                    "psu": "NIFTYCPSE",
                    "services": "NIFTY SERV SECTOR",
                    "service": "NIFTY SERV SECTOR",
                    "capital markets": "Nifty Capital Mkt",
                    "capital market": "Nifty Capital Mkt",
                    "manufacturing": "NIFTY INDIA MFG",
                    "mnc": "NIFTY MNC",
                    "digital": "NIFTY IND DIGITAL",
                    "ev": "NIFTY EV",
                    "electric vehicle": "NIFTY EV",
                    "housing": "Nifty Housing",
                    "mobility": "Nifty Mobility",
                    "transport": "Nifty Trans Logis",
                    "logistics": "Nifty Trans Logis",
                    "fin": "FINNIFTY",
                    "financial": "FINNIFTY",
                    "nifty 50": "NIFTY",
                    "nifty50": "NIFTY",
                    "midcap": "NIFTY MID100 FREE",
                    "mid cap": "NIFTY MID100 FREE",
                    "smallcap": "NIFTY SMALLCAP 100",
                    "small cap": "NIFTY SMALLCAP 100",
                    "microcap": "NIFTY MICROCAP250",
                    "micro cap": "NIFTY MICROCAP250",
                    "nifty 500": "NIFTY 500",
                }

                index_symbol = None
                for key, sym in SECTOR_MAP.items():
                    if key in sector_input or sector_input in key:
                        index_symbol = sym
                        break

                if not index_symbol:
                    return f"Could not find sector '{sector_input}'. Available sectors: defence, metal, IT, pharma, bank, PSU bank, auto, realty, energy, FMCG, infra, healthcare, oil and gas, EV, housing, midcap, smallcap, and more."

                # Compute MA for each stock in the index using candles_daily
                # MA condition: close vs N-day simple moving average
                cur.execute(f"""
                    WITH constituents AS (
                        SELECT
                            ic.stock_symbol,
                            ic.stock_name,
                            ic.weightage,
                            COALESCE(
                                su_symbol.security_id,
                                su_isin.security_id,
                                NULLIF(BTRIM(ic.security_id), ''),
                                NULL
                            ) AS security_id
                        FROM index_constituents ic
                        LEFT JOIN LATERAL (
                            SELECT su.security_id
                            FROM stock_universe su
                            WHERE su.symbol = ic.stock_symbol
                              AND su.is_active = true
                            LIMIT 1
                        ) su_symbol ON TRUE
                        LEFT JOIN LATERAL (
                            SELECT su.security_id
                            FROM stock_universe su
                            WHERE ic.isin IS NOT NULL
                              AND BTRIM(ic.isin) <> ''
                              AND su.isin = ic.isin
                              AND su.is_active = true
                            LIMIT 1
                        ) su_isin ON TRUE
                        WHERE ic.index_symbol = %s
                          AND COALESCE(
                                su_symbol.security_id,
                                su_isin.security_id,
                                NULLIF(BTRIM(ic.security_id), ''),
                                NULL
                              ) IS NOT NULL
                          AND COALESCE(
                                su_symbol.security_id,
                                su_isin.security_id,
                                NULLIF(BTRIM(ic.security_id), ''),
                                NULL
                              ) <> 'UNMAPPED'
                    ),
                    latest_close AS (
                        SELECT DISTINCT ON (c.security_id)
                            c.security_id, c.close as latest_close, c.date
                        FROM candles_daily c
                        JOIN constituents cn ON c.security_id = cn.security_id
                        ORDER BY c.security_id, c.date DESC
                    ),
                    ma_calc AS (
                        SELECT c.security_id,
                            ROUND(AVG(c.close)::numeric, 2) as ma_value
                        FROM candles_daily c
                        JOIN constituents cn ON c.security_id = cn.security_id
                        WHERE c.date >= CURRENT_DATE - INTERVAL '{ma_period * 2} days'
                        GROUP BY c.security_id
                        HAVING COUNT(*) >= %s
                    )
                    SELECT
                        cn.stock_symbol, cn.stock_name, cn.weightage,
                        lc.latest_close, lc.date as price_date,
                        ma.ma_value,
                        ROUND(((lc.latest_close - ma.ma_value) / NULLIF(ma.ma_value, 0) * 100)::numeric, 1) as pct_vs_ma
                    FROM constituents cn
                    JOIN latest_close lc ON cn.security_id = lc.security_id
                    JOIN ma_calc ma ON cn.security_id = ma.security_id
                    ORDER BY pct_vs_ma DESC
                """, (index_symbol, max(ma_period // 2, 5)))

                rows = cur.fetchall()

                if not rows:
                    return f"No price data found for {index_symbol}. The index may not have candle data loaded yet."

                above = [r for r in rows if float(r["latest_close"] or 0) >= float(r["ma_value"] or 0)]
                below = [r for r in rows if float(r["latest_close"] or 0) < float(r["ma_value"] or 0)]
                filtered = above if condition == "above" else below

                # Build display name
                DISPLAY_NAMES = {
                    "NIFTY IND DEFENCE": "Defence", "NIFTY METAL": "Metal", "NIFTYIT": "IT",
                    "NIFTY PHARMA": "Pharma", "BANKNIFTY": "Bank Nifty", "NIFTY PSU BANK": "PSU Bank",
                    "NIFTY PVT BANK": "Pvt Bank", "NIFTY AUTO": "Auto", "NIFTY REALTY": "Realty",
                    "NIFTY ENERGY": "Energy", "NIFTY FMCG": "FMCG", "NIFTY MEDIA": "Media",
                    "NIFTY COMMODITIES": "Commodities", "NIFTY HEALTHCARE": "Healthcare",
                    "NIFTY OIL AND GAS": "Oil & Gas", "NIFTY CONSR DURBL": "Consumer Durables",
                    "NIFTY CONSUMPTION": "Consumption", "NIFTYINFRA": "Infra",
                    "NIFTYCPSE": "CPSE", "NIFTYPSE": "PSE", "NIFTY SERV SECTOR": "Services",
                    "Nifty Capital Mkt": "Capital Markets", "NIFTY INDIA MFG": "Manufacturing",
                    "NIFTY MNC": "MNC", "NIFTY IND DIGITAL": "India Digital", "NIFTY EV": "EV",
                    "Nifty Housing": "Housing", "Nifty Mobility": "Mobility",
                    "Nifty Trans Logis": "Transport & Logistics", "FINNIFTY": "Fin Nifty",
                    "NIFTY": "Nifty 50", "NIFTY 500": "Nifty 500",
                    "NIFTY MID100 FREE": "Midcap 100", "NIFTY SMALLCAP 100": "Smallcap 100",
                    "NIFTY MICROCAP250": "Microcap 250", "NIFTY NEXT 50": "Next 50",
                }
                display = DISPLAY_NAMES.get(index_symbol, index_symbol)

                lines = [
                    f"═══ {display} — {ma_period}MA ANALYSIS ═══",
                    f"Total stocks with data: {len(rows)}",
                    f"Above {ma_period}MA: {len(above)} ({round(len(above)/len(rows)*100)}%)",
                    f"Below {ma_period}MA: {len(below)} ({round(len(below)/len(rows)*100)}%)",
                    f"",
                    f"{'Above' if condition == 'above' else 'Below'} {ma_period}MA: {len(filtered)} stocks",
                ]

                if include_list and filtered:
                    lines.append(f"")
                    lines.append(f"{'ABOVE' if condition == 'above' else 'BELOW'} {ma_period}MA (sorted by distance):")
                    for r in filtered[:25]:
                        pct = float(r["pct_vs_ma"] or 0)
                        sign = "+" if pct >= 0 else ""
                        lines.append(f"  {r['stock_symbol']:15s} CMP ₹{r['latest_close']:.0f} | {ma_period}MA ₹{r['ma_value']:.0f} | {sign}{pct:.1f}%")
                    if len(filtered) > 25:
                        lines.append(f"  ... and {len(filtered) - 25} more")

                # Build structured data for frontend card
                stocks_data = []
                for r in filtered[:25]:
                    stocks_data.append({
                        "symbol": r["stock_symbol"],
                        "name": r["stock_name"],
                        "price": float(r["latest_close"] or 0),
                        "ma_value": float(r["ma_value"] or 0),
                        "pct_vs_ma": float(r["pct_vs_ma"] or 0),
                    })

                return {
                    "text": "\n".join(lines),
                    "type": "sectoral_results",
                    "sector": display,
                    "ma_period": ma_period,
                    "condition": condition,
                    "total_stocks": len(rows),
                    "above_count": len(above),
                    "below_count": len(below),
                    "filtered_count": len(filtered),
                    "stocks": stocks_data,
                }

            except Exception as e:
                import traceback
                traceback.print_exc()
                return f"Sectoral screener error: {str(e)}"
            finally:
                conn.close()

        elif tool_name == "query_trade_analytics":
            fy = tool_input.get("fy", "2025-26")
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()

                # Build table source based on FY
                fy_tables = {
                    "2020-21": ("legacy_trades_fy2021", 6000000),
                    "2021-22": ("legacy_trades_fy2122", 9075419),
                    "2022-23": ("legacy_trades_fy2223", 16187147),
                    "2023-24": ("legacy_trades_fy2324", 18028240),
                    "2024-25": ("legacy_trades_fy2425", 28000000),
                    "2025-26": ("legacy_trades", 50000000),
                    "2026-27": ("journal_trades_computed", 50000000),
                }
                uid = g.user_id
                if fy == "all":
                    tbl = f"""(
                        SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner FROM legacy_trades_fy2021
                        UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner FROM legacy_trades_fy2122
                        UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner FROM legacy_trades_fy2223
                        UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner FROM legacy_trades_fy2324
                        UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner FROM legacy_trades_fy2425
                        UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner FROM legacy_trades WHERE user_id = '{uid}'
                        UNION ALL SELECT symbol, realized_pl::real, realized_pl_pct::real, month_label, is_winner FROM journal_trades_computed WHERE user_id = '{uid}'
                    ) combined"""
                    base = 6000000
                else:
                    t = fy_tables.get(fy, fy_tables["2025-26"])
                    tbl_name = t[0]
                    base = t[1]
                    # Tables with user_id need filtering
                    if tbl_name in ("legacy_trades", "journal_trades_computed"):
                        tbl = f"(SELECT * FROM {tbl_name} WHERE user_id = '{uid}') {tbl_name}"
                    else:
                        tbl = tbl_name

                cur.execute(f"""
                    SELECT COUNT(*) as total, SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) as wins,
                        ROUND(SUM(realized_pl)::numeric) as total_pl,
                        ROUND(AVG(CASE WHEN realized_pl > 0 THEN realized_pl_pct END)::numeric, 2) as avg_win_pct,
                        ROUND(AVG(CASE WHEN realized_pl <= 0 THEN realized_pl_pct END)::numeric, 2) as avg_loss_pct,
                        ROUND(SUM(CASE WHEN realized_pl > 0 THEN realized_pl ELSE 0 END)::numeric) as gross_win,
                        ROUND(ABS(SUM(CASE WHEN realized_pl <= 0 THEN realized_pl ELSE 0 END))::numeric) as gross_loss
                    FROM {tbl}
                """)
                s = dict(cur.fetchone())
                total = s["total"] or 0
                wins = s["wins"] or 0
                wr = round(wins / total * 100, 1) if total > 0 else 0
                pf = round(float(s["gross_win"] or 0) / max(float(s["gross_loss"] or 1), 1), 2)
                ret_pct = round(float(s["total_pl"] or 0) / base * 100, 1)

                # Best/worst trade
                cur.execute(f"SELECT symbol, realized_pl FROM {tbl} ORDER BY realized_pl DESC LIMIT 1")
                best = dict(cur.fetchone() or {})
                cur.execute(f"SELECT symbol, realized_pl FROM {tbl} ORDER BY realized_pl ASC LIMIT 1")
                worst = dict(cur.fetchone() or {})

                # Monthly breakdown
                cur.execute(f"""
                    SELECT month_label, COUNT(*) as trades, ROUND(SUM(realized_pl)::numeric) as pl,
                        ROUND((SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100), 0) as wr
                    FROM {tbl} GROUP BY month_label ORDER BY MIN(realized_pl_pct)
                """)
                months = [dict(r) for r in cur.fetchall()]

                _cdb(conn)

                lines = [f"═══ TRADE ANALYTICS — FY {fy} ═══"]
                lines.append(f"Total: {total} trades | Win Rate: {wr}% ({wins}W / {total - wins}L)")
                lines.append(f"P&L: ₹{float(s['total_pl'] or 0):,.0f} | Return: {ret_pct}%")
                lines.append(f"Avg Winner: +{s['avg_win_pct'] or 0}% | Avg Loser: {s['avg_loss_pct'] or 0}%")
                lines.append(f"Profit Factor: {pf}x | Gross Win: ₹{float(s['gross_win'] or 0):,.0f} | Gross Loss: ₹{float(s['gross_loss'] or 0):,.0f}")
                lines.append(f"Best: {best.get('symbol', '—')} (₹{float(best.get('realized_pl', 0)):+,.0f})")
                lines.append(f"Worst: {worst.get('symbol', '—')} (₹{float(worst.get('realized_pl', 0)):+,.0f})")
                if months:
                    lines.append(f"\nMonthly ({len(months)} months):")
                    for m in months:
                        lines.append(f"  {m['month_label']}: ₹{float(m['pl'] or 0):+,.0f} | {m['trades']} trades | WR {m['wr'] or 0}%")
                return "\n".join(lines)
            except Exception as e:
                return f"Analytics query error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "get_fy_summary":
            fy = tool_input.get("fy", "2025-26")
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                fy_tables = {
                    "2020-21": ("legacy_trades_fy2021", 6000000),
                    "2021-22": ("legacy_trades_fy2122", 9075419),
                    "2022-23": ("legacy_trades_fy2223", 16187147),
                    "2023-24": ("legacy_trades_fy2324", 18028240),
                    "2024-25": ("legacy_trades_fy2425", 28000000),
                    "2025-26": ("legacy_trades", 50000000),
                    "2026-27": ("journal_trades_computed", 50000000),
                }
                t = fy_tables.get(fy, fy_tables["2025-26"])
                cur.execute(f"""
                    SELECT COUNT(*) as total, SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) as wins,
                        ROUND(SUM(realized_pl)::numeric) as pl,
                        ROUND(SUM(CASE WHEN realized_pl > 0 THEN realized_pl ELSE 0 END)::numeric) as gw,
                        ROUND(ABS(SUM(CASE WHEN realized_pl <= 0 THEN realized_pl ELSE 0 END))::numeric) as gl
                    FROM {t[0]}
                """)
                s = dict(cur.fetchone())
                _cdb(conn)
                total = s["total"] or 0
                wins = s["wins"] or 0
                pf = round(float(s["gw"] or 0) / max(float(s["gl"] or 1), 1), 2)
                return (f"FY {fy}: {total} trades, WR {round(wins/max(total,1)*100, 1)}%, "
                        f"P&L ₹{float(s['pl'] or 0):,.0f}, Return {round(float(s['pl'] or 0)/t[1]*100, 1)}%, PF {pf}x")
            except Exception as e:
                return f"FY summary error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "create_journal_trade":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                from datetime import date as _date
                conn = _gdb()
                cur = conn.cursor()

                symbol = tool_input.get("symbol", "").upper()
                entry_price = tool_input.get("entry_price")
                sl = tool_input.get("sl")
                qty = tool_input.get("initial_qty", 0)
                trade_date = tool_input.get("trade_date", str(_date.today()))
                entry_type = tool_input.get("entry_type", "BREAKOUT")
                security_id = tool_input.get("security_id", "")
                sector = tool_input.get("sector", "")

                # Get next trade_no
                cur.execute("SELECT COALESCE(MAX(trade_no), 0) + 1 as n FROM journal_trades WHERE user_id = %s", (g.user_id,))
                next_no = cur.fetchone()["n"]

                # Auto-link to PM position
                position_id = None
                if symbol or security_id:
                    cur.execute("""
                        SELECT id FROM positions WHERE status = 'active' AND user_id = %s
                        AND (LOWER(stock_name) = LOWER(%s) OR (security_id IS NOT NULL AND security_id = %s))
                        LIMIT 1
                    """, (g.user_id, symbol, security_id))
                    match = cur.fetchone()
                    if match:
                        position_id = match["id"]

                cur.execute("""
                    INSERT INTO journal_trades (trade_no, trade_date, symbol, name, entry_type, buy_sell,
                        entry_price, avg_entry, sl, initial_qty, security_id, sector, position_id, user_id, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'Buy', %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    RETURNING id, trade_no
                """, (next_no, trade_date, symbol, symbol, entry_type,
                      entry_price, entry_price, sl, qty, security_id, sector, position_id, g.user_id))
                row = dict(cur.fetchone())
                conn.commit()
                _cdb(conn)

                linked = " (linked to PM position)" if position_id else ""
                return f"✅ Journal #{row['trade_no']}: {symbol} @ ₹{entry_price} × {qty} qty, SL ₹{sl}{linked}"
            except Exception as e:
                return f"Journal trade error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "scan_universe":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                days = tool_input.get("days", 20)
                direction = tool_input.get("direction", "gainers")
                limit = min(tool_input.get("limit", 10), 50)
                min_price = tool_input.get("min_price", 50)
                min_vol = tool_input.get("min_volume", 100000)
                sector = tool_input.get("sector")

                sector_join = ""
                sector_label = ""
                if sector:
                    sector_join = f"JOIN index_constituents ic ON u.security_id = ic.security_id AND LOWER(ic.index_display) LIKE LOWER('%{sector}%')"
                    sector_label = f" in {sector.upper()} sector"

                cur.execute(f"""
                    WITH latest AS (
                        SELECT DISTINCT ON (security_id) security_id, close as latest_close, date as latest_date
                        FROM candles_daily ORDER BY security_id, date DESC
                    ),
                    past AS (
                        SELECT DISTINCT ON (security_id) security_id, close as past_close
                        FROM candles_daily WHERE date <= CURRENT_DATE - INTERVAL '{days} days'
                        ORDER BY security_id, date DESC
                    ),
                    vol AS (
                        SELECT security_id, AVG(volume) as avg_vol
                        FROM candles_daily WHERE date >= CURRENT_DATE - INTERVAL '20 days'
                        GROUP BY security_id
                    )
                    SELECT DISTINCT u.symbol, u.company_name, l.latest_close as price,
                        ROUND(((l.latest_close - p.past_close) / NULLIF(p.past_close, 0) * 100)::numeric, 2) as change_pct,
                        ROUND(v.avg_vol::numeric) as avg_volume
                    FROM latest l
                    JOIN past p ON l.security_id = p.security_id
                    JOIN vol v ON l.security_id = v.security_id
                    JOIN stock_universe u ON l.security_id = u.security_id
                    {sector_join}
                    WHERE p.past_close > 0 AND l.latest_close >= %s AND v.avg_vol >= %s
                    ORDER BY change_pct {"DESC" if direction != "losers" else "ASC"}
                    LIMIT %s
                """, (min_price, min_vol, limit))
                rows = [dict(r) for r in cur.fetchall()]
                _cdb(conn)

                if not rows:
                    return f"No stocks found{sector_label} (min price ₹{min_price}, {days}d lookback)"
                lines = [f"═══ TOP {len(rows)} {'GAINERS' if direction != 'losers' else 'LOSERS'}{sector_label} — Last {days} Trading Days ═══"]
                for i, r in enumerate(rows, 1):
                    lines.append(f"  {i}. {r['symbol']} ({r['company_name'][:25]}): {r['change_pct']:+.2f}% | ₹{r['price']:,.1f} | Vol {r['avg_volume']:,.0f}")
                return "\n".join(lines)
            except Exception as e:
                return f"Universe scan error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_scoring_history":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                symbol = tool_input.get("symbol")
                limit = min(tool_input.get("limit", 10), 30)
                min_score = tool_input.get("min_score")
                max_score = tool_input.get("max_score")
                setup_type = tool_input.get("setup_type")
                full = tool_input.get("full_breakdown", False)
                stats = tool_input.get("stats_mode", False)

                if stats:
                    cur.execute("""
                        SELECT COUNT(*) as total, ROUND(AVG(final_score)::numeric, 1) as avg_score,
                            MAX(final_score) as max_score, MIN(final_score) as min_score,
                            ROUND(AVG(linearity_score)::numeric, 1) as avg_linearity,
                            ROUND(AVG(symmetry_score)::numeric, 1) as avg_symmetry,
                            ROUND(AVG(sector_strength_score)::numeric, 1) as avg_sector_str,
                            ROUND(AVG(institutional_participation_score)::numeric, 1) as avg_inst,
                            ROUND(AVG(relative_strength_score)::numeric, 1) as avg_rs,
                            ROUND(AVG(liquidity_score)::numeric, 1) as avg_liq,
                            ROUND(AVG(adr_score)::numeric, 1) as avg_adr,
                            ROUND(AVG(market_cap_score)::numeric, 1) as avg_mcap,
                            ROUND(AVG(market_trend_score)::numeric, 1) as avg_trend,
                            ROUND(AVG(ferocity_score)::numeric, 1) as avg_ferocity,
                            ROUND(AVG(magnitude_score)::numeric, 1) as avg_magnitude,
                            COUNT(*) FILTER (WHERE final_score >= 80) as above_80,
                            COUNT(*) FILTER (WHERE final_score >= 70 AND final_score < 80) as s70_80,
                            COUNT(*) FILTER (WHERE final_score >= 60 AND final_score < 70) as s60_70,
                            COUNT(*) FILTER (WHERE final_score < 60) as below_60
                        FROM submissions WHERE user_id = %s
                    """, (g.user_id,))
                    s = dict(cur.fetchone() or {})
                    _cdb(conn)
                    params_ranked = sorted([
                        ("Linearity", s["avg_linearity"]), ("Symmetry", s["avg_symmetry"]),
                        ("Sector Str", s["avg_sector_str"]), ("Inst. Part", s["avg_inst"]),
                        ("Rel Strength", s["avg_rs"]), ("Liquidity", s["avg_liq"]),
                        ("ADR", s["avg_adr"]), ("Mkt Cap", s["avg_mcap"]),
                        ("Mkt Trend", s["avg_trend"]), ("Ferocity", s["avg_ferocity"]),
                        ("Magnitude", s["avg_magnitude"])
                    ], key=lambda x: float(x[1] or 0))
                    lines = [f"═══ SCORING STATS ({s['total']} submissions) ═══",
                        f"Avg Score: {s['avg_score']}/100 | Max: {s['max_score']} | Min: {s['min_score']}",
                        f"Distribution: {s['above_80']} above 80 | {s['s70_80']} in 70-80 | {s['s60_70']} in 60-70 | {s['below_60']} below 60",
                        f"\nWeakest parameters: {params_ranked[0][0]} ({params_ranked[0][1]}), {params_ranked[1][0]} ({params_ranked[1][1]})",
                        f"Strongest: {params_ranked[-1][0]} ({params_ranked[-1][1]}), {params_ranked[-2][0]} ({params_ranked[-2][1]})"]
                    return "\n".join(lines)

                conditions, params = ["user_id = %s"], [g.user_id]
                if symbol:
                    conditions.append("LOWER(symbol) = LOWER(%s)")
                    params.append(symbol)
                if min_score is not None:
                    conditions.append("final_score >= %s")
                    params.append(min_score)
                if max_score is not None:
                    conditions.append("final_score <= %s")
                    params.append(max_score)
                if setup_type:
                    conditions.append("setup_type = %s")
                    params.append(setup_type)
                where = f"WHERE {' AND '.join(conditions)}"

                if full:
                    cur.execute(f"""SELECT symbol, final_score, setup_type, sector, market_price, market_cap, liquidity, adr,
                        linearity_score, symmetry_score, sector_strength_score, institutional_participation_score,
                        relative_strength_score, liquidity_score, adr_score, market_cap_score, market_trend_score,
                        ferocity_score, magnitude_score, move_ema_score, move_quality_score, extension_base_score,
                        linearity, sector_strength, symmetry, institutional_participation, relative_strength,
                        market_trend, move_percentage, move_days, timestamp
                        FROM submissions {where} ORDER BY timestamp DESC LIMIT %s""", params + [limit])
                else:
                    cur.execute(f"""SELECT symbol, final_score, setup_type, sector, timestamp, traded
                        FROM submissions {where} ORDER BY timestamp DESC LIMIT %s""", params + [limit])
                rows = [dict(r) for r in cur.fetchall()]
                _cdb(conn)
                if not rows:
                    return f"No scoring results found" + (f" for {symbol}" if symbol else "")

                lines = [f"═══ SCORING HISTORY ({len(rows)} results) ═══"]
                for r in rows:
                    dt = str(r.get("timestamp", ""))[:10]
                    traded = " [TRADED]" if r.get("traded") else ""
                    lines.append(f"  {r['symbol']}: {r.get('final_score', 0)}/100 | {r.get('setup_type', '?')} | {r.get('sector', '')} | {dt}{traded}")
                    if full:
                        lines.append(f"    Price ₹{r.get('market_price', 0)} | MCap {r.get('market_cap', 0)} | Liq {r.get('liquidity', 0)} | ADR {r.get('adr', 0)}")
                        lines.append(f"    Scores: Lin {r.get('linearity_score', 0)} | Sym {r.get('symmetry_score', 0)} | Sect {r.get('sector_strength_score', 0)} | Inst {r.get('institutional_participation_score', 0)} | RS {r.get('relative_strength_score', 0)} | Liq {r.get('liquidity_score', 0)} | ADR {r.get('adr_score', 0)} | MCap {r.get('market_cap_score', 0)} | Trend {r.get('market_trend_score', 0)} | Ferocity {r.get('ferocity_score', 0)} | Mag {r.get('magnitude_score', 0)}")
                return "\n".join(lines)
            except Exception as e:
                return f"Scoring history error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_sector_performance":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                days = tool_input.get("days", 20)
                sector = tool_input.get("sector")

                sector_filter = ""
                params = []
                if sector:
                    sector_filter = "AND LOWER(index_name) LIKE LOWER(%s)"
                    params.append(f"%{sector}%")

                cur.execute(f"""
                    WITH latest AS (
                        SELECT DISTINCT ON (index_name) index_name, close as latest_close, date
                        FROM candles_indices ORDER BY index_name, date DESC
                    ),
                    past AS (
                        SELECT DISTINCT ON (index_name) index_name, close as past_close
                        FROM candles_indices WHERE date <= CURRENT_DATE - INTERVAL '{days} days'
                        ORDER BY index_name, date DESC
                    )
                    SELECT l.index_name, l.latest_close as price,
                        ROUND(((l.latest_close - p.past_close) / NULLIF(p.past_close, 0) * 100)::numeric, 2) as change_pct
                    FROM latest l JOIN past p ON l.index_name = p.index_name
                    WHERE p.past_close > 0 {sector_filter}
                    ORDER BY change_pct DESC
                """, params)
                rows = [dict(r) for r in cur.fetchall()]
                _cdb(conn)
                if not rows:
                    return f"No sector data found"
                lines = [f"═══ SECTOR PERFORMANCE — Last {days} Days ═══"]
                for i, r in enumerate(rows, 1):
                    lines.append(f"  {i}. {r['index_name']}: {r['change_pct']:+.2f}% (₹{r['price']:,.1f})")
                return "\n".join(lines)
            except Exception as e:
                return f"Sector performance error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_monthly_pl":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                fy = tool_input.get("fy", "2025-26")
                fy_monthly = {"2021-22": "legacy_monthly_fy2122", "2022-23": "legacy_monthly_fy2223", "2023-24": "legacy_monthly_fy2324", "2024-25": "legacy_monthly_fy2425", "2025-26": "legacy_monthly_summary"}
                tbl = fy_monthly.get(fy, "legacy_monthly_summary")
                cur.execute(f"SELECT month_label, after_charges, scripts_traded, win_rate, approx_trades FROM {tbl} ORDER BY month_order")
                rows = [dict(r) for r in cur.fetchall()]
                _cdb(conn)
                if not rows:
                    return f"No monthly data for FY {fy}"
                best = max(rows, key=lambda x: float(x.get("after_charges") or 0))
                worst = min(rows, key=lambda x: float(x.get("after_charges") or 0))
                lines = [f"═══ MONTHLY P&L — FY {fy} ═══"]
                for r in rows:
                    pct = float(r.get("after_charges") or 0)
                    lines.append(f"  {r['month_label']}: {pct:+.2f}% | {r.get('approx_trades', 0)} trades | WR {r.get('win_rate', 0):.0f}%")
                lines.append(f"\nBest: {best['month_label']} ({float(best['after_charges'] or 0):+.2f}%)")
                lines.append(f"Worst: {worst['month_label']} ({float(worst['after_charges'] or 0):+.2f}%)")
                return "\n".join(lines)
            except Exception as e:
                return f"Monthly P&L error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_market_regime":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                cur.execute("SELECT regime, changed_at, notes FROM market_regime_history ORDER BY changed_at DESC LIMIT 10")
                rows = [dict(r) for r in cur.fetchall()]
                # Also get current regime from active positions
                cur.execute("SELECT DISTINCT market_regime FROM positions WHERE status = 'active' AND market_regime IS NOT NULL AND user_id = %s LIMIT 1", (g.user_id,))
                current = cur.fetchone()
                _cdb(conn)
                current_regime = current["market_regime"] if current else "unknown"
                lines = [f"═══ MARKET REGIME ═══", f"Current: {current_regime.upper()}"]
                if rows:
                    lines.append(f"\nHistory (last {len(rows)}):")
                    for r in rows:
                        lines.append(f"  {r.get('regime', '?').upper()} — {str(r.get('changed_at', ''))[:10]} {r.get('notes', '') or ''}")
                else:
                    lines.append("No regime history recorded yet.")
                return "\n".join(lines)
            except Exception as e:
                return f"Regime error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "search_trades":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                fy = tool_input.get("fy", "all")
                uid = g.user_id
                fy_tables = {
                    "2020-21": "legacy_trades_fy2021",
                    "2021-22": "legacy_trades_fy2122",
                    "2022-23": "legacy_trades_fy2223",
                    "2023-24": "legacy_trades_fy2324",
                    "2024-25": "legacy_trades_fy2425",
                    "2025-26": "legacy_trades",
                    "2026-27": "journal_trades_computed",
                }
                if fy == "all":
                    tbl = f"(SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner, quantity, buy_value, sell_value, impact_on_pf, '2020-21' as fy FROM legacy_trades_fy2021 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner, quantity, buy_value, sell_value, impact_on_pf, '2021-22' FROM legacy_trades_fy2122 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner, quantity, buy_value, sell_value, impact_on_pf, '2022-23' FROM legacy_trades_fy2223 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner, quantity, buy_value, sell_value, impact_on_pf, '2023-24' FROM legacy_trades_fy2324 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner, quantity, buy_value, sell_value, impact_on_pf, '2024-25' FROM legacy_trades_fy2425 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner, quantity, buy_value, sell_value, impact_on_pf, '2025-26' FROM legacy_trades WHERE user_id = '{uid}' UNION ALL SELECT symbol, realized_pl::real, realized_pl_pct::real, month_label, is_winner, quantity::real, buy_value::real, sell_value::real, impact_on_pf::real, '2026-27' FROM journal_trades_computed WHERE user_id = '{uid}') combined"
                else:
                    tbl_name = fy_tables.get(fy, "legacy_trades")
                    if tbl_name in ("legacy_trades", "journal_trades_computed"):
                        tbl = f"(SELECT *, '{fy}' as fy FROM {tbl_name} WHERE user_id = '{uid}') t"
                    else:
                        tbl = f"(SELECT *, '{fy}' as fy FROM {tbl_name}) t"

                conditions = []
                params = []
                sym = tool_input.get("symbol")
                if sym:
                    conditions.append("LOWER(symbol) LIKE LOWER(%s)")
                    params.append(f"%{sym}%")
                if tool_input.get("min_pl") is not None:
                    conditions.append("realized_pl >= %s")
                    params.append(tool_input["min_pl"])
                if tool_input.get("max_pl") is not None:
                    conditions.append("realized_pl <= %s")
                    params.append(tool_input["max_pl"])
                if tool_input.get("min_move_pct") is not None:
                    conditions.append("realized_pl_pct >= %s")
                    params.append(tool_input["min_move_pct"])
                if tool_input.get("max_move_pct") is not None:
                    conditions.append("realized_pl_pct <= %s")
                    params.append(tool_input["max_move_pct"])
                if tool_input.get("month_label"):
                    conditions.append("LOWER(month_label) LIKE LOWER(%s)")
                    params.append(f"%{tool_input['month_label']}%")
                if tool_input.get("winners_only"):
                    conditions.append("is_winner = TRUE")
                if tool_input.get("losers_only"):
                    conditions.append("is_winner = FALSE")

                where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
                sort_map = {"pl_desc": "realized_pl DESC", "pl_asc": "realized_pl ASC",
                            "move_desc": "realized_pl_pct DESC", "move_asc": "realized_pl_pct ASC",
                            "symbol": "symbol ASC"}
                sort = sort_map.get(tool_input.get("sort_by", "pl_desc"), "realized_pl DESC")
                limit = min(tool_input.get("limit", 20), 50)

                if tool_input.get("group_by_symbol"):
                    cur.execute(f"""
                        SELECT symbol, COUNT(*) as trades, SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) as wins,
                            ROUND(SUM(realized_pl)::numeric) as total_pl,
                            ROUND(AVG(realized_pl_pct)::numeric, 2) as avg_move
                        FROM {tbl} {where} GROUP BY symbol ORDER BY trades DESC LIMIT %s
                    """, params + [limit])
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ TRADE SEARCH — Grouped by Stock ({len(rows)} stocks) ═══"]
                    for r in rows:
                        wr = round(int(r["wins"]) / max(int(r["trades"]), 1) * 100)
                        lines.append(f"  {r['symbol']}: {r['trades']} trades | WR {wr}% | P&L ₹{float(r['total_pl'] or 0):+,.0f} | Avg {float(r['avg_move'] or 0):+.2f}%")
                    return "\n".join(lines)
                else:
                    cur.execute(f"""
                        SELECT symbol, realized_pl, realized_pl_pct, month_label, is_winner, fy
                        FROM {tbl} {where} ORDER BY {sort} LIMIT %s
                    """, params + [limit])
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    total_pl = sum(float(r.get("realized_pl") or 0) for r in rows)
                    wins = sum(1 for r in rows if r.get("is_winner"))
                    lines = [f"═══ TRADE SEARCH ({len(rows)} results, Total P&L ₹{total_pl:+,.0f}, WR {round(wins/max(len(rows),1)*100)}%) ═══"]
                    for i, r in enumerate(rows, 1):
                        pl = float(r.get("realized_pl") or 0)
                        mv = float(r.get("realized_pl_pct") or 0)
                        lines.append(f"  {i}. {r['symbol']}: ₹{pl:+,.0f} ({mv:+.2f}%) | {r.get('month_label', '')} | FY {r.get('fy', '')}")
                    return "\n".join(lines)
            except Exception as e:
                return f"Trade search error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_stock_data":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                qt = tool_input.get("query_type")
                sym = tool_input.get("symbol", "")
                sid = tool_input.get("security_id", "")
                days = tool_input.get("days", 20)

                # Resolve security_id from symbol if needed
                if sym and not sid:
                    cur.execute("SELECT security_id FROM stock_universe WHERE LOWER(symbol) = LOWER(%s) OR LOWER(company_name) LIKE LOWER(%s) LIMIT 1", (sym, f"%{sym}%"))
                    m = cur.fetchone()
                    if m: sid = m["security_id"]
                    if not sid:
                        _cdb(conn)
                        return f"Stock '{sym}' not found in universe"

                if qt == "price_at_date":
                    dt = tool_input.get("date")
                    cur.execute("SELECT date, open, high, low, close, volume FROM candles_daily WHERE security_id = %s AND date <= %s ORDER BY date DESC LIMIT 1", (sid, dt))
                    r = dict(cur.fetchone() or {})
                    _cdb(conn)
                    return f"{sym} on {r.get('date')}: O={r.get('open'):.2f} H={r.get('high'):.2f} L={r.get('low'):.2f} C={r.get('close'):.2f} Vol={r.get('volume'):,}" if r else "No data"

                elif qt == "52w_high_low":
                    cur.execute("SELECT MAX(high) as hi, MIN(low) as lo FROM candles_daily WHERE security_id = %s AND date >= CURRENT_DATE - INTERVAL '252 days'", (sid,))
                    hl = dict(cur.fetchone() or {})
                    cur.execute("SELECT close FROM candles_daily WHERE security_id = %s ORDER BY date DESC LIMIT 1", (sid,))
                    latest = cur.fetchone()
                    cmp = float(latest["close"]) if latest else 0
                    hi, lo = float(hl.get("hi") or 0), float(hl.get("lo") or 0)
                    _cdb(conn)
                    return f"{sym}: 52W High ₹{hi:,.2f} | 52W Low ₹{lo:,.2f} | CMP ₹{cmp:,.2f} | {((cmp-hi)/hi*100):.1f}% from high | {((cmp-lo)/lo*100):.1f}% from low"

                elif qt == "returns":
                    cur.execute(f"""
                        WITH latest AS (SELECT close FROM candles_daily WHERE security_id = %s ORDER BY date DESC LIMIT 1),
                        past AS (SELECT close FROM candles_daily WHERE security_id = %s AND date <= CURRENT_DATE - INTERVAL '{days} days' ORDER BY date DESC LIMIT 1)
                        SELECT (SELECT close FROM latest) as now, (SELECT close FROM past) as then
                    """, (sid, sid))
                    r = dict(cur.fetchone() or {})
                    _cdb(conn)
                    now, then = float(r.get("now") or 0), float(r.get("then") or 0)
                    return f"{sym} {days}-day return: {((now-then)/max(then,1)*100):+.2f}% (₹{then:,.2f} → ₹{now:,.2f})" if then else "No data"

                elif qt == "adr":
                    cur.execute(f"SELECT AVG(high - low) as avg_range, AVG((high-low)/NULLIF(close,0)*100) as adr_pct FROM candles_daily WHERE security_id = %s AND date >= CURRENT_DATE - INTERVAL '{days} days'", (sid,))
                    r = dict(cur.fetchone() or {})
                    _cdb(conn)
                    return f"{sym} ADR ({days}d): ₹{float(r.get('avg_range') or 0):,.2f} ({float(r.get('adr_pct') or 0):.2f}%)"

                elif qt == "volume_analysis":
                    cur.execute(f"""
                        SELECT AVG(volume) as avg_vol FROM candles_daily WHERE security_id = %s AND date >= CURRENT_DATE - INTERVAL '{days} days'
                    """, (sid,))
                    avg = float((cur.fetchone() or {}).get("avg_vol") or 0)
                    cur.execute("SELECT volume FROM candles_daily WHERE security_id = %s ORDER BY date DESC LIMIT 1", (sid,))
                    today = float((cur.fetchone() or {}).get("volume") or 0)
                    _cdb(conn)
                    ratio = today / max(avg, 1)
                    spike = " ⚠️ VOLUME SPIKE!" if ratio > 2 else ""
                    return f"{sym} Volume: Today {today:,.0f} | {days}d Avg {avg:,.0f} | Ratio {ratio:.2f}x{spike}"

                elif qt == "ma_status":
                    cur.execute("""
                        SELECT close,
                            AVG(close) OVER (ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) as ma5,
                            AVG(close) OVER (ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) as ma10,
                            AVG(close) OVER (ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) as ma20,
                            AVG(close) OVER (ORDER BY date ROWS BETWEEN 49 PRECEDING AND CURRENT ROW) as ma50,
                            AVG(close) OVER (ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) as ma200
                        FROM candles_daily WHERE security_id = %s ORDER BY date DESC LIMIT 1
                    """, (sid,))
                    r = dict(cur.fetchone() or {})
                    _cdb(conn)
                    c = float(r.get("close") or 0)
                    def status(ma): return "✅ ABOVE" if c > float(ma or 0) else "❌ BELOW"
                    return f"{sym} ₹{c:,.2f} | 5MA ₹{float(r.get('ma5') or 0):,.2f} {status(r.get('ma5'))} | 10MA ₹{float(r.get('ma10') or 0):,.2f} {status(r.get('ma10'))} | 20MA ₹{float(r.get('ma20') or 0):,.2f} {status(r.get('ma20'))} | 50MA ₹{float(r.get('ma50') or 0):,.2f} {status(r.get('ma50'))} | 200MA ₹{float(r.get('ma200') or 0):,.2f} {status(r.get('ma200'))}"

                elif qt == "new_52w_highs":
                    cur.execute("""
                        WITH highs AS (
                            SELECT c.security_id, u.symbol, c.close as today_close,
                                MAX(c2.high) as high_52w
                            FROM candles_daily c
                            JOIN stock_universe u ON c.security_id = u.security_id
                            JOIN candles_daily c2 ON c.security_id = c2.security_id AND c2.date >= CURRENT_DATE - INTERVAL '252 days'
                            WHERE c.date = (SELECT MAX(date) FROM candles_daily)
                            GROUP BY c.security_id, u.symbol, c.close
                        )
                        SELECT symbol, today_close, high_52w FROM highs
                        WHERE today_close >= high_52w * 0.99
                        ORDER BY today_close / NULLIF(high_52w, 0) DESC LIMIT 30
                    """)
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ STOCKS AT/NEAR 52-WEEK HIGH ({len(rows)}) ═══"]
                    for r in rows:
                        lines.append(f"  {r['symbol']}: ₹{float(r['today_close']):,.2f} (52W High: ₹{float(r['high_52w']):,.2f})")
                    return "\n".join(lines)

                elif qt == "new_52w_lows":
                    cur.execute("""
                        WITH lows AS (
                            SELECT c.security_id, u.symbol, c.close as today_close,
                                MIN(c2.low) as low_52w
                            FROM candles_daily c
                            JOIN stock_universe u ON c.security_id = u.security_id
                            JOIN candles_daily c2 ON c.security_id = c2.security_id AND c2.date >= CURRENT_DATE - INTERVAL '252 days'
                            WHERE c.date = (SELECT MAX(date) FROM candles_daily)
                            GROUP BY c.security_id, u.symbol, c.close
                        )
                        SELECT symbol, today_close, low_52w FROM lows
                        WHERE today_close <= low_52w * 1.01
                        ORDER BY today_close / NULLIF(low_52w, 0) ASC LIMIT 30
                    """)
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ STOCKS AT/NEAR 52-WEEK LOW ({len(rows)}) ═══"]
                    for r in rows:
                        lines.append(f"  {r['symbol']}: ₹{float(r['today_close']):,.2f} (52W Low: ₹{float(r['low_52w']):,.2f})")
                    return "\n".join(lines)

                elif qt == "near_52w_high":
                    threshold = tool_input.get("threshold_pct", 5)
                    cur.execute("""
                        WITH data AS (
                            SELECT c.security_id, u.symbol, c.close,
                                MAX(c2.high) as high_52w
                            FROM candles_daily c
                            JOIN stock_universe u ON c.security_id = u.security_id
                            JOIN candles_daily c2 ON c.security_id = c2.security_id AND c2.date >= CURRENT_DATE - INTERVAL '252 days'
                            WHERE c.date = (SELECT MAX(date) FROM candles_daily)
                            GROUP BY c.security_id, u.symbol, c.close
                        )
                        SELECT symbol, close, high_52w,
                            ROUND(((high_52w - close) / NULLIF(close, 0) * 100)::numeric, 2) as pct_away
                        FROM data WHERE close >= high_52w * (1 - %s / 100.0)
                        ORDER BY pct_away ASC LIMIT 30
                    """, (threshold,))
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ STOCKS WITHIN {threshold}% OF 52W HIGH ({len(rows)}) ═══"]
                    for r in rows:
                        lines.append(f"  {r['symbol']}: ₹{float(r['close']):,.2f} | 52W High ₹{float(r['high_52w']):,.2f} | {r['pct_away']}% away")
                    return "\n".join(lines)

                else:
                    _cdb(conn)
                    return f"Unknown query_type: {qt}"
            except Exception as e:
                return f"Stock data error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_positions_advanced":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                qt = tool_input.get("query_type")
                limit = min(tool_input.get("limit", 20), 50)
                sort = tool_input.get("sort_by", "pnl")

                if qt == "closed_positions":
                    sort_map = {"pnl": "total_pnl DESC", "r_multiple": "current_r_multiple DESC",
                                "extension": "entry_extension_pct DESC", "value": "position_value DESC",
                                "created_at": "created_at DESC"}
                    cur.execute(f"""
                        SELECT stock_name, entry_price, exit_price, quantity, total_pnl, total_pnl_pct,
                            current_r_multiple, ma_grade, market_regime, created_at, exit_date,
                            EXTRACT(DAY FROM exit_date - created_at) as holding_days
                        FROM positions WHERE status = 'closed' AND user_id = %s
                        ORDER BY {sort_map.get(sort, 'total_pnl DESC')} LIMIT %s
                    """, (g.user_id, limit))
                    rows = [dict(r) for r in cur.fetchall()]
                    total_pl = sum(float(r.get("total_pnl") or 0) for r in rows)
                    wins = sum(1 for r in rows if float(r.get("total_pnl") or 0) > 0)
                    _cdb(conn)
                    lines = [f"═══ CLOSED POSITIONS ({len(rows)} | Total P&L ₹{total_pl:+,.0f} | WR {round(wins/max(len(rows),1)*100)}%) ═══"]
                    for r in rows:
                        pl = float(r.get("total_pnl") or 0)
                        pct = float(r.get("total_pnl_pct") or 0)
                        rm = float(r.get("current_r_multiple") or 0)
                        hd = int(r.get("holding_days") or 0)
                        lines.append(f"  {r['stock_name']}: ₹{pl:+,.0f} ({pct:+.1f}%) | {rm:.1f}R | {hd}d | {r.get('ma_grade', '-')} | {r.get('market_regime', '-')}")
                    return "\n".join(lines)

                elif qt == "risk_summary":
                    cur.execute("""
                        SELECT COUNT(*) as count,
                            COALESCE(SUM(position_value), 0) as total_value,
                            COALESCE(SUM((entry_price - stop_loss) * quantity), 0) as total_risk_amount,
                            COALESCE(AVG(risk_pct), 0) as avg_risk_pct,
                            MAX((entry_price - stop_loss) / NULLIF(entry_price, 0) * 100) as widest_sl_pct,
                            MIN((entry_price - stop_loss) / NULLIF(entry_price, 0) * 100) as tightest_sl_pct,
                            MAX(entry_extension_pct) as max_extension,
                            SUM(CASE WHEN current_price < entry_price THEN 1 ELSE 0 END) as in_loss,
                            SUM(CASE WHEN current_price >= entry_price THEN 1 ELSE 0 END) as in_profit
                        FROM positions WHERE status = 'active' AND user_id = %s
                    """, (g.user_id,))
                    r = dict(cur.fetchone() or {})
                    _cdb(conn)
                    risk_amt = float(r.get("total_risk_amount") or 0)
                    total_val = float(r.get("total_value") or 0)
                    heat = risk_amt / 50000000 * 100
                    lines = [f"═══ PORTFOLIO RISK SUMMARY ═══",
                        f"Active Positions: {r['count']}",
                        f"Capital Deployed: ₹{total_val:,.0f} ({total_val/50000000*100:.1f}%)",
                        f"Total Risk (all SLs): ₹{risk_amt:,.0f}",
                        f"Portfolio Heat: {heat:.2f}% of capital",
                        f"Avg Risk/Trade: {float(r.get('avg_risk_pct') or 0):.1f}%",
                        f"Widest SL: {float(r.get('widest_sl_pct') or 0):.1f}% | Tightest: {float(r.get('tightest_sl_pct') or 0):.1f}%",
                        f"Max Extension: {float(r.get('max_extension') or 0):.1f}%",
                        f"In Profit: {r.get('in_profit', 0)} | In Loss: {r.get('in_loss', 0)}"]
                    return "\n".join(lines)

                elif qt == "danger_zone":
                    cur.execute("""
                        SELECT stock_name, current_price, stop_loss, entry_price,
                            ROUND(((current_price - stop_loss) / NULLIF(current_price, 0) * 100)::numeric, 2) as pct_to_sl,
                            ROUND(((current_price - entry_price) / NULLIF(entry_price, 0) * 100)::numeric, 2) as pnl_pct,
                            current_r_multiple
                        FROM positions WHERE status = 'active' AND current_price > 0 AND stop_loss > 0 AND user_id = %s
                        ORDER BY (current_price - stop_loss) / NULLIF(current_price, 0) ASC
                        LIMIT %s
                    """, (g.user_id, limit))
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ DANGER ZONE — Closest to SL ({len(rows)} positions) ═══"]
                    for r in rows:
                        pct = float(r.get("pct_to_sl") or 0)
                        flag = "🔴" if pct < 2 else "🟡" if pct < 5 else "🟢"
                        lines.append(f"  {flag} {r['stock_name']}: {pct}% to SL | CMP ₹{float(r.get('current_price') or 0):,.2f} | SL ₹{float(r.get('stop_loss') or 0):,.2f} | P&L {float(r.get('pnl_pct') or 0):+.1f}%")
                    return "\n".join(lines)

                elif qt == "concentration":
                    cur.execute("""
                        SELECT stock_name, position_value,
                            ROUND((position_value / NULLIF(SUM(position_value) OVER (), 0) * 100)::numeric, 1) as pct_of_portfolio
                        FROM positions WHERE status = 'active' AND user_id = %s
                        ORDER BY position_value DESC
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    total = sum(float(r.get("position_value") or 0) for r in rows)
                    lines = [f"═══ CONCENTRATION ANALYSIS ({len(rows)} positions, ₹{total:,.0f} deployed) ═══"]
                    cum = 0
                    for r in rows:
                        pct = float(r.get("pct_of_portfolio") or 0)
                        cum += pct
                        lines.append(f"  {r['stock_name']}: ₹{float(r.get('position_value') or 0):,.0f} ({pct}%) | Cumulative {cum:.1f}%")
                    top3 = sum(float(r.get("pct_of_portfolio") or 0) for r in rows[:3])
                    lines.append(f"\nTop 3 concentration: {top3:.1f}%")
                    return "\n".join(lines)

                elif qt == "sell_system_status":
                    cur.execute("""
                        SELECT stock_name, bucket_sold_pct, bucket_cap, first_sell_done,
                            entry_extension_pct, first_sell_zone, reversal_detect_from,
                            daily_sell_from, trail_remaining_pct, market_regime
                        FROM positions WHERE status = 'active' AND user_id = %s
                        ORDER BY entry_extension_pct DESC
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ SELL SYSTEM STATUS ({len(rows)} positions) ═══"]
                    for r in rows:
                        ext = float(r.get("entry_extension_pct") or 0)
                        bucket = float(r.get("bucket_sold_pct") or 0)
                        fsz = float(r.get("first_sell_zone") or 35)
                        flag = "🔴 OVERDUE" if ext > fsz and not r.get("first_sell_done") else "🟢"
                        lines.append(f"  {flag} {r['stock_name']}: Ext {ext:.1f}% | Bucket {bucket:.0f}%/{float(r.get('bucket_cap') or 66):.0f}% | 1st Sell {'✅ Done' if r.get('first_sell_done') else f'at {fsz:.0f}%'} | {r.get('market_regime', 'bull')}")
                    return "\n".join(lines)

                elif qt == "ma_breakdown":
                    cur.execute("""
                        SELECT ma_followed, ma_grade, COUNT(*) as cnt,
                            AVG(current_r_multiple) as avg_r, AVG(entry_extension_pct) as avg_ext
                        FROM positions WHERE status = 'active' AND user_id = %s
                        GROUP BY ma_followed, ma_grade ORDER BY ma_followed, ma_grade
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ MA FOLLOWING BREAKDOWN ═══"]
                    for r in rows:
                        lines.append(f"  {r.get('ma_followed', '?')} ({r.get('ma_grade', '?')}): {r['cnt']} positions | Avg R {float(r.get('avg_r') or 0):.2f} | Avg Ext {float(r.get('avg_ext') or 0):.1f}%")
                    return "\n".join(lines)

                elif qt == "sort_active":
                    sort_map = {"pnl": "(current_price - entry_price) * quantity DESC",
                                "r_multiple": "current_r_multiple DESC", "extension": "entry_extension_pct DESC",
                                "value": "position_value DESC", "risk": "risk_pct DESC", "created_at": "created_at ASC"}
                    cur.execute(f"""
                        SELECT stock_name, entry_price, current_price, quantity,
                            ROUND(((current_price - entry_price) * quantity)::numeric) as unr_pnl,
                            ROUND(((current_price - entry_price) / NULLIF(entry_price, 0) * 100)::numeric, 2) as pnl_pct,
                            current_r_multiple, entry_extension_pct, bucket_sold_pct, ma_grade, market_regime
                        FROM positions WHERE status = 'active' AND user_id = %s
                        ORDER BY {sort_map.get(sort, '(current_price - entry_price) * quantity DESC')}
                        LIMIT %s
                    """, (g.user_id, limit))
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ ACTIVE POSITIONS sorted by {sort} ({len(rows)}) ═══"]
                    for r in rows:
                        lines.append(f"  {r['stock_name']}: ₹{float(r.get('unr_pnl') or 0):+,.0f} ({float(r.get('pnl_pct') or 0):+.1f}%) | {float(r.get('current_r_multiple') or 0):.1f}R | Ext {float(r.get('entry_extension_pct') or 0):.1f}% | Bucket {float(r.get('bucket_sold_pct') or 0):.0f}%")
                    return "\n".join(lines)

                else:
                    _cdb(conn)
                    return f"Unknown query_type: {qt}"
            except Exception as e:
                return f"Positions advanced error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_advanced_stats":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                fy = tool_input.get("fy", "all")
                st = tool_input.get("stat_type", "all_stats")

                uid = g.user_id
                fy_tables = {
                    "2020-21": "legacy_trades_fy2021",
                    "2021-22": "legacy_trades_fy2122",
                    "2022-23": "legacy_trades_fy2223",
                    "2023-24": "legacy_trades_fy2324",
                    "2024-25": "legacy_trades_fy2425",
                    "2025-26": "legacy_trades",
                }
                if fy == "all":
                    tbl = f"(SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label, impact_on_pf FROM legacy_trades_fy2021 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label, impact_on_pf FROM legacy_trades_fy2122 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label, impact_on_pf FROM legacy_trades_fy2223 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label, impact_on_pf FROM legacy_trades_fy2324 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label, impact_on_pf FROM legacy_trades_fy2425 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label, impact_on_pf FROM legacy_trades WHERE user_id = '{uid}') combined"
                else:
                    tbl_name = fy_tables.get(fy, "legacy_trades")
                    if tbl_name == "legacy_trades":
                        tbl = f"(SELECT * FROM legacy_trades WHERE user_id = '{uid}') legacy_trades"
                    else:
                        tbl = tbl_name

                lines = [f"═══ ADVANCED STATS — FY {fy} ═══"]

                if st in ("streaks", "all_stats"):
                    cur.execute(f"SELECT is_winner FROM {tbl}")
                    trades = [r["is_winner"] for r in cur.fetchall()]
                    if trades:
                        max_w, max_l, cur_streak = 0, 0, 0
                        cw, cl = 0, 0
                        for w in trades:
                            if w: cw += 1; cl = 0
                            else: cl += 1; cw = 0
                            max_w = max(max_w, cw); max_l = max(max_l, cl)
                        cur_streak = cw if trades[-1] else -cl
                        lines.append(f"\n📊 STREAKS: Max Win {max_w} | Max Loss {max_l} | Current {'W' if cur_streak > 0 else 'L'}{abs(cur_streak)}")

                if st in ("drawdown", "all_stats"):
                    cur.execute(f"SELECT realized_pl, month_label FROM {tbl}")
                    trades = [dict(r) for r in cur.fetchall()]
                    if trades:
                        cumulative = 0; peak = 0; max_dd = 0; max_dd_from = 0
                        for t in trades:
                            cumulative += float(t["realized_pl"] or 0)
                            if cumulative > peak: peak = cumulative
                            dd = peak - cumulative
                            if dd > max_dd: max_dd = dd; max_dd_from = peak
                        dd_pct = max_dd / max(max_dd_from, 1) * 100
                        lines.append(f"📉 DRAWDOWN: Max ₹{max_dd:,.0f} ({dd_pct:.1f}% from peak ₹{max_dd_from:,.0f}) | Current cumulative ₹{cumulative:,.0f}")

                if st in ("expectancy", "all_stats"):
                    cur.execute(f"""
                        SELECT COUNT(*) as n, AVG(realized_pl) as avg_pl,
                            AVG(CASE WHEN is_winner THEN realized_pl END) as avg_w,
                            AVG(CASE WHEN NOT is_winner THEN ABS(realized_pl) END) as avg_l,
                            SUM(CASE WHEN is_winner THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*), 0) as wr
                        FROM {tbl}
                    """)
                    r = dict(cur.fetchone() or {})
                    exp = float(r.get("avg_pl") or 0)
                    wr = float(r.get("wr") or 0)
                    avg_w = float(r.get("avg_w") or 0)
                    avg_l = float(r.get("avg_l") or 1)
                    payoff = avg_w / max(avg_l, 1)
                    kelly = wr - ((1 - wr) / max(payoff, 0.01))
                    lines.append(f"🎯 EXPECTANCY: ₹{exp:+,.0f}/trade | WR {wr*100:.1f}% | Avg Win ₹{avg_w:,.0f} | Avg Loss ₹{avg_l:,.0f} | Payoff {payoff:.2f}x | Kelly {kelly*100:.1f}%")

                if st in ("consistency", "all_stats"):
                    cur.execute(f"""
                        SELECT month_label, SUM(realized_pl) as pl, COUNT(*) as n
                        FROM {tbl} GROUP BY month_label ORDER BY MIN(realized_pl_pct)
                    """)
                    months = [dict(r) for r in cur.fetchall()]
                    if months:
                        pls = [float(m["pl"]) for m in months]
                        import statistics
                        avg = statistics.mean(pls)
                        std = statistics.stdev(pls) if len(pls) > 1 else 0
                        positive = sum(1 for p in pls if p > 0)
                        sharpe = (avg / max(std, 1)) * (12 ** 0.5) if std > 0 else 0
                        lines.append(f"📈 CONSISTENCY: {positive}/{len(months)} months positive | Avg ₹{avg:+,.0f}/mo | StdDev ₹{std:,.0f} | Sharpe {sharpe:.2f}")

                _cdb(conn)
                return "\n".join(lines) if len(lines) > 1 else "No data available for the selected period"
            except Exception as e:
                return f"Advanced stats error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_behavior_analysis":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                at = tool_input.get("analysis_type", "full_behavior")
                lines = [f"═══ BEHAVIOR ANALYSIS ═══"]

                if at in ("entry_type_comparison", "full_behavior"):
                    cur.execute("""
                        SELECT entry_type, COUNT(*) as n,
                            SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) as wins,
                            ROUND(AVG(realized_pl)::numeric) as avg_pl,
                            ROUND(AVG(realized_pl_pct)::numeric, 2) as avg_move
                        FROM journal_trades_computed WHERE entry_type IS NOT NULL AND user_id = %s
                        GROUP BY entry_type ORDER BY n DESC
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    if rows:
                        lines.append("\n📊 ENTRY TYPE COMPARISON:")
                        for r in rows:
                            wr = round(int(r["wins"]) / max(int(r["n"]), 1) * 100)
                            lines.append(f"  {r['entry_type']}: {r['n']} trades | WR {wr}% | Avg P&L ₹{float(r['avg_pl'] or 0):+,.0f} | Avg Move {float(r['avg_move'] or 0):+.2f}%")

                if at in ("rating_analysis", "full_behavior"):
                    cur.execute("""
                        SELECT rating as self_rating, COUNT(*) as n,
                            SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) as wins,
                            ROUND(AVG(realized_pl)::numeric) as avg_pl
                        FROM journal_trades_computed WHERE rating IS NOT NULL AND rating > 0 AND user_id = %s
                        GROUP BY rating ORDER BY rating DESC
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    if rows:
                        lines.append("\n⭐ RATING ANALYSIS:")
                        for r in rows:
                            wr = round(int(r["wins"]) / max(int(r["n"]), 1) * 100)
                            lines.append(f"  {r['self_rating']}★: {r['n']} trades | WR {wr}% | Avg P&L ₹{float(r['avg_pl'] or 0):+,.0f}")

                if at in ("plan_followed", "full_behavior"):
                    cur.execute("""
                        SELECT plan_followed, COUNT(*) as n,
                            SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) as wins,
                            ROUND(AVG(realized_pl)::numeric) as avg_pl
                        FROM journal_trades_computed WHERE plan_followed IS NOT NULL AND plan_followed != '' AND user_id = %s
                        GROUP BY plan_followed ORDER BY n DESC
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    if rows:
                        lines.append("\n📋 PLAN FOLLOWED:")
                        for r in rows:
                            wr = round(int(r["wins"]) / max(int(r["n"]), 1) * 100)
                            lines.append(f"  {r['plan_followed']}: {r['n']} trades | WR {wr}% | Avg P&L ₹{float(r['avg_pl'] or 0):+,.0f}")

                if at in ("exit_trigger_breakdown", "full_behavior"):
                    cur.execute("""
                        SELECT exit_trigger, COUNT(*) as n,
                            SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) as wins,
                            ROUND(AVG(realized_pl)::numeric) as avg_pl
                        FROM journal_trades_computed WHERE exit_trigger IS NOT NULL AND exit_trigger != '' AND user_id = %s
                        GROUP BY exit_trigger ORDER BY n DESC
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    if rows:
                        lines.append("\n🎯 EXIT TRIGGER BREAKDOWN:")
                        for r in rows:
                            wr = round(int(r["wins"]) / max(int(r["n"]), 1) * 100)
                            lines.append(f"  {r['exit_trigger']}: {r['n']} trades | WR {wr}% | Avg P&L ₹{float(r['avg_pl'] or 0):+,.0f}")

                if at in ("setup_analysis", "full_behavior"):
                    cur.execute("""
                        SELECT sector, COUNT(*) as n,
                            SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) as wins,
                            ROUND(SUM(realized_pl)::numeric) as total_pl
                        FROM journal_trades_computed WHERE sector IS NOT NULL AND sector != '' AND user_id = %s
                        GROUP BY sector ORDER BY total_pl DESC
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    if rows:
                        lines.append("\n🏭 SECTOR PERFORMANCE:")
                        for r in rows:
                            wr = round(int(r["wins"]) / max(int(r["n"]), 1) * 100)
                            lines.append(f"  {r['sector']}: {r['n']} trades | WR {wr}% | Total P&L ₹{float(r['total_pl'] or 0):+,.0f}")

                _cdb(conn)
                return "\n".join(lines) if len(lines) > 1 else "No journal behavior data available yet. Journal trades need entry_type, self_rating, plan_followed fields populated."
            except Exception as e:
                return f"Behavior analysis error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_sector_deep":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                qt = tool_input.get("query_type")
                ma_period = tool_input.get("ma_period", 20)
                days = tool_input.get("days", 20)

                if qt == "market_breadth":
                    cur.execute(f"""
                        WITH latest_with_ma AS (
                            SELECT security_id, close,
                                AVG(close) OVER (PARTITION BY security_id ORDER BY date ROWS BETWEEN {ma_period - 1} PRECEDING AND CURRENT ROW) as ma
                            FROM candles_daily
                            WHERE date = (SELECT MAX(date) FROM candles_daily)
                        )
                        SELECT COUNT(*) as total,
                            SUM(CASE WHEN close > ma THEN 1 ELSE 0 END) as above,
                            SUM(CASE WHEN close <= ma THEN 1 ELSE 0 END) as below
                        FROM latest_with_ma WHERE ma IS NOT NULL
                    """)
                    r = dict(cur.fetchone() or {})
                    _cdb(conn)
                    total = int(r.get("total") or 1)
                    above = int(r.get("above") or 0)
                    pct = round(above / max(total, 1) * 100, 1)
                    return f"═══ MARKET BREADTH ({ma_period}MA) ═══\n{above} / {total} stocks above {ma_period}MA ({pct}%)\n{'🟢 HEALTHY' if pct > 50 else '🔴 WEAK'} breadth"

                elif qt == "leading_sectors":
                    cur.execute("SELECT sectors, regime, note, updated_at FROM leading_sectors ORDER BY updated_at DESC LIMIT 1")
                    r = cur.fetchone()
                    _cdb(conn)
                    if r:
                        sectors = r.get("sectors", [])
                        return f"═══ LEADING SECTORS ═══\nRegime: {r.get('regime', '?')}\nLeaders: {', '.join(sectors) if isinstance(sectors, list) else str(sectors)}\nNote: {r.get('note', '')}\nUpdated: {str(r.get('updated_at', ''))[:10]}"
                    return "No leading sectors data. Update via market regime settings."

                elif qt == "sector_breadth":
                    cur.execute(f"""
                        WITH stock_ma AS (
                            SELECT c.security_id, c.close,
                                AVG(c.close) OVER (PARTITION BY c.security_id ORDER BY c.date ROWS BETWEEN {ma_period - 1} PRECEDING AND CURRENT ROW) as ma
                            FROM candles_daily c
                            WHERE c.date = (SELECT MAX(date) FROM candles_daily)
                        )
                        SELECT ic.index_display as sector,
                            COUNT(*) as total,
                            SUM(CASE WHEN sm.close > sm.ma THEN 1 ELSE 0 END) as above,
                            ROUND((SUM(CASE WHEN sm.close > sm.ma THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100), 0) as pct_above
                        FROM index_constituents ic
                        JOIN stock_ma sm ON ic.security_id = sm.security_id
                        WHERE sm.ma IS NOT NULL
                        GROUP BY ic.index_display
                        ORDER BY pct_above DESC
                    """)
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ SECTOR BREADTH ({ma_period}MA) ═══"]
                    for r in rows:
                        pct = int(r.get("pct_above") or 0)
                        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                        lines.append(f"  {r.get('sector', '?')[:20]:20s} {bar} {pct}% ({r.get('above', 0)}/{r.get('total', 0)})")
                    return "\n".join(lines)

                elif qt == "sector_rotation":
                    cur.execute(f"""
                        WITH current AS (
                            SELECT DISTINCT ON (symbol) symbol, close FROM candles_indices ORDER BY symbol, date DESC
                        ),
                        past AS (
                            SELECT DISTINCT ON (symbol) symbol, close as past_close
                            FROM candles_indices WHERE date <= CURRENT_DATE - INTERVAL '{days} days'
                            ORDER BY symbol, date DESC
                        ),
                        prev AS (
                            SELECT DISTINCT ON (symbol) symbol, close as prev_close
                            FROM candles_indices WHERE date <= CURRENT_DATE - INTERVAL '{days * 2} days'
                            ORDER BY symbol, date DESC
                        )
                        SELECT c.symbol, 
                            ROUND(((c.close - p.past_close) / NULLIF(p.past_close, 0) * 100)::numeric, 2) as current_chg,
                            ROUND(((p.past_close - pr.prev_close) / NULLIF(pr.prev_close, 0) * 100)::numeric, 2) as prev_chg
                        FROM current c
                        JOIN past p ON c.symbol = p.symbol
                        JOIN prev pr ON c.symbol = pr.symbol
                        ORDER BY current_chg DESC
                    """)
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ SECTOR ROTATION (current {days}d vs prior {days}d) ═══"]
                    for r in rows:
                        curr = float(r.get("current_chg") or 0)
                        prev = float(r.get("prev_chg") or 0)
                        delta = curr - prev
                        trend = "↑ ACCELERATING" if delta > 2 else "↓ DECELERATING" if delta < -2 else "→ STEADY"
                        lines.append(f"  {r.get('symbol', '?')}: {curr:+.2f}% (was {prev:+.2f}%) {trend}")
                    return "\n".join(lines)
                else:
                    _cdb(conn)
                    return f"Unknown sector query: {qt}"
            except Exception as e:
                return f"Sector deep error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_cross_intelligence":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                qt = tool_input.get("query_type")

                if qt == "score_vs_outcome":
                    cur.execute("""
                        SELECT s.symbol, s.final_score, s.setup_type,
                            t.realized_pl, t.realized_pl_pct, t.is_winner
                        FROM submissions s
                        JOIN journal_trades_computed t ON LOWER(s.symbol) = LOWER(t.symbol)
                        WHERE s.traded = TRUE AND s.user_id = %s
                        ORDER BY s.final_score DESC
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    if not rows:
                        return "No scored-and-traded matches found."
                    high = [r for r in rows if float(r.get("final_score") or 0) >= 70]
                    low = [r for r in rows if float(r.get("final_score") or 0) < 70]
                    high_wr = round(sum(1 for r in high if r.get("is_winner")) / max(len(high), 1) * 100)
                    low_wr = round(sum(1 for r in low if r.get("is_winner")) / max(len(low), 1) * 100)
                    lines = [f"═══ SCORE vs OUTCOME ({len(rows)} scored+traded) ═══",
                        f"Score ≥ 70: {len(high)} trades, WR {high_wr}%, Avg P&L ₹{sum(float(r.get('realized_pl') or 0) for r in high)/max(len(high),1):+,.0f}",
                        f"Score < 70: {len(low)} trades, WR {low_wr}%, Avg P&L ₹{sum(float(r.get('realized_pl') or 0) for r in low)/max(len(low),1):+,.0f}",
                        f"\nConclusion: {'Higher scores DO predict better outcomes' if high_wr > low_wr else 'Score alone does NOT predict outcomes'}", ""]
                    for r in rows:
                        lines.append(f"  {r['symbol']}: Score {r.get('final_score')}/100 → ₹{float(r.get('realized_pl') or 0):+,.0f} ({float(r.get('realized_pl_pct') or 0):+.2f}%) {'✅' if r.get('is_winner') else '❌'}")
                    return "\n".join(lines)

                elif qt == "scored_and_traded":
                    cur.execute("""
                        SELECT s.symbol, s.final_score, s.setup_type, s.timestamp as scored_at, s.traded
                        FROM submissions s WHERE s.user_id = %s ORDER BY s.timestamp DESC
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    traded = [r for r in rows if r.get("traded")]
                    not_traded = [r for r in rows if not r.get("traded")]
                    lines = [f"═══ SCORED & TRADED ANALYSIS ═══",
                        f"Total scored: {len(rows)} | Traded: {len(traded)} | Not traded: {len(not_traded)}",
                        f"Conversion rate: {round(len(traded)/max(len(rows),1)*100)}%"]
                    if not_traded:
                        lines.append(f"\nScored but NOT traded:")
                        for r in not_traded[:10]:
                            lines.append(f"  {r['symbol']}: Score {r.get('final_score')}/100 | {r.get('setup_type', '?')} | {str(r.get('scored_at', ''))[:10]}")
                    return "\n".join(lines)

                elif qt == "regime_performance":
                    tbl = "(SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label FROM legacy_trades_fy2324 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label FROM legacy_trades_fy2425 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label FROM legacy_trades WHERE user_id = %s) combined"
                    cur.execute("SELECT regime, updated_at FROM market_regime_history ORDER BY updated_at")
                    regimes = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ REGIME PERFORMANCE ANALYSIS ═══"]
                    if regimes:
                        lines.append(f"Regime changes: {len(regimes)}")
                        for r in regimes:
                            lines.append(f"  {r.get('regime', '?').upper()} from {str(r.get('updated_at', ''))[:10]}")
                    lines.append("\n(Full regime-trade correlation requires regime dates to be mapped to trade dates — this is a directional analysis)")
                    return "\n".join(lines)

                elif qt == "repeat_trade_improvement":
                    tbl = "(SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label, '2324' as fy FROM legacy_trades_fy2324 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label, '2425' FROM legacy_trades_fy2425 UNION ALL SELECT symbol, realized_pl, realized_pl_pct, is_winner, month_label, '2526' FROM legacy_trades WHERE user_id = %s) combined"
                    cur.execute(f"""
                        SELECT symbol, COUNT(*) as times,
                            SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) as wins,
                            ROUND(SUM(realized_pl)::numeric) as total_pl,
                            ROUND(AVG(realized_pl_pct)::numeric, 2) as avg_move
                        FROM {tbl} GROUP BY symbol HAVING COUNT(*) >= 2
                        ORDER BY times DESC LIMIT 20
                    """, (g.user_id,))
                    rows = [dict(r) for r in cur.fetchall()]
                    _cdb(conn)
                    lines = [f"═══ REPEAT TRADES — Improvement Analysis ({len(rows)} stocks) ═══"]
                    for r in rows:
                        wr = round(int(r["wins"]) / max(int(r["times"]), 1) * 100)
                        lines.append(f"  {r['symbol']}: {r['times']}x traded | WR {wr}% | Total P&L ₹{float(r['total_pl'] or 0):+,.0f} | Avg {float(r['avg_move'] or 0):+.2f}%")
                    return "\n".join(lines)

                else:
                    _cdb(conn)
                    return f"Unknown cross-intelligence query: {qt}"
            except Exception as e:
                return f"Cross intelligence error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "query_fund_management":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                year = tool_input.get("year", 2026)

                # journal_fund_months has per-user rows (user_id UUID NOT NULL);
                # filter or every caller sees every other user's fund movements.
                cur.execute(
                    "SELECT month, added, withdrawn, notes FROM journal_fund_months "
                    "WHERE year = %s AND user_id = %s ORDER BY month",
                    (year, g.user_id),
                )
                rows = [dict(r) for r in cur.fetchall()]

                # FY capital history
                fy_history = [
                    {"fy": "2020-21", "start": 6000000, "end": 7031653, "added_to": 9075419},
                    {"fy": "2021-22", "start": 9075419, "end": 16187147, "added_to": 16187147},
                    {"fy": "2022-23", "start": 16187147, "end": 18028240, "added_to": 18028240},
                    {"fy": "2023-24", "start": 18028240, "end": 28000000, "added_to": 28000000},
                    {"fy": "2024-25", "start": 28000000, "end": 42600000, "added_to": 50000000},
                    {"fy": "2025-26", "start": 50000000},
                ]

                # Get current FY P&L
                cur.execute("SELECT COALESCE(SUM(realized_pl), 0) as total_pl FROM legacy_trades WHERE user_id = %s", (g.user_id,))
                current_pl = float(cur.fetchone()["total_pl"])

                total_added = sum(float(r.get("added") or 0) for r in rows)
                total_withdrawn = sum(float(r.get("withdrawn") or 0) for r in rows)

                _cdb(conn)

                lines = [f"═══ FUND MANAGEMENT — {year} ═══"]
                lines.append(f"Capital Added: ₹{total_added:,.0f} | Withdrawn: ₹{total_withdrawn:,.0f} | Net Flow: ₹{total_added - total_withdrawn:,.0f}")
                lines.append(f"\nFY History:")
                for h in fy_history:
                    ret = ((h.get("end", h["start"] + current_pl) - h["start"]) / h["start"] * 100) if h["start"] else 0
                    lines.append(f"  {h['fy']}: ₹{h['start']/10000000:.2f}Cr → ₹{h.get('end', h['start'] + current_pl)/10000000:.2f}Cr ({ret:+.1f}%)")
                lines.append(f"\nCurrent FY 25-26: ₹5.00Cr + ₹{current_pl:,.0f} P&L = ₹{(50000000 + current_pl)/10000000:.2f}Cr")

                if rows:
                    lines.append(f"\nMonthly ({year}):")
                    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                    for r in rows:
                        m = int(r.get("month", 1))
                        added = float(r.get("added") or 0)
                        withdrawn = float(r.get("withdrawn") or 0)
                        if added > 0 or withdrawn > 0:
                            lines.append(f"  {months[m-1]}: +₹{added:,.0f} / -₹{withdrawn:,.0f}")
                return "\n".join(lines)
            except Exception as e:
                return f"Fund management error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "daily_briefing":
            try:
                from database.database import get_db as _gdb, close_db as _cdb
                conn = _gdb()
                cur = conn.cursor()
                scope = tool_input.get("scope", "daily")
                lines = [f"═══ {'DAILY' if scope == 'daily' else 'WEEKLY' if scope == 'weekly' else 'MONTHLY'} BRIEFING ═══"]

                # 1. Portfolio snapshot
                cur.execute("""
                    SELECT COUNT(*) as cnt, COALESCE(SUM(position_value), 0) as deployed,
                        COALESCE(SUM((current_price - entry_price) * quantity), 0) as unr_pnl,
                        COALESCE(AVG(entry_extension_pct), 0) as avg_ext,
                        SUM(CASE WHEN current_price > entry_price THEN 1 ELSE 0 END) as in_profit
                    FROM positions WHERE status = 'active' AND user_id = %s
                """, (g.user_id,))
                pf = dict(cur.fetchone() or {})
                cnt = int(pf.get("cnt") or 0)
                deployed = float(pf.get("deployed") or 0)
                unr = float(pf.get("unr_pnl") or 0)
                lines.append(f"\n📊 PORTFOLIO: {cnt} positions | ₹{deployed/10000000:.2f}Cr deployed ({deployed/50000000*100:.0f}%) | Unrealized ₹{unr:+,.0f}")
                if cnt > 0:
                    lines.append(f"   {pf.get('in_profit', 0)} in profit, {cnt - int(pf.get('in_profit') or 0)} in loss | Avg Extension {float(pf.get('avg_ext') or 0):.1f}%")

                # 2. Positions needing attention (near SL or overdue first sell)
                cur.execute("""
                    SELECT stock_name,
                        ROUND(((current_price - stop_loss) / NULLIF(current_price, 0) * 100)::numeric, 1) as pct_to_sl,
                        entry_extension_pct, first_sell_done, first_sell_zone
                    FROM positions WHERE status = 'active' AND current_price > 0 AND user_id = %s
                    ORDER BY (current_price - stop_loss) / NULLIF(current_price, 0) ASC LIMIT 3
                """, (g.user_id,))
                danger = [dict(r) for r in cur.fetchall()]
                if danger:
                    lines.append(f"\n⚠️ ATTENTION:")
                    for d in danger:
                        pct = float(d.get("pct_to_sl") or 0)
                        ext = float(d.get("entry_extension_pct") or 0)
                        fsz = float(d.get("first_sell_zone") or 35)
                        alerts = []
                        if pct < 3: alerts.append(f"only {pct}% from SL!")
                        if ext > fsz and not d.get("first_sell_done"): alerts.append(f"first sell OVERDUE (ext {ext:.0f}%)")
                        if alerts:
                            lines.append(f"   {d['stock_name']}: {' | '.join(alerts)}")

                # 3. Market regime
                cur.execute("SELECT regime, updated_at FROM market_regime_history ORDER BY updated_at DESC LIMIT 1")
                regime = cur.fetchone()
                if regime:
                    lines.append(f"\n🌍 REGIME: {regime['regime'].upper()} (since {str(regime['updated_at'])[:10]})")

                # 4. Recent trade stats
                cur.execute("""
                    SELECT COUNT(*) as recent, SUM(CASE WHEN is_winner THEN 1 ELSE 0 END) as wins,
                        ROUND(SUM(realized_pl)::numeric) as pl
                    FROM legacy_trades WHERE (month_label LIKE '%2026%' OR month_label LIKE '%2025%') AND user_id = %s
                """, (g.user_id,))
                recent = dict(cur.fetchone() or {})
                if int(recent.get("recent") or 0) > 0:
                    wr = round(int(recent["wins"]) / max(int(recent["recent"]), 1) * 100)
                    lines.append(f"\n📈 RECENT TRADES: {recent['recent']} | WR {wr}% | P&L ₹{float(recent.get('pl') or 0):+,.0f}")

                # 5. Watchlist movers (from multi-watchlist system)
                cur.execute("""
                    SELECT wi.symbol, c.close, c.volume,
                        CASE WHEN prev.close > 0 THEN ROUND(((c.close - prev.close) / prev.close * 100)::numeric, 2) ELSE 0 END as pchange
                    FROM watchlist_items wi
                    JOIN watchlists wl ON wi.watchlist_id = wl.id AND wl.user_id = %s
                    JOIN candles_daily c ON wi.security_id = c.security_id
                    LEFT JOIN LATERAL (SELECT close FROM candles_daily WHERE security_id = wi.security_id AND date < CURRENT_DATE ORDER BY date DESC LIMIT 1) prev ON true
                    WHERE c.date = (SELECT MAX(date) FROM candles_daily WHERE security_id = wi.security_id)
                    AND wi.security_id IS NOT NULL
                    ORDER BY ABS(CASE WHEN prev.close > 0 THEN ((c.close - prev.close) / prev.close * 100) ELSE 0 END) DESC
                    LIMIT 5
                """, (g.user_id,))
                wl_rows = [dict(r) for r in cur.fetchall()]
                if wl_rows:
                    big_movers = [w for w in wl_rows if abs(float(w.get("pchange") or 0)) > 2]
                    if big_movers:
                        lines.append(f"\n👁️ WATCHLIST MOVERS:")
                        for w in big_movers:
                            lines.append(f"   {w['symbol']}: {float(w['pchange']):+.2f}% (₹{float(w['close']):,.2f})")

                _cdb(conn)
                return "\n".join(lines)
            except Exception as e:
                return f"Briefing error: {str(e)}"
            finally:
                try:
                    if conn:
                        _cdb(conn)
                except:
                    pass

        elif tool_name == "run_custom_scan":
            # Call our own custom scanner endpoint internally
            import requests as req_lib
            try:
                body = {}
                for k in ["max_pct_from_high", "min_pct_from_low", "above_ma5", "above_ma10",
                           "above_ma20", "above_ma50", "above_ma200", "min_liquidity", "min_mcap",
                           "max_mcap", "rs_1w_positive", "rs_3m_positive", "rs_6m_positive",
                           "leading_sectors_only", "sort_by", "limit"]:
                    if k in tool_input and tool_input[k] is not None:
                        body[k] = tool_input[k]
                if "limit" not in body:
                    body["limit"] = 20

                # Direct internal call using the same DB
                conn = _get_db()
                if not conn:
                    return "Database unavailable"
                cur = conn.cursor()
                cur.execute("""
                    WITH arrayed AS (
                        SELECT cd.security_id,
                            ARRAY_AGG(cd.close ORDER BY cd.date DESC) as cl,
                            ARRAY_AGG(cd.high ORDER BY cd.date DESC) as hi,
                            ARRAY_AGG(cd.low ORDER BY cd.date DESC) as lo,
                            ARRAY_AGG(cd.volume * cd.close ORDER BY cd.date DESC) as turnover,
                            MAX(cd.date) as last_date
                        FROM candles_daily cd
                        WHERE cd.date >= CURRENT_DATE - 400
                        GROUP BY cd.security_id
                        HAVING array_length(ARRAY_AGG(cd.close ORDER BY cd.date DESC), 1) >= 20
                    ),
                    sc100 AS (
                        SELECT ARRAY_AGG(close ORDER BY date DESC) as sc_cl
                        FROM candles_indices WHERE symbol = 'NIFTY SMALLCAP 100' AND date >= CURRENT_DATE - 400
                    )
                    SELECT a.security_id, su.symbol, su.company_name as stock_name, a.cl[1] as cmp,
                        (SELECT MAX(v) FROM UNNEST(a.hi[1:252]) v) as high_52w,
                        (SELECT MIN(v) FROM UNNEST(a.lo[1:252]) v) as low_52w,
                        ROUND(((a.cl[1] - (SELECT MAX(v) FROM UNNEST(a.hi[1:252]) v)) / NULLIF((SELECT MAX(v) FROM UNNEST(a.hi[1:252]) v), 0) * 100)::numeric, 1) as pct_from_high,
                        ROUND(((a.cl[1] - (SELECT MIN(v) FROM UNNEST(a.lo[1:252]) v)) / NULLIF((SELECT MIN(v) FROM UNNEST(a.lo[1:252]) v), 0) * 100)::numeric, 1) as pct_from_low,
                        a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:5]) v), 0) as above_ma5,
                        a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:10]) v), 0) as above_ma10,
                        a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:20]) v), 0) as above_ma20,
                        a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:50]) v), 0) as above_ma50,
                        a.cl[1] > COALESCE((SELECT AVG(v) FROM UNNEST(a.cl[1:200]) v), 0) as above_ma200,
                        ROUND(((SELECT AVG(v) FROM UNNEST(a.turnover[1:20]) v) / 10000000.0)::numeric, 1) as liq_cr,
                        CASE WHEN a.cl[6] > 0 AND sc.sc_cl[6] > 0 THEN
                            ROUND((((a.cl[1] / a.cl[6] - 1) - (sc.sc_cl[1] / sc.sc_cl[6] - 1)) * 100)::numeric, 1)
                        END as rs_1w,
                        CASE WHEN a.cl[64] > 0 AND sc.sc_cl[64] > 0 THEN
                            ROUND((((a.cl[1] / a.cl[64] - 1) - (sc.sc_cl[1] / sc.sc_cl[64] - 1)) * 100)::numeric, 1)
                        END as rs_3m,
                        CASE WHEN a.cl[127] > 0 AND sc.sc_cl[127] > 0 THEN
                            ROUND((((a.cl[1] / a.cl[127] - 1) - (sc.sc_cl[1] / sc.sc_cl[127] - 1)) * 100)::numeric, 1)
                        END as rs_6m
                    FROM arrayed a
                    JOIN stock_universe su ON a.security_id = su.security_id
                    CROSS JOIN sc100 sc
                    WHERE (SELECT AVG(v) FROM UNNEST(a.turnover[1:20]) v) / 10000000.0 > 0.01
                """)
                all_stocks = [dict(r) for r in cur.fetchall()]

                # Apply filters in Python (same logic as screener endpoint)
                results = all_stocks
                if body.get("max_pct_from_high") is not None:
                    mp = float(body["max_pct_from_high"])
                    results = [s for s in results if s["pct_from_high"] is not None and float(s["pct_from_high"]) >= -abs(mp)]
                if body.get("min_pct_from_low") is not None:
                    ml = float(body["min_pct_from_low"])
                    results = [s for s in results if s["pct_from_low"] is not None and float(s["pct_from_low"]) >= ml]
                for ma in ["ma5", "ma10", "ma20", "ma50", "ma200"]:
                    if body.get(f"above_{ma}"):
                        results = [s for s in results if s.get(f"above_{ma}")]
                if body.get("min_liquidity"):
                    results = [s for s in results if float(s.get("liq_cr") or 0) >= float(body["min_liquidity"])]
                if body.get("rs_1w_positive"):
                    results = [s for s in results if (float(s.get("rs_1w") or 0)) > 0]
                if body.get("rs_3m_positive"):
                    results = [s for s in results if (float(s.get("rs_3m") or 0)) > 0]
                if body.get("rs_6m_positive"):
                    results = [s for s in results if (float(s.get("rs_6m") or 0)) > 0]

                sort_key = body.get("sort_by", "liq_cr")
                results.sort(key=lambda s: float(s.get(sort_key) or 0), reverse=True)
                limit = min(int(body.get("limit", 20)), 50)
                results = results[:limit]

                # Format response
                lines = [f"CUSTOM SCAN: {len(results)} stocks matched (from {len(all_stocks)} scanned)"]
                lines.append(f"Filters: {', '.join(f'{k}={v}' for k,v in body.items() if k != 'limit')}")
                lines.append("")
                for i, s in enumerate(results):
                    mas = "/".join(str(m) for m in [5,10,20,50,200] if s.get(f"above_ma{m}"))
                    lines.append(f"{i+1}. {s['symbol']} — ₹{float(s['cmp']):.1f} | 52W: {s['pct_from_high']}% from high, +{s['pct_from_low']}% from low | MAs: {mas or '—'} | Liq: ₹{s['liq_cr']}Cr | RS 1W:{s.get('rs_1w','—')} 3M:{s.get('rs_3m','—')} 6M:{s.get('rs_6m','—')}")
                return "\n".join(lines)
            except Exception as e:
                return f"Custom scan error: {str(e)}"
            finally:
                if conn:
                    try:
                        conn.close()
                    except:
                        pass

        elif tool_name == "list_saved_scanners":
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                cur.execute("SELECT id, name, description, pinned, last_run_count, last_run_at, filters FROM saved_scanners WHERE user_id = %s ORDER BY pinned DESC, created_at DESC", (g.user_id,))
                rows = cur.fetchall()
                if not rows:
                    return "No saved scanners found. User can create them in the Custom Scanner section."
                lines = [f"SAVED SCANNERS ({len(rows)} total):"]
                for r in rows:
                    r = dict(r)
                    pin = " 📌" if r.get("pinned") else ""
                    count = f" | Last: {r['last_run_count']} matches" if r.get("last_run_count") else ""
                    lines.append(f"• {r['name']}{pin} — {r.get('description', '')}{count}")
                return "\n".join(lines)
            except Exception as e:
                return f"Error: {str(e)}"
            finally:
                if conn:
                    try:
                        conn.close()
                    except:
                        pass

        elif tool_name == "query_watchlist":
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                wl_filter = tool_input.get("watchlist_name", "").strip()
                # Get all watchlists with items
                if wl_filter:
                    cur.execute("SELECT id, name, pin_slot, color FROM watchlists WHERE user_id = %s AND LOWER(name) LIKE %s ORDER BY pin_slot", (g.user_id, f"%{wl_filter.lower()}%"))
                else:
                    cur.execute("SELECT id, name, pin_slot, color FROM watchlists WHERE user_id = %s ORDER BY pin_slot NULLS LAST", (g.user_id,))
                wls = [dict(r) for r in cur.fetchall()]
                if not wls:
                    return "No watchlists found."
                wl_ids = [w["id"] for w in wls]
                cur.execute("""
                    SELECT wi.watchlist_id, wi.symbol, wi.company_name, wi.security_id, wi.section_name,
                        su.sector, su.industry
                    FROM watchlist_items wi
                    LEFT JOIN stock_universe su ON wi.security_id = su.security_id
                    WHERE wi.watchlist_id = ANY(%s)
                    ORDER BY wi.watchlist_id, wi.sort_order, wi.added_at DESC
                """, (wl_ids,))
                items = [dict(r) for r in cur.fetchall()]
                # Get live prices
                sids = list(set(i["security_id"] for i in items if i.get("security_id")))
                prices = {}
                if sids:
                    cur.execute("""
                        SELECT DISTINCT ON (security_id) security_id, close, date
                        FROM candles_daily WHERE security_id = ANY(%s) AND volume > 0
                        ORDER BY security_id, date DESC
                    """, (sids,))
                    for r in cur.fetchall():
                        prices[r["security_id"]] = float(r["close"])
                conn.close()
                # Build output
                lines = []
                for wl in wls:
                    wl_items = [i for i in items if i["watchlist_id"] == wl["id"]]
                    lines.append(f"\n═══ {wl['name']} ({len(wl_items)} stocks) ═══")
                    sectors = {}
                    for it in wl_items:
                        sec = it.get("sector") or "Unknown"
                        sectors[sec] = sectors.get(sec, 0) + 1
                        price = prices.get(it["security_id"], 0)
                        sec_tag = f" [{sec}]" if sec != "Unknown" else ""
                        section = f" ({it['section_name']})" if it.get("section_name") else ""
                        lines.append(f"  {it['symbol']}: ₹{price:,.2f}{sec_tag}{section}")
                    if sectors:
                        lines.append(f"  Sectors: {', '.join(f'{k}({v})' for k,v in sorted(sectors.items(), key=lambda x:-x[1]))}")
                return "\n".join(lines)
            except Exception as e:
                return f"Watchlist query error: {str(e)}"
            finally:
                try:
                    if conn:
                        conn.close()
                except:
                    pass

        elif tool_name == "get_drawdown_analysis":
            conn = _get_db()
            if not conn:
                return "Database unavailable"
            try:
                cur = conn.cursor()
                # Reuse the same logic as the drawdown-deep endpoint
                fy_config = [
                    {"fy": "2020-21", "start_capital": 6000000, "monthly_table": "legacy_monthly_fy2021", "trades_table": "legacy_trades_fy2021"},
                    {"fy": "2021-22", "start_capital": 9075419, "monthly_table": "legacy_monthly_fy2122", "trades_table": "legacy_trades_fy2122"},
                    {"fy": "2022-23", "start_capital": 16187147, "monthly_table": "legacy_monthly_fy2223", "trades_table": "legacy_trades_fy2223"},
                    {"fy": "2023-24", "start_capital": 18028240, "monthly_table": "legacy_monthly_fy2324", "trades_table": "legacy_trades_fy2324"},
                    {"fy": "2024-25", "start_capital": 28000000, "monthly_table": "legacy_monthly_fy2425", "trades_table": "legacy_trades_fy2425"},
                    {"fy": "2025-26", "start_capital": 50000000, "monthly_table": "legacy_monthly_summary", "trades_table": "legacy_trades"},
                ]
                month_dates = ["04-30","05-31","06-30","07-31","08-31","09-30","10-31","11-30","12-31","01-31","02-28","03-31"]
                timeline = []
                for fc in fy_config:
                    base = float(fc["start_capital"])
                    start_year = int(fc["fy"].split("-")[0])
                    cur.execute(f"SELECT month_label, month_order, after_charges, net_pf_impact FROM {fc['monthly_table']} ORDER BY month_order")
                    running = base
                    for m in cur.fetchall():
                        pct = float(m["after_charges"] or 0)
                        month_pl = round((pct / 100) * running)
                        running += month_pl
                        timeline.append({"month_label": m["month_label"], "fy": fc["fy"], "pf_value": round(running), "month_pct": round(pct, 2)})
                # Compute DD from peak (reset peak at FY boundaries)
                peak = 0
                prev_fy = None
                neg_months = []
                max_dd = 0
                max_dd_month = ""
                for m in timeline:
                    if m["fy"] != prev_fy:
                        peak = m["pf_value"]
                        prev_fy = m["fy"]
                    if m["pf_value"] > peak: peak = m["pf_value"]
                    dd_pct = round(((m["pf_value"] - peak) / peak) * 100, 2) if peak > 0 else 0
                    m["dd_pct"] = dd_pct
                    if dd_pct < max_dd:
                        max_dd = dd_pct
                        max_dd_month = m["month_label"]
                    if m["month_pct"] < 0:
                        neg_months.append(m)

                # Get SC100 monthly for context
                cur.execute("""
                    WITH mc AS (
                        SELECT date_trunc('month', date) as month, (ARRAY_AGG(close ORDER BY date DESC))[1] as cl
                        FROM candles_indices WHERE symbol = 'NIFTY SMALLCAP 100' AND date >= '2023-04-01'
                        GROUP BY date_trunc('month', date)
                    )
                    SELECT TO_CHAR(month, 'Mon YYYY') as lbl,
                        ROUND(((cl / LAG(cl) OVER (ORDER BY month)) - 1)::numeric * 100, 1) as pct
                    FROM mc ORDER BY month
                """)
                sc100 = {r["lbl"]: float(r["pct"]) for r in cur.fetchall() if r["pct"] is not None}

                # Build response
                lines = ["DRAWDOWN ANALYSIS — All FYs"]
                lines.append(f"Max Drawdown: {max_dd}% ({max_dd_month})")
                lines.append(f"Negative months: {len(neg_months)} out of {len(timeline)}")
                lines.append(f"Monthly win rate: {round((len(timeline) - len(neg_months)) / len(timeline) * 100, 1)}%")
                lines.append("")
                lines.append("NEGATIVE MONTHS (with market context):")
                for m in neg_months:
                    sc = sc100.get(m["month_label"])
                    mkt = f" | SC100: {sc:+.1f}%" if sc is not None else ""
                    rel = ""
                    if sc is not None:
                        gap = m["month_pct"] - sc
                        rel = f" | {'Outperformed' if gap > 0 else 'Underperformed'}: {gap:+.1f}%"
                    lines.append(f"  {m['month_label']} ({m['fy']}): {m['month_pct']:+.2f}% | DD: {m['dd_pct']}% | PF: ₹{m['pf_value']/10000000:.2f}Cr{mkt}{rel}")
                return "\n".join(lines)
            except Exception as e:
                return f"Drawdown analysis error: {str(e)}"
            finally:
                if conn:
                    try:
                        conn.close()
                    except:
                        pass

        return f"Unknown tool: {tool_name}"

    except Exception as e:
        return f"Tool error: {str(e)}"


# ═══════════════════════════════════════════════════════════════════
# MAIN CHAT ENDPOINT
# ═══════════════════════════════════════════════════════════════════

@chat_bp.route("/api/chat", methods=["POST"])
@limiter.limit("20 per minute")
def chat():
    data = request.json
    message = data.get("message", "").strip()
    page_context = data.get("page_context")
    stock_context = data.get("stock_context")
    response_mode = data.get("response_mode", "auto")
    is_voice = data.get("voice", False)
    model_pref = data.get("model", "sonnet")  # sonnet (default), haiku (fast), opus (deep)

    if not message:
        return jsonify({"error": "Message required"}), 400

    category = "general"
    matched_stock = None
    structured_data = None

    try:
        # ─── DETAILED MODE: existing full-text behavior ───
        if response_mode == "detailed":
            import anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                return jsonify({"error": "ANTHROPIC_API_KEY not set on server"}), 500

            live_ctx = _build_full_context()
            system = DETAILED_SYSTEM + live_ctx
            history = _get_chat_history(limit=16)
            messages = history + [{"role": "user", "content": message}]

            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=system,
                messages=messages,
            )
            ai_response = response.content[0].text
            _save_message("user", message, page_context, stock_context)
            _save_message("assistant", ai_response, page_context, stock_context)

            return jsonify({
                "response": ai_response,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "format": "detailed",
            })

        # ─── QUICK / AUTO MODE: structured cards + short insight ───

        # Step 1: Get active stock names for classifier
        conn = _get_db()
        stock_names = []

        if conn:
            try:
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT stock_name FROM positions WHERE status = 'active' AND user_id = %s", (g.user_id,))
                    stock_names = [r["stock_name"] for r in cur.fetchall()]
                except:
                    pass

                # Step 2: Classify
                category, matched_stock = _classify_question(message, stock_names)
                print(f"📋 Classified as: {category} (stock: {matched_stock})")

                # Step 3: Build structured data
                try:
                    cur2 = conn.cursor()
                    if category == "sell_decision":
                        structured_data = _build_sell_decision(cur2)
                    elif category == "portfolio_overview":
                        structured_data = _build_portfolio_overview(cur2)
                    elif category == "risk_check":
                        structured_data = _build_risk_check(cur2)
                    elif category == "rankings":
                        structured_data = _build_rankings(cur2, message)
                    elif category == "trailing_status":
                        structured_data = _build_trailing_status(cur2)
                    elif category == "single_stock" and matched_stock:
                        structured_data = _build_single_stock(cur2, matched_stock)
                    elif category == "action_add":
                        structured_data = _build_action_add(message)
                    elif category == "action_reconcile":
                        structured_data = _build_action_reconcile()
                    elif category == "greeting":
                        structured_data = None  # No cards for greetings
                    elif category == "journal_stats":
                        # Build journal trade stats context
                        try:
                            cur2.execute("SELECT COUNT(*) as cnt FROM journal_trades_computed WHERE user_id = %s", (g.user_id,))
                            tcount = cur2.fetchone()["cnt"]
                            cur2.execute("SELECT COUNT(*) as cnt FROM journal_trades_computed WHERE position_status = 'Closed' AND user_id = %s", (g.user_id,))
                            ccount = cur2.fetchone()["cnt"]
                            structured_data = {
                                "type": "journal_stats",
                                "has_analytics": True,
                                "total_trades": tcount,
                                "closed_trades": ccount,
                            }
                            cur2.execute("SELECT data FROM nexus_analytics WHERE id = 1")
                            arow = cur2.fetchone()
                            if arow:
                                import json as _j
                                a = arow["data"] if isinstance(arow["data"], dict) else _j.loads(arow["data"])
                                structured_data["win_rate"] = a.get("win_rate", 0)
                                structured_data["total_pnl"] = a.get("total_pnl", 0)
                        except:
                            structured_data = {"type": "journal_stats", "has_analytics": False, "total_trades": 0, "closed_trades": 0}
                    elif category == "screener_query":
                        # Skip structured data — let the AI use query_screener tool
                        structured_data = {"type": "screener_query", "message": "Use query_screener tool to answer this."}
                    elif category == "greeting":
                        structured_data = None  # No portfolio dump for greetings
                    else:
                        # For truly unrecognized categories, don't force portfolio stats
                        # The AI has tools — let it fetch what it needs
                        structured_data = None
                except Exception as e:
                    print(f"⚠️ Structured data build error: {e}")
                    import traceback
                    traceback.print_exc()
            finally:
                try:
                    conn.close()
                except:
                    pass

        lightweight = None
        if should_use_lightweight_reply(category, structured_data):
            lightweight = build_lightweight_reply(
                category,
                structured_data,
                message=message,
                is_voice=is_voice,
            )

        if lightweight:
            ai_response = lightweight["response"]
            _save_message("user", message, page_context, stock_context)
            _save_message("assistant", ai_response, page_context, stock_context)

            resp = {
                "response": ai_response,
                "format": "quick",
                "category": category,
            }
            if structured_data:
                resp["structured_response"] = structured_data
            if lightweight.get("follow_ups"):
                resp["follow_ups"] = lightweight["follow_ups"]
            return jsonify(resp)

        # Step 4: Get Claude's insight (short) — WITH TOOLS
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return jsonify({"error": "ANTHROPIC_API_KEY not set on server"}), 500

        slim_ctx = _build_slim_context(structured_data) if structured_data else "\nNo data available.\n"
        cat_prompt = CATEGORY_PROMPTS.get(category, CATEGORY_PROMPTS["general"])

        tool_system = QUICK_SYSTEM + slim_ctx
        if is_voice:
            tool_system += """

VOICE MODE — STRICT RULES (override everything else):

1. MAX 2 SENTENCES. Never exceed 30 words for simple questions.
2. NO metaphors, NO analogies, NO Hollywood references, NO emojis, NO "---"
3. NO markdown formatting — no **, no ##, no bullet points
4. Numbers are sacred: "5 positions, P&L plus 3.2 lakhs, best R is 4.1"
5. Empty portfolio = "Portfolio is empty. Zero positions, 5 crore available."
6. Portfolio summary = "5 positions. Total P&L plus 3.2 lakhs. Best: VEDL at 4.1R. One 5MA alert on MTAR."
7. Tool confirmations = "Done. Stop loss updated to 650." (max 8 words)
8. Stock price = "VEDL is at 483, up 2.1 percent today."
9. NEVER explain what you're doing. Just do it and report the result.
10. NEVER say "Let me pull" or "Let me check" — just give the answer directly.
"""
        user_msg = f"{cat_prompt}\n\nUser asked: \"{message}\""

        history = _get_chat_history(limit=6)
        api_messages = history + [{"role": "user", "content": user_msg}]

        client = anthropic.Anthropic(api_key=api_key)

        # Model selection: user can choose via frontend toggle
        MODEL_MAP = {
            "haiku": "claude-haiku-4-5-20251001",
            "sonnet": "claude-sonnet-4-6",
            "opus": "claude-opus-4-6",
        }
        voice_model = MODEL_MAP.get(model_pref, "claude-sonnet-4-6")
        voice_tokens = 150 if (is_voice and model_pref != "opus") else 1000
        response = client.messages.create(
            model=voice_model,
            max_tokens=voice_tokens,
            system=tool_system,
            messages=api_messages,
            tools=AI_TOOLS,
        )

        # Handle tool use (max 3 rounds to prevent infinite loops)
        tool_results = []
        ai_response = ""
        rounds = 0

        while response.stop_reason == "tool_use" and rounds < 3:
            rounds += 1
            # Extract all tool uses and text from response
            tool_uses = []
            for block in response.content:
                if block.type == "text":
                    ai_response += block.text
                elif block.type == "tool_use":
                    tool_uses.append(block)

            if not tool_uses:
                break

            # Execute each tool and collect results
            tool_result_blocks = []
            for tu in tool_uses:
                result = _execute_tool(tu.name, tu.input)
                # Structured results: {"text": "...", "stocks": [...]} — split for Claude vs frontend
                if isinstance(result, dict) and "text" in result:
                    claude_text = result["text"]
                    tool_results.append({"tool": tu.name, "input": tu.input, "result": claude_text, "data": result})
                else:
                    claude_text = result
                    tool_results.append({"tool": tu.name, "input": tu.input, "result": result})
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": claude_text,
                })

            # Send results back to Claude for final response
            api_messages = api_messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": tool_result_blocks},
            ]

            response = client.messages.create(
                model=voice_model,
                max_tokens=300 if (is_voice and model_pref != "opus") else 800,
                system=tool_system,
                messages=api_messages,
                tools=AI_TOOLS,
            )

        # Extract final text response
        for block in response.content:
            if hasattr(block, "text"):
                ai_response += block.text
        _save_message("user", message, page_context, stock_context)
        # Save assistant response with context hint so follow-up questions work
        context_hint = ""
        if structured_data and structured_data.get("type") != "empty":
            dtype = structured_data["type"]
            if dtype == "actions":
                names = [c["name"] for c in structured_data.get("cards", [])[:5]]
                context_hint = f"\n[Cards shown: sell decisions for {', '.join(names)}]"
            elif dtype == "positions":
                names = [c["name"] for c in structured_data.get("cards", [])[:5]]
                context_hint = f"\n[Cards shown: portfolio overview — {', '.join(names)}]"
            elif dtype == "rankings":
                names = [c["name"] for c in structured_data.get("cards", [])[:5]]
                context_hint = f"\n[Cards shown: rankings — {', '.join(names)}]"
            elif dtype == "trailing":
                context_hint = f"\n[Cards shown: trailing/5MA status for {structured_data.get('total', 0)} positions]"
            elif dtype == "risk":
                context_hint = f"\n[Cards shown: risk assessment]"
            elif dtype == "single_stock":
                context_hint = f"\n[Cards shown: deep dive on {structured_data.get('card', {}).get('name', '?')}]"
        _save_message("assistant", ai_response + context_hint, page_context, stock_context)

        resp = {
            "response": ai_response,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "format": "quick",
            "category": category,
        }

        if structured_data:
            resp["structured_response"] = structured_data

        if tool_results:
            resp["tool_results"] = tool_results

        # Extract follow-up suggestions (lines starting with →)
        import re
        follow_ups = re.findall(r'→\s*(.+?)(?:\n|$)', ai_response)
        if follow_ups:
            resp["follow_ups"] = [f.strip() for f in follow_ups[:4]]
            # Clean the response text — remove the → lines for cleaner display
            clean_response = re.sub(r'\n*→\s*.+?(?:\n|$)', '', ai_response).strip()
            resp["response"] = clean_response

        return jsonify(resp)

    except Exception as e:
        print(f"❌ Chat error: {e}")
        import traceback
        tb = traceback.format_exc()
        print(tb)
        # Return detailed error for debugging
        error_detail = str(e)
        if "authentication" in error_detail.lower() or "api_key" in error_detail.lower():
            error_detail = "API key issue: " + error_detail
        elif "model" in error_detail.lower():
            error_detail = "Model issue: " + error_detail
        elif "tool" in error_detail.lower():
            error_detail = "Tool definition issue: " + error_detail
        return jsonify({"error": error_detail, "traceback": tb[:500]}), 500


@chat_bp.route("/api/chat/health", methods=["GET"])
@limiter.limit("60 per minute")
def chat_health():
    """Diagnostic endpoint to test AI connectivity"""
    checks = {}
    
    # Check API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    checks["api_key"] = "set" if api_key else "MISSING"
    checks["api_key_prefix"] = api_key[:12] + "..." if api_key else "N/A"
    
    # Check anthropic SDK version
    try:
        import anthropic
        checks["sdk_version"] = getattr(anthropic, "__version__", "unknown")
    except Exception as e:
        checks["sdk_version"] = f"IMPORT ERROR: {e}"
    
    # Check AI_TOOLS count
    checks["tools_count"] = len(AI_TOOLS)
    
    # Check DB
    conn = _get_db()
    checks["db"] = "connected" if conn else "FAILED"
    if conn:
        try: conn.close()
        except: pass
    
    # Try a minimal Claude call
    if api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=10,
                messages=[{"role": "user", "content": "Say OK"}],
            )
            checks["claude_api"] = "OK"
            checks["claude_response"] = resp.content[0].text if resp.content else "empty"
        except Exception as e:
            checks["claude_api"] = f"FAILED: {str(e)[:200]}"
    
    return jsonify(checks)
def chat_history():
    limit = request.args.get("limit", 50, type=int)
    conn = _get_db()
    if not conn:
        return jsonify([])
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, role, content, page_context, stock_context, created_at FROM chat_messages WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (g.user_id, limit,),
        )
        rows = cur.fetchall()
        rows.reverse()
        for r in rows:
            r["created_at"] = str(r["created_at"])
        return jsonify(rows)
    except:
        return jsonify([])
    finally:
        try:
            conn.close()
        except:
            pass


@chat_bp.route("/api/chat/clear", methods=["DELETE"])
@limiter.limit("15 per minute")
def clear_chat():
    conn = _get_db()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 500
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM chat_messages WHERE user_id = %s", (g.user_id,))
        conn.commit()
        return jsonify({"cleared": True})
    except Exception as e:
        print(f"[chat] error: {e}")
        return jsonify({"error": "Internal error"}), 500
    finally:
        try:
            conn.close()
        except:
            pass
