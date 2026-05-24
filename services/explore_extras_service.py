"""
Extras for the Explore Strip — bits that don't fit the main cards but
enrich the right-hand panel:

  • Recent concall snapshot — period + themes management flagged on the
    last call (slugs resolved against `themes_v2` for readable labels),
    model used, when Gemini generated it.

  • Peer set — top N other companies sharing the same V1 `industry`
    (we'd prefer V2 sub-sector here, but the `peers` table is empty and
    sub-sector coverage is still thin enough that `industry` is the most
    reliable grouping today). Sorted by market cap desc so the largest
    comparable name surfaces first.

Single round-trip — both payloads come back together to keep the strip
to one extra request per stock.
"""
from __future__ import annotations

import json
from typing import Optional

from database.database import get_db, close_db


_PEER_LIMIT = 5
_CONCALL_THEME_LIMIT = 8


def _jsonb(val) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        return list(val.values()) if val else []
    try:
        parsed = json.loads(val)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return list(parsed.values()) if parsed else []
    except Exception:
        pass
    return []


def _resolve_stock(cur, symbol_or_sid: str) -> Optional[dict]:
    needle = (symbol_or_sid or "").strip()
    if not needle:
        return None
    cur.execute("""
        SELECT security_id, symbol, company_name, sector, industry
          FROM stock_universe
         WHERE security_id = %s
            OR upper(symbol) = upper(%s)
         LIMIT 1
    """, (needle, needle))
    row = cur.fetchone()
    return dict(row) if row else None


def _load_concall(cur, security_id: str) -> Optional[dict]:
    cur.execute("""
        SELECT period, period_end_date, themes_extracted,
               model_used, generated_at
          FROM concall_understanding_v2
         WHERE security_id = %s
         ORDER BY period_end_date DESC NULLS LAST,
                  generated_at DESC
         LIMIT 1
    """, (security_id,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    raw_themes = _jsonb(d.get("themes_extracted"))[:_CONCALL_THEME_LIMIT]
    themes: list[dict] = []
    if raw_themes:
        # Resolve slug → readable name + accent via themes_v2.
        cur.execute("""
            SELECT slug, name, accent_color
              FROM themes_v2
             WHERE slug = ANY(%s)
        """, (raw_themes,))
        by_slug = {r["slug"]: dict(r) for r in cur.fetchall()}
        # Preserve original ordering — Gemini writes them in importance order.
        for slug in raw_themes:
            t = by_slug.get(slug)
            if t:
                themes.append({
                    "slug": slug,
                    "name": t["name"],
                    "accent_color": t.get("accent_color"),
                })
            else:
                # Defensive: humanise the slug if it's not in themes_v2.
                themes.append({
                    "slug": slug,
                    "name": slug.replace("_", " ").title(),
                    "accent_color": None,
                })
    return {
        "period": d.get("period"),
        "period_end_date": str(d["period_end_date"]) if d.get("period_end_date") else None,
        "themes": themes,
        "model_used": d.get("model_used"),
        "generated_at": d["generated_at"].isoformat() if d.get("generated_at") else None,
    }


def _load_peers(cur, security_id: str, industry: Optional[str], limit: int = _PEER_LIMIT) -> list[dict]:
    if not industry:
        return []
    cur.execute("""
        SELECT su.security_id, su.symbol, su.company_name,
               fo.market_cap_cr, fo.pe_ratio, fo.roe, fo.current_price
          FROM stock_universe su
          LEFT JOIN fundamentals_overview fo ON fo.security_id = su.security_id
         WHERE su.industry = %s
           AND su.security_id <> %s
         ORDER BY fo.market_cap_cr DESC NULLS LAST,
                  su.symbol
         LIMIT %s
    """, (industry, security_id, limit))
    out = []
    for r in cur.fetchall():
        d = dict(r)
        for k in ("market_cap_cr", "pe_ratio", "roe", "current_price"):
            if d.get(k) is not None:
                d[k] = float(d[k])
        out.append(d)
    return out


def get_extras(symbol_or_sid: str) -> dict:
    if not symbol_or_sid:
        return {"error": "symbol_or_sid is required"}
    conn = get_db()
    try:
        cur = conn.cursor()
        stock = _resolve_stock(cur, symbol_or_sid)
        if not stock:
            return {"error": f"stock not found: {symbol_or_sid}"}
        sid = stock["security_id"]
        return {
            "symbol": stock.get("symbol"),
            "security_id": sid,
            "industry": stock.get("industry"),
            "concall": _load_concall(cur, sid),
            "peers": _load_peers(cur, sid, stock.get("industry")),
        }
    finally:
        close_db(conn)
