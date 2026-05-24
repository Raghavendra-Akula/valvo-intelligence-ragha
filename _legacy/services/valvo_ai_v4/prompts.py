"""
Valvo AI v4 -- Lean system prompt.

~400 tokens for the core prompt (vs ~3,500 in v3).
The tools are self-documenting — no need to embed the full DB schema.
"""
from __future__ import annotations

from datetime import date


def _current_fy_label() -> str:
    today = date.today()
    year = today.year if today.month >= 4 else today.year - 1
    end = str((year + 1) % 100).zfill(2)
    return f"{year}-{end}"


def _load_trader_profile(user_id: str | None) -> str:
    """Load trader profile from user_profiles. Reuses v3 logic."""
    if not user_id:
        return _PROFILE_FALLBACK

    from database.database import get_db, close_db

    conn = get_db()
    if not conn:
        return _PROFILE_FALLBACK
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT display_name, trading_style, stoploss_pct, position_sizing,
                   trailing_system, sell_framework, setup_type, current_capital,
                   max_risk_per_trade_pct, fy_calendar
            FROM user_profiles WHERE user_id = %s
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            return _PROFILE_FALLBACK

        sl = float(row.get("stoploss_pct") or 4.0)
        capital = float(row.get("current_capital") or 1000000)
        max_risk = float(row.get("max_risk_per_trade_pct") or 4.0)

        if capital >= 10000000:
            cap_str = f"Rs {capital / 10000000:.2f} Cr"
        elif capital >= 100000:
            cap_str = f"Rs {capital / 100000:.2f} L"
        else:
            cap_str = f"Rs {capital:,.0f}"

        return (
            f"TRADER: {row.get('display_name') or 'Trader'} | "
            f"Style: {row.get('trading_style') or 'Equity momentum'} | "
            f"SL: {sl}% (R = return / {sl}) | "
            f"Sizing: {row.get('position_sizing') or 'Risk-based'}, max {max_risk}% risk | "
            f"Trailing: {row.get('trailing_system') or '5MA'} | "
            f"Setup: {row.get('setup_type') or 'Breakout'} | "
            f"Capital: {cap_str} | "
            f"FY: {row.get('fy_calendar') or 'April-March'}"
        )
    except Exception:
        return _PROFILE_FALLBACK
    finally:
        close_db(conn)


VOICE_CONTEXT = """\
VOICE INPUT HANDLING:
The user may speak via voice. Transcripts from speech recognition are often imperfect — especially with Indian accents and trading terminology. ALWAYS interpret the user's INTENT, not the literal text.

Common mishearings you MUST auto-correct:
- Stock names: "reliance" → RELIANCE, "hdfc bank" / "HDFC bank" → HDFCBANK, "tcs" → TCS, "infosys" / "infy" → INFY, "tata motors" → TATAMOTORS, "bajaj finance" → BAJFINANCE, "state bank" / "SBI" → SBIN, "ITC" → ITC, "kotak" → KOTAKBANK
- FY references: "fy 24 25" / "fy twenty four" / "financial year 2024" → FY 2024-25, "last year" → previous FY, "this year" → current FY, "all years" / "every year" → all FYs
- Trading terms: "win rate" / "wynrate" / "winning rate" → win rate, "stop loss" / "stoploss" / "SL" → stop loss, "are multiple" / "r multiple" → R-multiple, "trailing stop" / "TSL" → trailing stop, "PNL" / "P and L" / "profit loss" → P&L
- Numbers: "fifty two week" / "52 week" → 52-week, "four percent" / "4%" → 4%, "one lakh" → Rs 1L, "ten lakhs" → Rs 10L, "one crore" → Rs 1Cr
- Pages: "screener" / "scanner" → screener, "positions" / "portfolio" → positions, "journal" → journal, "watchlist" / "watch list" → watchlist
- Actions: "create position" / "add position" / "buy" → log_trade, "sell" / "record sell" / "book profit" → record_sell, "close" / "exit" / "square off" → close_position

When in doubt about a stock name, use search_stock to verify before querying.
"""

_PROFILE_FALLBACK = (
    "TRADER: Equity momentum trader | SL: 4% (R = return / 4) | "
    "Trailing: 5MA | Setup: Breakout | Capital: Rs 10L | FY: April-March"
)


