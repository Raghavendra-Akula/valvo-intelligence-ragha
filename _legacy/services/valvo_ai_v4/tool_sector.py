"""
Valvo AI v4 -- Sector tool (read-only).

One merged tool: get_sectors(include_leading=False, focus=None, limit=5).

Two data sources, both read directly from the DB:

1. candles_indices (via inline SQL mirroring /api/screener/sectoral):
   37 curated indices with MA health score (0-5), 1w/1m change,
   % from 200 MA, and per-MA above/below flags. Sorted by strength.

2. leading_sectors (user-scoped):
   The user's manually-curated list of sectors they consider leading
   (set via Settings page). Surfaced as supporting context, not ranked.

Write path is intentionally NOT wired yet. To change leading_sectors,
the user visits Settings. A future follow-up may add a set_leading_sectors
v2 action if that friction becomes real.
"""
from __future__ import annotations

import json

from database.database import get_db, close_db
from services.valvo_ai_v2.utils import to_jsonable


# ═══════════════════════════════════════════════════════════════════════════
#  Curated indices (mirrors screener_routes.CURATED_INDICES)
#
#  Kept local so this tool doesn't import from routes/ (tool_* should stay
#  in services/). If screener_routes changes its index list, update here too.
#  I've set a unit test note in the docstring so a future reviewer knows.
# ═══════════════════════════════════════════════════════════════════════════

CURATED_INDICES = {
    # ── Broad (7) ──
    "NIFTY":                   {"group": "broad", "display": "Nifty 50"},
    "NIFTY 500":               {"group": "broad", "display": "Nifty 500"},
    "NIFTY NEXT 50":           {"group": "broad", "display": "Next 50"},
    "NIFTY MID100 FREE":       {"group": "broad", "display": "Midcap 100"},
    "NIFTY MIDSMALLCAP 400":   {"group": "broad", "display": "MidSmall 400"},
    "NIFTY SMALLCAP 100":      {"group": "broad", "display": "Smallcap 100"},
    "NIFTY MICROCAP250":       {"group": "broad", "display": "Microcap 250"},
    # ── Sectoral (30) ──
    "BANKNIFTY":               {"group": "sectoral", "display": "Bank Nifty"},
    "FINNIFTY":                {"group": "sectoral", "display": "Fin Nifty"},
    "NIFTY PSU BANK":          {"group": "sectoral", "display": "PSU Bank"},
    "NIFTY PVT BANK":          {"group": "sectoral", "display": "Pvt Bank"},
    "NIFTY METAL":             {"group": "sectoral", "display": "Metal"},
    "NIFTY AUTO":              {"group": "sectoral", "display": "Auto"},
    "NIFTYIT":                 {"group": "sectoral", "display": "IT"},
    "NIFTY PHARMA":            {"group": "sectoral", "display": "Pharma"},
    "NIFTY REALTY":            {"group": "sectoral", "display": "Realty"},
    "NIFTY ENERGY":            {"group": "sectoral", "display": "Energy"},
    "NIFTY FMCG":              {"group": "sectoral", "display": "FMCG"},
    "NIFTY MEDIA":             {"group": "sectoral", "display": "Media"},
    "NIFTY COMMODITIES":       {"group": "sectoral", "display": "Commodities"},
    "NIFTY HEALTHCARE":        {"group": "sectoral", "display": "Healthcare"},
    "NIFTY OIL AND GAS":       {"group": "sectoral", "display": "Oil & Gas"},
    "NIFTY CONSR DURBL":       {"group": "sectoral", "display": "Consumer Durables"},
    "NIFTY CONSUMPTION":       {"group": "sectoral", "display": "Consumption"},
    "NIFTY IND DEFENCE":       {"group": "sectoral", "display": "Defence"},
    "NIFTYINFRA":              {"group": "sectoral", "display": "Infra"},
    "NIFTYCPSE":               {"group": "sectoral", "display": "CPSE"},
    "NIFTYPSE":                {"group": "sectoral", "display": "PSE"},
    "NIFTY SERV SECTOR":       {"group": "sectoral", "display": "Services"},
    "Nifty Capital Mkt":       {"group": "sectoral", "display": "Capital Mkt"},
    "NIFTY INDIA MFG":         {"group": "sectoral", "display": "Manufacturing"},
    "NIFTY MNC":               {"group": "sectoral", "display": "MNC"},
    "NIFTY IND DIGITAL":       {"group": "sectoral", "display": "India Digital"},
    "NIFTY EV":                {"group": "sectoral", "display": "EV"},
    "Nifty Housing":           {"group": "sectoral", "display": "Housing"},
    "Nifty Mobility":          {"group": "sectoral", "display": "Mobility"},
    "Nifty Trans Logis":       {"group": "sectoral", "display": "Transport & Logistics"},
}


