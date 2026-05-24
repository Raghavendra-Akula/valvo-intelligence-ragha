"""
Position Management AI Service
Analyzes chart images with entry price + quantity
Returns trailing stop, sell-into-strength levels, exit plan
Supports: Gemini (primary) and Claude (secondary)
"""
import os
import json
import base64
import PIL.Image
from dotenv import load_dotenv

load_dotenv()

# ─── Shared prompt builder ───

SYSTEM_PROMPT = """You are an expert Indian stock market swing/positional trader specializing in momentum breakout strategies on NSE-listed stocks.

You analyze TradingView charts and provide actionable position management advice.

Your trading philosophy:
- Trail winners using 5 EMA (aggressive/fast stocks) or 10 EMA (steady movers) or 20 EMA (slow grinders)
- Sell 50% into strength at key extension levels, trail the rest
- Never let a winning trade turn into a loss
- Respect moving average supports — a close below trailing EMA = exit signal
- Consider ADR (Average Daily Range) for realistic daily move expectations"""


def _build_analysis_prompt(entry_price, quantity):
    return f"""Analyze this stock chart image. The trader has an ACTIVE POSITION:

POSITION DETAILS:
- Entry Price: ₹{entry_price}
- Quantity: {quantity} shares
- Position Value: ₹{round(float(entry_price) * int(quantity)):,}

ANALYZE THE CHART AND PROVIDE:

1. **CURRENT ASSESSMENT**
   - Read the current price (CMP) from the chart
   - Current P&L in ₹ and %
   - What phase is the stock in? (early breakout / mid-move / extended / exhaustion)
   - Is volume supporting the move?

2. **TRAILING STOP RECOMMENDATION**
   - Which EMA to trail: 5 EMA (for fast/ferocious moves), 10 EMA (normal momentum), or 20 EMA (slow grind)
   - WHY this EMA — explain based on the chart's price action relative to EMAs
   - Current approximate trailing stop price (the EMA value you can see)
   - Risk from current price to trailing stop in ₹ and %

3. **SELL INTO STRENGTH LEVELS**
   - Identify 2-3 overhead resistance levels / extension zones where partial selling makes sense
   - For each level: price target, reasoning (prior high, round number, Fibonacci extension, supply zone)
   - Probability assessment for reaching each level

4. **RISK/REWARD ASSESSMENT**
   - Downside risk (to trailing stop)
   - Upside potential (to nearest resistance)
   - Risk-reward ratio
   - Position health rating: STRONG / HEALTHY / CAUTION / EXIT

5. **LEG-BY-LEG EXIT PLAN**
   Provide a specific plan, example format:
   - Leg 1: Sell [X]% at ₹[price] (reason)
   - Leg 2: Sell [X]% at ₹[price] (reason)  
   - Leg 3: Trail remaining [X]% with [EMA] — exit on close below ₹[price]
   Include the ₹ profit and total ₹ P&L for each leg based on the {quantity} shares.

IMPORTANT RULES:
- Be SPECIFIC with prices — read them from the chart
- All monetary values in ₹ (Indian Rupees)
- If you can't read exact EMA values, estimate based on candle positions
- Be actionable — this is a real trade, not theory
- If the trade looks bad, say EXIT clearly

Return ONLY valid JSON:
{{
    "stock_name": "",
    "current_price": 0,
    "entry_price": {entry_price},
    "quantity": {quantity},
    "position_value": 0,
    "unrealized_pnl": 0,
    "unrealized_pnl_pct": 0,
    "phase": "",
    "volume_supporting": true,
    "trailing_ema": "",
    "trailing_ema_reason": "",
    "trailing_stop_price": 0,
    "risk_to_stop_pct": 0,
    "risk_to_stop_rupees": 0,
    "sell_levels": [
        {{"price": 0, "reason": "", "probability": ""}}
    ],
    "risk_reward_ratio": "",
    "position_health": "",
    "health_reason": "",
    "exit_plan": [
        {{"leg": 1, "action": "", "pct_of_position": 0, "price": 0, "shares": 0, "profit_per_share": 0, "leg_profit": 0, "reason": ""}}
    ],
    "overall_recommendation": "",
    "key_warning": ""
}}"""


# ─── Gemini Analysis ───

def analyze_with_gemini(image_path, entry_price, quantity):
    """Analyze chart using Google Gemini"""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.getenv("api_key"))
    image = PIL.Image.open(image_path)
    prompt = _build_analysis_prompt(entry_price, quantity)

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[
            image,
            f"{SYSTEM_PROMPT}\n\n{prompt}"
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )

    return _parse_response(response.text)


# ─── Claude Analysis ───

def analyze_with_claude(image_path, entry_price, quantity):
    """Analyze chart using Anthropic Claude"""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment")

    client = anthropic.Anthropic(api_key=api_key)

    # Read image as base64
    with open(image_path, "rb") as f:
        image_data = base64.b64encode(f.read()).decode("utf-8")

    # Determine media type
    ext = os.path.splitext(image_path)[1].lower()
    media_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
    media_type = media_types.get(ext, "image/png")

    prompt = _build_analysis_prompt(entry_price, quantity)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                {"type": "text", "text": prompt + "\n\nReturn ONLY valid JSON, no markdown backticks."}
            ]
        }]
    )

    raw_text = response.content[0].text
    # Strip markdown code fences if present
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    raw_text = raw_text.strip()
    if raw_text.startswith("json"):
        raw_text = raw_text[4:].strip()

    return _parse_response(raw_text)


# ─── Dual Analysis (both models) ───

def analyze_with_both(image_path, entry_price, quantity):
    """Run both Gemini and Claude, return both results"""
    results = {}
    errors = {}

    # Gemini
    try:
        results["gemini"] = analyze_with_gemini(image_path, entry_price, quantity)
    except Exception as e:
        errors["gemini"] = str(e)
        print(f"   ⚠️ Gemini failed: {e}")

    # Claude
    try:
        results["claude"] = analyze_with_claude(image_path, entry_price, quantity)
    except Exception as e:
        errors["claude"] = str(e)
        print(f"   ⚠️ Claude failed: {e}")

    if not results:
        raise ValueError(f"Both AI models failed. Gemini: {errors.get('gemini')}. Claude: {errors.get('claude')}")

    return results, errors


# ─── Response parser ───

def _parse_response(raw_text):
    """Parse JSON response from AI, handle edge cases"""
    try:
        data = json.loads(raw_text)
        # Ensure numeric fields are proper types
        for key in ["current_price", "entry_price", "quantity", "position_value",
                     "unrealized_pnl", "unrealized_pnl_pct", "trailing_stop_price",
                     "risk_to_stop_pct", "risk_to_stop_rupees"]:
            if key in data and data[key] is not None:
                try:
                    data[key] = float(str(data[key]).replace(",", "").replace("₹", ""))
                except (ValueError, TypeError):
                    pass
        return data
    except json.JSONDecodeError as e:
        print(f"   ❌ JSON parse error: {e}")
        print(f"   Raw text: {raw_text[:500]}")
        raise ValueError(f"AI returned invalid JSON: {str(e)[:100]}")

        