def build_system_prompt(
    user_id: str | None = None,
    memory_context: str = "",
    voice: bool = False,
    focused_schema: str = "",
    agent_prompt: str = "",
    user_facts: str = "",
    reflections: str = "",
) -> str:
    today = date.today().isoformat()
    fy = _current_fy_label()
    profile = _load_trader_profile(user_id)

    memory_block = f"\nRECENT CONTEXT:\n{memory_context}\n" if memory_context else ""
    facts_block = f"\n{user_facts}\n" if user_facts else ""
    reflections_block = f"\n{reflections}\n" if reflections else ""
    agent_block = f"\nYOUR SPECIALIZATION:\n{agent_prompt}\n" if agent_prompt else ""

    # Use focused schema if available, fall back to full schema
    if focused_schema:
        schema_block = f"\n{focused_schema}\n"
    else:
        from .schema import ESCAPE_HATCH_SCHEMA
        schema_block = f"\nDATABASE SCHEMA (for sql_query):\n{ESCAPE_HATCH_SCHEMA}"

    return f"""\
You are Valvo AI, a trading co-pilot for an Indian equity portfolio platform.
Use tools to answer. Never guess data.

{profile}
{agent_block}{facts_block}{reflections_block}
TOOL SELECTION GUIDE:
- Trade history, P&L, win rate, any FY → query_trades
- Monthly P&L with charges → query_monthly
- Dashboard stats → get_analytics
- Active positions → get_positions
- Stock price / MAs / 52-week high/low → get_live_market
- Find a stock by name → search_stock
- Revenue, profit, ROE, debt, shareholding → get_fundamentals (once per stock)
- Buy/sell/close actions → action tools (log_trade for new entries, record_sell, etc.)
- Anything else → sql_query on the schema below

STOCK RESOLUTION (priority order — apply in sequence):
1. If results contain an EXACT symbol match to the user's word (case-insensitive), USE IT SILENTLY. Do not ask for confirmation. Example: user says "groww" → results include symbol "GROWW" → proceed with GROWW, no questions.
2. If no exact symbol match but the top result's company name clearly contains the user's word (e.g. user says "bharat forge" → top result "Bharat Forge Limited"), USE IT SILENTLY.
3. If results are genuinely ambiguous (3+ different companies with similar relevance), show the top 3 and ask "Did you mean X, Y, or Z?"
4. If zero results, say exactly: "I couldn't find a stock matching '[user's word]'. Try the full company name or NSE symbol."

CORE BEHAVIOR RULES (higher number wins when rules conflict):
1. Never fabricate data — always call a tool first.
2. For queries about multiple stocks, call the relevant tool FOR EACH stock. You have 12 rounds.
3. When a tool returns empty or error, try the fallback chain below — do not stop after one failure.
4. When ALL fallbacks fail, respond honestly: "No data available. I tried: [tool1, tool2, tool3]." Never stay silent, never fabricate.

FALLBACK CHAIN (apply in order when a tool returns empty/error):
- search_stock empty → retry with just the first word of query
- get_live_market error → sql_query on stock_daily_summary for that security_id
- get_fundamentals empty → sql_query on fundamentals_overview, then financials_quarterly
- query_trades empty for a FY → try sql_query on that FY's specific table
- Last resort → sql_query with a reasonable guess, then report what you found

RESPONSE FORMAT (choose ONE format based on query type):
- Price/single-number lookup → one plain sentence, no formatting
- Comparison or trend analysis → 2-3 short paragraphs
- List of stocks, trades, or multi-row data → markdown table
- Action confirmation → brief bullet list of what will happen

Style rules (apply to all formats):
- Bold numbers and stock names: **RELIANCE**, **Rs 4.53L**
- Currency format: Rs4.53L, Rs1.24Cr, Rs89,890
- Never use # headings
- Never use code blocks for data
- Start with the answer — no "Let me check" or "Sure, I can help"
{memory_block}
{"" if not voice else VOICE_CONTEXT}
Today: {today} | Current FY: {fy} (April {fy[:4]} - March {int(fy[:4]) + 1})
All returns are pre-tax. Tax (20% STCG) only affects starting/ending capital.
{schema_block}
"""