# ═══════════════════════════════════════════════════════════════════════════
#  Natural-language sector name -> index_symbol
#  Mirrored from chat_routes.py SECTOR_MAP (v2). Keep both in sync if one
#  gets new keys. All keys lowercase.
# ═══════════════════════════════════════════════════════════════════════════

SECTOR_MAP = {
    "defence": "NIFTY IND DEFENCE", "defense": "NIFTY IND DEFENCE",
    "metal": "NIFTY METAL", "metals": "NIFTY METAL",
    "it": "NIFTYIT", "tech": "NIFTYIT", "technology": "NIFTYIT",
    "pharma": "NIFTY PHARMA", "pharmaceutical": "NIFTY PHARMA",
    "bank": "BANKNIFTY", "banking": "BANKNIFTY", "banks": "BANKNIFTY",
    "bank nifty": "BANKNIFTY",
    "psu bank": "NIFTY PSU BANK", "psu banks": "NIFTY PSU BANK",
    "pvt bank": "NIFTY PVT BANK", "private bank": "NIFTY PVT BANK",
    "auto": "NIFTY AUTO", "automobile": "NIFTY AUTO",
    "realty": "NIFTY REALTY", "real estate": "NIFTY REALTY",
    "energy": "NIFTY ENERGY",
    "fmcg": "NIFTY FMCG",
    "media": "NIFTY MEDIA",
    "commodities": "NIFTY COMMODITIES", "commodity": "NIFTY COMMODITIES",
    "healthcare": "NIFTY HEALTHCARE", "health": "NIFTY HEALTHCARE",
    "oil": "NIFTY OIL AND GAS", "oil and gas": "NIFTY OIL AND GAS",
    "gas": "NIFTY OIL AND GAS",
    "consumer durables": "NIFTY CONSR DURBL", "durables": "NIFTY CONSR DURBL",
    "consumption": "NIFTY CONSUMPTION",
    "infra": "NIFTYINFRA", "infrastructure": "NIFTYINFRA",
    "cpse": "NIFTYCPSE", "pse": "NIFTYPSE", "psu": "NIFTYCPSE",
    "services": "NIFTY SERV SECTOR", "service": "NIFTY SERV SECTOR",
    "capital markets": "Nifty Capital Mkt", "capital market": "Nifty Capital Mkt",
    "manufacturing": "NIFTY INDIA MFG",
    "mnc": "NIFTY MNC",
    "digital": "NIFTY IND DIGITAL", "india digital": "NIFTY IND DIGITAL",
    "ev": "NIFTY EV", "electric vehicle": "NIFTY EV",
    "housing": "Nifty Housing",
    "mobility": "Nifty Mobility",
    "transport": "Nifty Trans Logis", "logistics": "Nifty Trans Logis",
    "fin": "FINNIFTY", "financial": "FINNIFTY", "fin nifty": "FINNIFTY",
    "nifty 50": "NIFTY", "nifty50": "NIFTY", "nifty": "NIFTY",
    "midcap": "NIFTY MID100 FREE", "mid cap": "NIFTY MID100 FREE",
    "smallcap": "NIFTY SMALLCAP 100", "small cap": "NIFTY SMALLCAP 100",
    "microcap": "NIFTY MICROCAP250", "micro cap": "NIFTY MICROCAP250",
    "nifty 500": "NIFTY 500",
    "next 50": "NIFTY NEXT 50",
    "midsmall": "NIFTY MIDSMALLCAP 400", "midsmallcap": "NIFTY MIDSMALLCAP 400",
    "defence sector": "NIFTY IND DEFENCE",
}


