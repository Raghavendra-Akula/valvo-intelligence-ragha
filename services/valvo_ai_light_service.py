"""
Compatibility layer kept for the legacy chat route.

The live frontend is now mounted to Valvo AI v2, but the legacy backend
imports this helper at module load time. Keeping this file prevents the
old route tree from breaking while it remains disconnected from the UI.
"""

DETERMINISTIC_TYPES = {
    "empty",
    "actions",
    "positions",
    "risk",
    "rankings",
    "trailing",
    "single_stock",
    "action",
}


def should_use_lightweight_reply(category, structured_data=None):
    if category == "greeting":
        return True
    if not structured_data:
        return False
    return structured_data.get("type") in DETERMINISTIC_TYPES


def build_lightweight_reply(category, structured_data=None, message="", is_voice=False):
    if category == "greeting":
        return {
            "response": "Hi. Valvo AI is ready." if is_voice else "Hi. Valvo AI is ready. Ask me about sells, risk, trailing, or your portfolio.",
            "follow_ups": [] if is_voice else [
                "What should I sell today?",
                "Show my portfolio overview",
                "Run a risk check",
            ],
        }

    if not structured_data:
        return None

    dtype = structured_data.get("type")
    if dtype == "empty":
        return {"response": structured_data.get("message") or "No active positions."}

    if dtype == "positions":
        summary = structured_data.get("summary") or {}
        cards = structured_data.get("cards") or []
        if not cards:
            return {"response": "No active positions."}
        best = max(cards, key=lambda c: c.get("r", 0))
        weakest = min(cards, key=lambda c: c.get("r", 0))
        return {
            "response": (
                f"{summary.get('count', len(cards))} positions. Total P&L {_money(summary.get('total_pnl'))}. "
                f"Best is {best['name']} at {best['r']:.1f}R. Weakest is {weakest['name']} at {weakest['r']:.1f}R."
            ),
            "follow_ups": [] if is_voice else ["What should I sell today?", "Show 5MA alerts", "Run a risk check"],
        }

    if dtype == "actions":
        cards = structured_data.get("cards") or []
        top = cards[0] if cards else None
        if not top:
            return {"response": "No sell actions are active right now."}
        return {
            "response": f"Top action is {top['name']}: {top['verdict']} at {top['metric']}.",
            "follow_ups": [] if is_voice else ["Show full 5MA status", "Rank positions by urgency", "Run a risk check"],
        }

    if dtype == "risk":
        cards = structured_data.get("cards") or []
        top = cards[0] if cards else None
        if not top:
            return {"response": f"Total risk {_money(structured_data.get('total_risk'))}."}
        return {
            "response": f"Total risk {_money(structured_data.get('total_risk'))}. Highest defined risk is {top['name']} at {_money(top['risk_rupees'])}.",
            "follow_ups": [] if is_voice else ["What should I trim first?", "Show 5MA alerts", "Rank positions by risk"],
        }

    if dtype == "rankings":
        cards = structured_data.get("cards") or []
        top = cards[:3]
        return {
            "response": " | ".join(f"#{c['rank']} {c['name']} {c['r']:.1f}R" for c in top) if top else "No ranked positions available.",
            "follow_ups": [] if is_voice else ["What should I sell today?", "Show risk check", "Show trailing status"],
        }

    if dtype == "trailing":
        alerts = int(structured_data.get("alerts") or 0)
        total = int(structured_data.get("total") or 0)
        return {
            "response": f"{alerts} of {total} positions need attention." if total else "No trailing data available.",
            "follow_ups": [] if is_voice else ["What should I sell today?", "Run a portfolio overview", "Show the highest-risk position"],
        }

    if dtype == "single_stock":
        card = structured_data.get("card") or {}
        return {
            "response": f"{card.get('name')} is at {card.get('cmp')} versus entry {card.get('entry')}, with {card.get('r', 0):.1f}R.",
            "follow_ups": [] if is_voice else ["What should I do with this stock?", "Show portfolio overview", "Run risk check"],
        }

    if dtype == "action":
        return {"response": structured_data.get("message") or "Action prepared.", "follow_ups": []}

    return None


def _money(amount):
    amount = float(amount or 0)
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 10000000:
        return f"{sign}Rs {amount / 10000000:.2f}Cr"
    if amount >= 100000:
        return f"{sign}Rs {amount / 100000:.2f}L"
    if amount >= 1000:
        return f"{sign}Rs {amount / 1000:.1f}K"
    return f"{sign}Rs {amount:.0f}"