def _resolve_sector_to_index(sector_input):
    """
    Turn a plain-English sector name into an index_symbol from CURATED_INDICES.
    Returns the symbol or None.

    Matching precedence:
      1. Exact key match in SECTOR_MAP (lowered)
      2. Exact match against CURATED_INDICES key (original case)
      3. Exact match against CURATED_INDICES display (lowered)
      4. Substring both directions on SECTOR_MAP keys
    """
    if not sector_input or not isinstance(sector_input, str):
        return None
    s = sector_input.strip().lower()
    if not s:
        return None

    # 1. Exact SECTOR_MAP key
    if s in SECTOR_MAP:
        return SECTOR_MAP[s]
    # 2. Exact CURATED_INDICES key (case-insensitive)
    for sym in CURATED_INDICES:
        if sym.lower() == s:
            return sym
    # 3. CURATED_INDICES display match
    for sym, meta in CURATED_INDICES.items():
        if meta["display"].lower() == s:
            return sym
    # 4. Substring (both directions)
    for key, sym in SECTOR_MAP.items():
        if key in s or s in key:
            return sym
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _get_user_id():
    try:
        from flask import g
        return getattr(g, "user_id", None)
    except RuntimeError:
        return None


def _set_rls(cur, uid):
    if uid:
        cur.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            (json.dumps({"sub": str(uid)}),),
        )
        cur.execute("SET LOCAL ROLE authenticated")


def _fetch_sectoral_health(cur, group_filter=None):
    """
    Compute MA health for the 37 curated indices from candles_indices.

    This SQL mirrors /api/screener/sectoral in screener_routes.py (as of
    commit 69ab658). If that endpoint's logic diverges in future, update
    here as well. Deliberately NOT calling the HTTP endpoint to avoid
    intra-cluster round-trips.

    Returns: list of dicts sorted by score desc, pct_from_200ma desc.
    """
    symbols = list(CURATED_INDICES.keys())
    cur.execute(
        """
        WITH ranked AS (
            SELECT symbol, date, close,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) as rn
            FROM candles_indices
            WHERE symbol = ANY(%s)
              AND date >= CURRENT_DATE - 310
        )
        SELECT symbol,
               MAX(CASE WHEN rn = 1  THEN date END) AS latest_date,
               MAX(CASE WHEN rn = 1  THEN close END) AS cmp,
               MAX(CASE WHEN rn = 6  THEN close END) AS close_1w,
               MAX(CASE WHEN rn = 22 THEN close END) AS close_1m,
               AVG(CASE WHEN rn <= 5   THEN close END) AS ma5,
               AVG(CASE WHEN rn <= 10  THEN close END) AS ma10,
               AVG(CASE WHEN rn <= 20  THEN close END) AS ma20,
               AVG(CASE WHEN rn <= 50  THEN close END) AS ma50,
               AVG(CASE WHEN rn <= 200 THEN close END) AS ma200
        FROM ranked
        WHERE rn <= 200
        GROUP BY symbol
        """,
        (symbols,),
    )
    raw = cur.fetchall() or []

    results = []
    for r in raw:
        try:
            row = dict(r)
        except (TypeError, ValueError):
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, r))

        sym = row.get("symbol")
        if sym not in CURATED_INDICES:
            continue
        meta = CURATED_INDICES[sym]
        if group_filter and meta["group"] != group_filter:
            continue

        def _f(v):
            try:
                return float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        cmp = _f(row.get("cmp"))
        ma5 = _f(row.get("ma5"))
        ma10 = _f(row.get("ma10"))
        ma20 = _f(row.get("ma20"))
        ma50 = _f(row.get("ma50"))
        ma200 = _f(row.get("ma200"))
        close_1w = _f(row.get("close_1w")) or cmp
        close_1m = _f(row.get("close_1m")) or cmp

        above = {
            "ma5":   cmp > ma5,
            "ma10":  cmp > ma10,
            "ma20":  cmp > ma20,
            "ma50":  cmp > ma50,
            "ma200": cmp > ma200,
        }
        score = sum(above.values())
        chg_1w = round((cmp - close_1w) / close_1w * 100, 2) if close_1w else 0
        chg_1m = round((cmp - close_1m) / close_1m * 100, 2) if close_1m else 0
        pct_200 = round((cmp - ma200) / ma200 * 100, 1) if ma200 else 0

        results.append({
            "symbol": sym,
            "display": meta["display"],
            "group": meta["group"],
            "cmp": round(cmp, 1),
            "latest_date": str(row.get("latest_date") or ""),
            "score": score,             # 0-5, higher = healthier
            "above": above,             # which MAs price is above
            "pct_from_200ma": pct_200,
            "chg_1w": chg_1w,
            "chg_1m": chg_1m,
            "ma5": round(ma5, 1),
            "ma10": round(ma10, 1),
            "ma20": round(ma20, 1),
            "ma50": round(ma50, 1),
            "ma200": round(ma200, 1),
        })

    results.sort(key=lambda x: (-x["score"], -x["pct_from_200ma"]))
    return results


def _fetch_leading_sectors(cur, uid):
    """Read the user's most recent leading_sectors entry. Returns dict or None."""
    if uid:
        cur.execute(
            """
            SELECT sectors, regime, note, updated_at
            FROM leading_sectors
            WHERE user_id = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (uid,),
        )
    else:
        cur.execute(
            """
            SELECT sectors, regime, note, updated_at
            FROM leading_sectors
            ORDER BY updated_at DESC
            LIMIT 1
            """,
        )
    row = cur.fetchone()
    if row is None:
        return None
    try:
        return dict(row)
    except (TypeError, ValueError):
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def _match_focus(focus, results):
    """
    Try to match a focus string (user typed 'metal', 'pharma', 'bank nifty')
    against the results. Returns the matching entry or None.

    Matching: case-insensitive substring check on both display and symbol.
    """
    if not focus or not isinstance(focus, str):
        return None
    needle = focus.strip().lower()
    if not needle:
        return None
    # Exact display/symbol match first
    for r in results:
        if r["display"].lower() == needle or r["symbol"].lower() == needle:
            return r
    # Substring fallback
    for r in results:
        if needle in r["display"].lower() or needle in r["symbol"].lower():
            return r
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Tool executor
# ═══════════════════════════════════════════════════════════════════════════

def exec_get_sectors(params: dict) -> dict:
    """
    Return sectoral-index health, user's leading sectors, or both.

    Params:
      include_leading: bool = False   -- include user's leading_sectors entry
      focus: str | None               -- name/symbol to narrow to one sector
                                         (e.g. 'metal', 'pharma', 'bank nifty')
      limit: int = 5                  -- how many top indices to return when
                                         no focus (1..37)
      group: str | None               -- 'sectoral' or 'broad' to restrict
                                         (default both; usually sectoral)

    Returns:
      top:           top N indices by score (when focus=None)
      bottom:        bottom N indices by score (when focus=None)
      focused:       single index dict (when focus set and matched)
      leading:       user's leading_sectors entry (when include_leading=True)
      total_tracked: 37 (or 30 if group='sectoral')
      summary:       {above_ma200, above_ma50, above_ma20, total}
      data_date:     latest candles_indices date
    """
    include_leading = bool(params.get("include_leading", False))
    focus = params.get("focus") or None
    group = (params.get("group") or "").strip().lower() or None
    if group not in (None, "sectoral", "broad"):
        group = None

    try:
        limit = int(params.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    if limit < 1:
        limit = 1
    if limit > 37:
        limit = 37

    uid = _get_user_id()
    print(
        f"[get_sectors] START include_leading={include_leading} "
        f"focus={focus!r} group={group!r} limit={limit} uid={uid}"
    )

    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            _set_rls(cur, uid)

            try:
                results = _fetch_sectoral_health(cur, group_filter=group)
                print(f"[get_sectors] fetched {len(results)} index rows")
            except Exception as health_exc:
                import traceback
                print(
                    f"[get_sectors] sectoral health query failed: "
                    f"{type(health_exc).__name__}: {health_exc}"
                )
                traceback.print_exc()
                return to_jsonable({
                    "error": (
                        "Could not retrieve sectoral health data. Try again "
                        "in a moment."
                    ),
                    "retryable": True,
                    "top": [],
                    "bottom": [],
                    "focused": None,
                    "leading": None,
                })

            response = {
                "total_tracked": len(results),
                "data_date": results[0]["latest_date"] if results else None,
            }

            # Summary counts (above key MAs)
            summary = {
                "above_ma200": sum(1 for r in results if r["above"]["ma200"]),
                "above_ma50":  sum(1 for r in results if r["above"]["ma50"]),
                "above_ma20":  sum(1 for r in results if r["above"]["ma20"]),
                "above_ma5":   sum(1 for r in results if r["above"]["ma5"]),
                "total":       len(results),
            }
            response["summary"] = summary

            if focus:
                matched = _match_focus(focus, results)
                if matched is None:
                    # Graceful empty — tell the LLM what IS available
                    response["focused"] = None
                    response["focus_not_found"] = True
                    response["available_displays"] = [r["display"] for r in results[:37]]
                else:
                    response["focused"] = matched
                    response["focus_not_found"] = False
            else:
                response["top"] = results[:limit]
                response["bottom"] = results[-limit:][::-1] if len(results) > limit else []

            if include_leading:
                try:
                    leading = _fetch_leading_sectors(cur, uid)
                    if leading is None:
                        response["leading"] = None
                        response["leading_note"] = (
                            "No leading sectors set. Open Settings to pick which "
                            "sectors you're tracking as bullish."
                        )
                    else:
                        # Normalize sectors field — it's jsonb, could be list or text
                        sectors_raw = leading.get("sectors")
                        if isinstance(sectors_raw, str):
                            try:
                                sectors_raw = json.loads(sectors_raw)
                            except (ValueError, TypeError):
                                sectors_raw = [sectors_raw]
                        if not isinstance(sectors_raw, list):
                            sectors_raw = []
                        response["leading"] = {
                            "sectors": sectors_raw,
                            "regime_at_set": leading.get("regime"),
                            "note": leading.get("note"),
                            "updated_at": str(leading.get("updated_at") or ""),
                        }
                except Exception as lead_exc:
                    print(
                        f"[get_sectors] leading_sectors query failed: "
                        f"{type(lead_exc).__name__}: {lead_exc}"
                    )
                    # Don't poison the whole response — return what we have
                    response["leading"] = None
                    response["leading_note"] = (
                        "Could not retrieve leading sectors — responding with "
                        "sectoral health only."
                    )

            return to_jsonable(response)

    except Exception as db_exc:
        import traceback
        print(
            f"[get_sectors] DB error (connection/cursor level): "
            f"{type(db_exc).__name__}: {db_exc}"
        )
        traceback.print_exc()
        return to_jsonable({
            "error": "Sectoral data temporarily unavailable. Try again in a moment.",
            "retryable": True,
            "top": [],
            "bottom": [],
            "focused": None,
            "leading": None,
        })
    finally:
        if conn is not None:
            try:
                close_db(conn)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
#  Tool 2: get_sector_constituents — stocks within one sector
# ═══════════════════════════════════════════════════════════════════════════

def _fetch_constituents_with_ma(cur, index_symbol, ma_period):
    """
    For the given index_symbol, join index_constituents with latest close
    and N-day SMA from candles_daily. Returns list sorted by pct_vs_ma desc.
    Mirrors the SQL in chat_routes.query_sectoral_screener (v2).
    """
    # Require at least ma_period/2 candles in window (relaxes for MA=5/10)
    min_candles = max(ma_period // 2, 5)
    cur.execute(
        f"""
        WITH constituents AS (
            SELECT
                ic.stock_symbol,
                ic.stock_name,
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
                c.security_id, c.close AS latest_close, c.date
            FROM candles_daily c
            JOIN constituents cn ON c.security_id = cn.security_id
            ORDER BY c.security_id, c.date DESC
        ),
        ma_calc AS (
            SELECT c.security_id,
                   ROUND(AVG(c.close)::numeric, 2) AS ma_value
            FROM candles_daily c
            JOIN constituents cn ON c.security_id = cn.security_id
            WHERE c.date >= CURRENT_DATE - INTERVAL '{ma_period * 2} days'
            GROUP BY c.security_id
            HAVING COUNT(*) >= %s
        )
        SELECT
            cn.stock_symbol,
            cn.stock_name,
            lc.latest_close,
            lc.date AS price_date,
            ma.ma_value,
            ROUND(((lc.latest_close - ma.ma_value) / NULLIF(ma.ma_value, 0) * 100)::numeric, 1)
              AS pct_vs_ma
        FROM constituents cn
        JOIN latest_close lc ON cn.security_id = lc.security_id
        JOIN ma_calc ma ON cn.security_id = ma.security_id
        ORDER BY pct_vs_ma DESC
        """,
        (index_symbol, min_candles),
    )
    raw = cur.fetchall() or []
    rows = []
    for r in raw:
        try:
            rows.append(dict(r))
        except (TypeError, ValueError):
            cols = [d[0] for d in cur.description]
            rows.append(dict(zip(cols, r)))
    return rows


def exec_get_sector_constituents(params: dict) -> dict:
    """
    Return the stocks INSIDE a sector index with MA-health detail.

    Params:
      sector: str (required)         -- 'metal' / 'pharma' / 'bank nifty' / etc.
      ma_period: int = 20            -- MA to compute: 5 / 10 / 20 / 50 / 200
      condition: str = 'all'         -- 'above' | 'below' | 'all'
      limit: int = 25                -- max rows per returned slice

    Returns:
      sector: {symbol, display, group}
      ma_period: int
      total_stocks: int
      above: list        -- stocks above the MA, sorted by distance desc
      below: list        -- stocks below the MA, sorted by distance desc (most negative first)
      breadth: {
          above_count, below_count, pct_above_ma
      }
      data_date: str (latest price date seen)
    """
    sector_input = params.get("sector")
    if not sector_input:
        return {"error": "sector is required (e.g. 'metal', 'pharma', 'bank nifty')"}

    try:
        ma_period = int(params.get("ma_period", 20))
    except (TypeError, ValueError):
        ma_period = 20
    if ma_period not in (5, 10, 20, 50, 200):
        # Accept exotic values but prefer canonical MAs
        if ma_period < 5:
            ma_period = 5
        elif ma_period > 200:
            ma_period = 200

    condition = (params.get("condition") or "all").strip().lower()
    if condition not in ("above", "below", "all"):
        condition = "all"

    try:
        limit = int(params.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    index_symbol = _resolve_sector_to_index(sector_input)
    if index_symbol is None:
        return {
            "error": f"Could not match '{sector_input}' to a tracked sector.",
            "hint": "Try: defence, metal, IT, pharma, bank, PSU bank, auto, realty, "
                    "energy, FMCG, healthcare, oil and gas, EV, housing, midcap, smallcap.",
        }

    if index_symbol not in CURATED_INDICES:
        return {
            "error": f"Matched '{index_symbol}' but it's not in our curated 37.",
        }
    meta = CURATED_INDICES[index_symbol]

    uid = _get_user_id()
    print(
        f"[get_sector_constituents] START sector={sector_input!r} "
        f"-> {index_symbol} ma={ma_period} cond={condition} limit={limit} uid={uid}"
    )

    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            _set_rls(cur, uid)
            try:
                rows = _fetch_constituents_with_ma(cur, index_symbol, ma_period)
                print(f"[get_sector_constituents] fetched {len(rows)} constituents")
            except Exception as q_exc:
                import traceback
                print(
                    f"[get_sector_constituents] query failed: "
                    f"{type(q_exc).__name__}: {q_exc}"
                )
                traceback.print_exc()
                return to_jsonable({
                    "error": "Could not query sector constituents. Try again in a moment.",
                    "retryable": True,
                    "sector": {
                        "symbol": index_symbol,
                        "display": meta["display"],
                        "group": meta["group"],
                    },
                    "above": [],
                    "below": [],
                })

            if not rows:
                return to_jsonable({
                    "sector": {
                        "symbol": index_symbol,
                        "display": meta["display"],
                        "group": meta["group"],
                    },
                    "ma_period": ma_period,
                    "total_stocks": 0,
                    "above": [],
                    "below": [],
                    "breadth": {"above_count": 0, "below_count": 0, "pct_above_ma": 0},
                    "data_date": None,
                    "note": f"No constituent price data found for {meta['display']}.",
                })

            # Split above/below; rows already sorted by pct_vs_ma DESC
            above = []
            below = []
            data_date = None
            for r in rows:
                close = float(r.get("latest_close") or 0)
                ma = float(r.get("ma_value") or 0)
                if data_date is None and r.get("price_date"):
                    data_date = str(r["price_date"])
                entry = {
                    "symbol": r.get("stock_symbol"),
                    "name": r.get("stock_name"),
                    "close": close,
                    "ma_value": ma,
                    "pct_vs_ma": float(r.get("pct_vs_ma") or 0),
                }
                if close >= ma:
                    above.append(entry)
                else:
                    below.append(entry)

            # Below is sorted asc by pct_vs_ma (most negative first)
            below.sort(key=lambda e: e["pct_vs_ma"])

            total = len(rows)
            response = {
                "sector": {
                    "symbol": index_symbol,
                    "display": meta["display"],
                    "group": meta["group"],
                },
                "ma_period": ma_period,
                "total_stocks": total,
                "breadth": {
                    "above_count": len(above),
                    "below_count": len(below),
                    "pct_above_ma": round(len(above) / total * 100) if total else 0,
                },
                "data_date": data_date,
            }

            # Apply condition filtering to the returned slices
            if condition == "above":
                response["above"] = above[:limit]
                response["below"] = []
            elif condition == "below":
                response["above"] = []
                response["below"] = below[:limit]
            else:
                response["above"] = above[:limit]
                response["below"] = below[:limit]

            return to_jsonable(response)
    except Exception as db_exc:
        import traceback
        print(
            f"[get_sector_constituents] DB error: "
            f"{type(db_exc).__name__}: {db_exc}"
        )
        traceback.print_exc()
        return to_jsonable({
            "error": "Sector constituents temporarily unavailable. Try again.",
            "retryable": True,
            "above": [],
            "below": [],
        })
    finally:
        if conn is not None:
            try:
                close_db(conn)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
#  Tool 3: compare_sectors — head-to-head on 2-5 sectors
# ═══════════════════════════════════════════════════════════════════════════

def exec_compare_sectors(params: dict) -> dict:
    """
    Compare 2-5 sectors side by side on index-level health metrics.

    Params:
      sectors: list[str] (required) -- 2 to 5 sector names

    Returns:
      comparison: list of dicts with {
        symbol, display, group,
        score, above, chg_1w, chg_1m, pct_from_200ma,
        cmp, ma5, ma10, ma20, ma50, ma200
      }
      Sorted by score desc, pct_from_200ma desc (like get_sectors).
      Plus a tiny 'winner' hint for the LLM:
        strongest: display of top
        weakest: display of bottom
    """
    raw = params.get("sectors") or []
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",") if s.strip()]
    sectors = [s.strip() for s in raw if isinstance(s, str) and s.strip()]

    if len(sectors) < 2:
        return {
            "error": "compare_sectors needs at least 2 sectors. "
                     "For a single sector, use get_sectors(focus=X) instead.",
        }
    if len(sectors) > 5:
        return {
            "error": f"Too many sectors ({len(sectors)}). Compare up to 5 at a time.",
        }

    resolved = []
    unresolved = []
    for s in sectors:
        sym = _resolve_sector_to_index(s)
        if sym and sym in CURATED_INDICES:
            resolved.append((s, sym))
        else:
            unresolved.append(s)

    if unresolved and not resolved:
        return {
            "error": f"Could not match any of: {unresolved}",
            "hint": "Try: metal, pharma, IT, bank, auto, etc.",
        }

    uid = _get_user_id()
    print(
        f"[compare_sectors] START resolved={resolved} unresolved={unresolved} uid={uid}"
    )

    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            _set_rls(cur, uid)
            try:
                health = _fetch_sectoral_health(cur)
            except Exception as h_exc:
                import traceback
                print(
                    f"[compare_sectors] health fetch failed: "
                    f"{type(h_exc).__name__}: {h_exc}"
                )
                traceback.print_exc()
                return to_jsonable({
                    "error": "Sectoral health data temporarily unavailable. Try again.",
                    "retryable": True,
                    "comparison": [],
                })

            # Pick only the requested symbols, in the order they were requested
            want_symbols = {sym for _, sym in resolved}
            picked = [h for h in health if h["symbol"] in want_symbols]
            # Re-sort by score desc, pct_from_200ma desc (health is already sorted
            # but picked subset may need re-sort after filtering)
            picked.sort(key=lambda h: (-h["score"], -h["pct_from_200ma"]))

            response = {
                "comparison": picked,
                "requested_count": len(sectors),
                "resolved_count": len(picked),
                "data_date": picked[0]["latest_date"] if picked else None,
            }
            if unresolved:
                response["unresolved"] = unresolved
                response["unresolved_note"] = (
                    f"Could not match: {', '.join(unresolved)}. "
                    "Returning the sectors I could resolve."
                )
            if picked:
                response["strongest"] = picked[0]["display"]
                response["weakest"] = picked[-1]["display"]
            return to_jsonable(response)
    except Exception as db_exc:
        import traceback
        print(
            f"[compare_sectors] DB error: {type(db_exc).__name__}: {db_exc}"
        )
        traceback.print_exc()
        return to_jsonable({
            "error": "Sectoral comparison temporarily unavailable. Try again.",
            "retryable": True,
            "comparison": [],
        })
    finally:
        if conn is not None:
            try:
                close_db(conn)
            except Exception:
                pass
