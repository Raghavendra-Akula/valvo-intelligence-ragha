"""
Valvo AI v4 -- Scoring tools.

Read-only lookup and ranking from the submissions table.

The scoring specialist NEVER scores. Every final_score you see came from
the /scoring page where the user manually filled judgment parameters
(MA-following, shakeouts, sector_strength, etc.). Chat only reads what
was already computed and presents it.

Two tools:

- exec_lookup_scores: latest score for one or more symbols, with freshness
  tag (within fresh_days) and top contributors for display.

- exec_rank_stocks: calls lookup internally, sorts by final_score desc,
  applies sector_strength tie-break for near-ties, returns a ranked list
  plus any missing/stale names with /scoring links.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from database.database import get_db, close_db
from services.valvo_ai_v2.utils import to_jsonable


# ═══════════════════════════════════════════════════════════════════════════
#  Weight tables — MUST match scoring_service.py + oob_scoring_service.py
# ═══════════════════════════════════════════════════════════════════════════

# Large Base (scoring_service.py)
_LB_WEIGHTS = {
    "sector_strength": 26,
    "magnitude": 14,
    "move_ema": 12,
    "relative_strength": 10,
    "institutional_participation": 10,
    "symmetry": 8,
    "ferocity": 8,
    "adr": 7,
    "market_trend": 5,
}

# Out of Base (oob_scoring_service.py)
_OOB_WEIGHTS = {
    "sector_strength": 26,
    "move_quality": 20,
    "move_ema": 12,
    "market_trend": 12,
    "relative_strength": 10,
    "institutional_participation": 10,
    "adr": 7,
    "extension_base": 3,
}

# Param name -> submissions column holding the 0-100 individual score
_PARAM_SCORE_COL = {
    "sector_strength": "sector_strength_score",
    "magnitude": "magnitude_score",
    "move_ema": "move_ema_score",
    "relative_strength": "relative_strength_score",
    "institutional_participation": "institutional_participation_score",
    "symmetry": "symmetry_score",
    "ferocity": "ferocity_score",
    "adr": "adr_score",
    "market_trend": "market_trend_score",
    "move_quality": "move_quality_score",
    "extension_base": "extension_base_score",
}


# ═══════════════════════════════════════════════════════════════════════════
#  Config (locked during Step 2 design)
# ═══════════════════════════════════════════════════════════════════════════

_FRESH_WINDOW_DAYS = 3        # Debate 5 Q1
_TIE_BREAK_WINDOW = 0.1       # Debate "three small questions" Q2
_SCORING_PAGE_URL = "/scoring"


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


def _days_ago(ts):
    """Days between a timestamp (str or datetime) and now. Returns None on error."""
    if not ts:
        return None
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def _pick_weights(setup_type):
    if setup_type and "out_of_base" in str(setup_type).lower():
        return _OOB_WEIGHTS
    return _LB_WEIGHTS


def _scoring_link(symbol):
    return f"{_SCORING_PAGE_URL}?symbol={symbol.upper()}"


def _contributor_label(param, row):
    """Short human-readable label for a contributor param, using stored values."""
    if param == "sector_strength":
        return "strong sector" if row.get("sector_strength_score", 0) > 0 else "weak sector"
    if param == "relative_strength":
        return "RS confirmed" if row.get("relative_strength_score", 0) > 0 else "RS weak"
    if param == "institutional_participation":
        return "institutional flow" if row.get("institutional_participation_score", 0) > 0 else "no IP"
    if param == "symmetry":
        return "symmetric" if row.get("symmetry_score", 0) > 0 else "asymmetric"
    if param == "move_ema":
        ema = row.get("move_ema") or "?"
        return f"{ema} follower"
    if param == "market_trend":
        return (row.get("market_trend") or "Uptrend").lower()
    if param == "adr":
        adr = row.get("adr")
        try:
            return f"{float(adr):.1f}% ADR" if adr is not None else "ADR n/a"
        except (TypeError, ValueError):
            return "ADR n/a"
    if param == "ferocity":
        sc = row.get("ferocity_score") or 0
        return f"ferocity {int(sc)}/100"
    if param == "magnitude":
        mp = row.get("move_percentage")
        try:
            return f"{int(float(mp))}% move" if mp is not None else "magnitude n/a"
        except (TypeError, ValueError):
            return "magnitude n/a"
    if param == "move_quality":
        mp = row.get("move_percentage")
        md = row.get("move_days")
        try:
            if mp is not None and md:
                return f"{int(float(mp))}% in {int(md)}d"
        except (TypeError, ValueError):
            pass
        return "move quality"
    if param == "extension_base":
        ext = row.get("extension_pct")
        try:
            return f"{int(float(ext))}% extension" if ext is not None else "extension n/a"
        except (TypeError, ValueError):
            return "extension n/a"
    return param


def _compute_contributors(row):
    """
    Sorted list of contributor dicts: (param, individual_score, weighted_contribution, label).
    weighted_contribution is that param's contribution to the 0-10 final_score.
    """
    weights = _pick_weights(row.get("setup_type"))
    sum_w = sum(weights.values()) or 1
    items = []
    for param, weight in weights.items():
        col = _PARAM_SCORE_COL.get(param)
        if not col:
            continue
        score = row.get(col) or 0
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0
        contrib_of_10 = (score * weight) / (sum_w * 10)
        items.append({
            "param": param,
            "individual_score": round(score, 1),
            "weighted_contribution": round(contrib_of_10, 2),
            "label": _contributor_label(param, row),
        })
    items.sort(key=lambda c: c["weighted_contribution"], reverse=True)
    return items


def _setup_label(setup_type):
    """Short display label: OOB vs LB."""
    if setup_type and "out_of_base" in str(setup_type).lower():
        return "OOB"
    return "LB"


def _build_one_liner(row, rank=None, stale=False):
    """
    One-line display:
      "1. VEDL — Excellent 8.1 — OOB · 5 EMA follower, strong sector"
    """
    symbol = (row.get("symbol") or row.get("stock_name") or "?").upper()
    rating = row.get("rating") or "?"
    score = row.get("final_score")
    try:
        score_str = f"{float(score):.1f}" if score is not None else "N/A"
    except (TypeError, ValueError):
        score_str = "N/A"
    setup_lbl = _setup_label(row.get("setup_type"))
    contribs = _compute_contributors(row)
    top2 = [c["label"] for c in contribs[:2] if c["weighted_contribution"] > 0]
    top_str = ", ".join(top2) if top2 else "no standout contributors"
    stale_tag = " (stale)" if stale else ""
    prefix = f"{rank}. " if rank is not None else ""
    return f"{prefix}{symbol} — {rating} {score_str}{stale_tag} — {setup_lbl} · {top_str}"


def _parse_since_date(raw):
    """
    Normalize a since_date input into something safe to pass to PostgreSQL.

    The LLM may pass:
      - A valid ISO date '2026-04-11'
      - A valid ISO timestamp '2026-04-11T00:00:00'
      - A relative string 'last week' / '7 days ago' / garbage
      - None

    We accept the first two (return them verbatim), convert common relative
    phrases to ISO dates, and return None for anything else so the caller
    silently drops the filter instead of crashing the SQL query.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None

    # Try ISO parse first
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s
    except (ValueError, TypeError):
        pass

    # Handle a few common relative phrases the LLM might pass
    now = datetime.now(timezone.utc)
    low = s.lower()
    if low in ("today",):
        return now.date().isoformat()
    if low in ("yesterday",):
        return (now - timedelta(days=1)).date().isoformat()
    if low in ("last week", "past week", "this week"):
        return (now - timedelta(days=7)).date().isoformat()
    if low in ("last month", "past month", "this month"):
        return (now - timedelta(days=30)).date().isoformat()

    # Pattern: "N days ago"
    import re as _re
    m = _re.match(r"(\d+)\s*days?\s*ago", low)
    if m:
        try:
            days = int(m.group(1))
            if 0 <= days <= 3650:
                return (now - timedelta(days=days)).date().isoformat()
        except (ValueError, TypeError):
            pass

    # Unknown format — drop the filter rather than crash the query
    return None


def _fetch_submissions_history(cur, symbol, limit=2, since_date=None):
    """
    Return up to `limit` most recent submissions rows for this symbol
    (matches stock_name OR symbol, case-insensitive), ordered newest first.
    If `since_date` is provided (and parses to a valid date), only rows on
    or after that date are returned. Current user scoping enforced via RLS.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 2
    if limit < 1:
        limit = 1
    if limit > 10:
        limit = 10

    safe_since = _parse_since_date(since_date)

    params = [symbol, symbol]
    where_clause = "(LOWER(stock_name) = LOWER(%s) OR LOWER(symbol) = LOWER(%s))"
    if safe_since:
        where_clause += ' AND "timestamp" >= %s'
        params.append(safe_since)
    params.append(limit)

    sql = f"""
        SELECT *
        FROM submissions
        WHERE {where_clause}
        ORDER BY "timestamp" DESC
        LIMIT %s
    """
    cur.execute(sql, tuple(params))
    rows = cur.fetchall() or []
    out = []
    for row in rows:
        try:
            out.append(dict(row))
        except (TypeError, ValueError):
            cols = [d[0] for d in cur.description]
            out.append(dict(zip(cols, row)))
    return out


def _row_to_entry(row, fresh_days):
    """Convert a raw submissions row into the per-entry dict returned to the LLM."""
    age = _days_ago(row.get("timestamp"))
    fresh = age is not None and age <= fresh_days
    contribs = _compute_contributors(row)[:3]
    return {
        "fresh": fresh,
        "score": float(row.get("final_score") or 0),
        "rating": row.get("rating"),
        "setup_type": row.get("setup_type"),
        "sector": row.get("sector"),
        "scored_days_ago": age,
        "scored_at": str(row.get("timestamp") or ""),
        "top_contributors": contribs,
        "raw_inputs": {
            "move_ema": row.get("move_ema"),
            "linearity": row.get("linearity"),
            "move_percentage": row.get("move_percentage"),
            "move_days": row.get("move_days"),
            "sector_strength": row.get("sector_strength"),
            "relative_strength": row.get("relative_strength"),
            "institutional_participation": row.get("institutional_participation"),
            "symmetry": row.get("symmetry"),
            "extension_pct": row.get("extension_pct"),
            "market_trend": row.get("market_trend"),
        },
        "one_liner": _build_one_liner(row, stale=not fresh),
        "submission_id": row.get("id"),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Tool executors
# ═══════════════════════════════════════════════════════════════════════════

def exec_lookup_scores(params: dict) -> dict:
    """
    Return recent score HISTORY in submissions for each symbol.

    Params:
      symbols: list[str]            — required
      history_limit: int = 2        — how many recent submissions to return per symbol
      fresh_days: int = 3           — scores older than this are flagged fresh=False
      since_date: str | None        — ISO date/timestamp; only submissions at or after
                                       this date are returned (useful for 'last week',
                                       'this month' queries)

    Returns:
      results: list of { symbol, found, history: [entry, ...], scoring_link }
      Each entry has: fresh, score, rating, setup_type, sector, scored_days_ago,
                      scored_at, top_contributors, raw_inputs, one_liner.
    """
    raw = params.get("symbols") or []
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",") if s.strip()]
    symbols = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    if not symbols:
        return {"error": "symbols is required (list of stock names or tickers)"}

    try:
        fresh_days = int(params.get("fresh_days", _FRESH_WINDOW_DAYS))
    except (TypeError, ValueError):
        fresh_days = _FRESH_WINDOW_DAYS
    if fresh_days < 1:
        fresh_days = _FRESH_WINDOW_DAYS

    try:
        history_limit = int(params.get("history_limit", 2))
    except (TypeError, ValueError):
        history_limit = 2
    if history_limit < 1:
        history_limit = 1
    if history_limit > 10:
        history_limit = 10

    since_date = params.get("since_date")
    if since_date is not None and (not isinstance(since_date, str) or not since_date.strip()):
        since_date = None

    uid = _get_user_id()
    # Pre-normalize since_date so we can echo the real effective value
    # in the response and in the per-symbol "note" strings.
    effective_since = _parse_since_date(since_date)

    print(
        f"[lookup_scores] START symbols={symbols!r} "
        f"since_date={since_date!r} effective_since={effective_since!r} "
        f"history_limit={history_limit} fresh_days={fresh_days} uid={uid}"
    )

    results = []
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            _set_rls(cur, uid)
            for sym in symbols:
                try:
                    rows = _fetch_submissions_history(
                        cur, sym,
                        limit=history_limit,
                        since_date=effective_since,
                    )
                    print(f"[lookup_scores] {sym!r}: fetched {len(rows)} row(s)")
                except Exception as sym_exc:
                    # One symbol crashed — log and continue so one bad symbol
                    # doesn't poison the whole batch.
                    import traceback
                    print(
                        f"[lookup_scores] query failed for {sym!r}: "
                        f"{type(sym_exc).__name__}: {sym_exc}"
                    )
                    traceback.print_exc()
                    results.append({
                        "symbol": sym.upper(),
                        "found": False,
                        "history": [],
                        "scoring_link": _scoring_link(sym),
                        "note": (
                            f"Could not query scores for {sym}; try again in a moment."
                        ),
                    })
                    continue
                if not rows:
                    # Either never scored, or no score in the since_date window
                    note = (
                        f"No submission found at or after {effective_since}."
                        if effective_since else "No submission found for this symbol."
                    )
                    results.append({
                        "symbol": sym.upper(),
                        "found": False,
                        "history": [],
                        "scoring_link": _scoring_link(sym),
                        "note": note,
                    })
                    continue
                resolved_symbol = (
                    rows[0].get("symbol") or rows[0].get("stock_name") or sym
                ).upper()
                # Wrap row-to-entry mapping so a single malformed row
                # doesn't surface as a top-level "DB error" to the user.
                try:
                    history = [_row_to_entry(r, fresh_days) for r in rows]
                except Exception as fmt_exc:
                    import traceback
                    print(
                        f"[lookup_scores] row formatting failed for {sym!r}: "
                        f"{type(fmt_exc).__name__}: {fmt_exc}"
                    )
                    traceback.print_exc()
                    results.append({
                        "symbol": sym.upper(),
                        "found": False,
                        "history": [],
                        "scoring_link": _scoring_link(sym),
                        "note": (
                            f"Found a score for {sym} but could not format it. "
                            "This is a bug — please report."
                        ),
                    })
                    continue
                results.append({
                    "symbol": resolved_symbol,
                    "found": True,
                    "history": history,
                    "scoring_link": _scoring_link(sym),
                })
    except Exception as db_exc:
        # Connection or cursor-level failure — return a recoverable shape so
        # the LLM can retry or fall back, instead of raising into the engine
        # which would waste the user's turn with a hard error.
        import traceback
        print(
            f"[lookup_scores] DB error (connection/cursor level): "
            f"{type(db_exc).__name__}: {db_exc}"
        )
        traceback.print_exc()
        return to_jsonable({
            "results": [],
            "error": "Database temporarily unavailable. Retry without since_date may help.",
            "retryable": True,
            "fresh_window_days": fresh_days,
            "history_limit": history_limit,
            "since_date": since_date,
            "effective_since_date": effective_since,
        })
    finally:
        if conn is not None:
            try:
                close_db(conn)
            except Exception:
                pass

    return to_jsonable({
        "results": results,
        "fresh_window_days": fresh_days,
        "history_limit": history_limit,
        "since_date": since_date,
        "effective_since_date": effective_since,
        "since_date_dropped": bool(since_date) and not effective_since,
        "user_scoped": uid is not None,
    })


def exec_rank_stocks(params: dict) -> dict:
    """
    Rank a list of symbols by final_score (desc), with near-tie resolution
    by sector_strength. Stale scores included with stale=True; missing
    symbols returned separately with a /scoring link.
    """
    raw = params.get("symbols") or []
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.split(",") if s.strip()]
    symbols = [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    if len(symbols) < 2:
        return {"error": "rank_stocks needs at least 2 symbols"}

    try:
        fresh_days = int(params.get("fresh_days", _FRESH_WINDOW_DAYS))
    except (TypeError, ValueError):
        fresh_days = _FRESH_WINDOW_DAYS
    if fresh_days < 1:
        fresh_days = _FRESH_WINDOW_DAYS

    lookup = exec_lookup_scores({"symbols": symbols, "fresh_days": fresh_days, "history_limit": 1})
    if "error" in lookup:
        return lookup

    found_items = []
    missing = []
    for r in lookup["results"]:
        history = r.get("history") or []
        if r.get("found") and history:
            # For ranking, flatten to the single most recent entry
            latest = history[0]
            found_items.append({
                "symbol": r["symbol"],
                "found": True,
                "fresh": latest.get("fresh", False),
                "score": latest.get("score", 0),
                "rating": latest.get("rating"),
                "setup_type": latest.get("setup_type"),
                "sector": latest.get("sector"),
                "scored_days_ago": latest.get("scored_days_ago"),
                "top_contributors": latest.get("top_contributors", []),
                "raw_inputs": latest.get("raw_inputs", {}),
                "one_liner": latest.get("one_liner", ""),
            })
        else:
            missing.append({
                "symbol": r["symbol"],
                "reason": "no recent score in submissions",
                "scoring_link": r["scoring_link"],
            })

    if not found_items:
        return to_jsonable({
            "ranked": [],
            "tied_pairs": [],
            "missing_or_stale": missing,
            "ranked_count": 0,
            "total_requested": len(symbols),
            "fresh_window_days": fresh_days,
            "message": (
                "No scores found for any of the given symbols. "
                "Open /scoring for each, score them, then ask me to rank again."
            ),
        })

    # Sort highest first
    found_items.sort(key=lambda r: r["score"], reverse=True)

    # Walk the sorted list, collapsing near-ties into groups
    ranked = []
    tied_pairs = []
    rank_num = 1
    i = 0
    while i < len(found_items):
        j = i
        while (
            j + 1 < len(found_items)
            and abs(found_items[i]["score"] - found_items[j + 1]["score"]) <= _TIE_BREAK_WINDOW
        ):
            j += 1
        group = found_items[i : j + 1]

        if len(group) == 1:
            r = group[0]
            ranked.append(_format_ranked(r, str(rank_num)))
            rank_num += 1
        else:
            # Tie-break by sector_strength (True beats False)
            group.sort(key=lambda r: bool(r["raw_inputs"].get("sector_strength")), reverse=True)
            ss_values = {bool(r["raw_inputs"].get("sector_strength")) for r in group}
            if len(ss_values) == 1:
                # Still tied after sector — show as tied group
                letters = "abcdefg"
                for k, r in enumerate(group):
                    entry = _format_ranked(r, f"{rank_num}{letters[k]}")
                    entry["tied_with"] = [g["symbol"] for g in group if g["symbol"] != r["symbol"]]
                    ranked.append(entry)
                tied_pairs.append({
                    "rank_group": rank_num,
                    "symbols": [g["symbol"] for g in group],
                    "reason": (
                        f"Scores within {_TIE_BREAK_WINDOW} and sector_strength is equal. "
                        "Your judgment call."
                    ),
                })
                rank_num += 1
            else:
                # Sector tie-break resolved — assign sequential ranks
                for r in group:
                    ranked.append(_format_ranked(r, str(rank_num)))
                    rank_num += 1
        i = j + 1

    return to_jsonable({
        "ranked": ranked,
        "tied_pairs": tied_pairs,
        "missing_or_stale": missing,
        "ranked_count": len(ranked),
        "total_requested": len(symbols),
        "fresh_window_days": fresh_days,
    })


def exec_get_top_scores(params: dict) -> dict:
    """
    Return the highest-scored stocks from the current user's recent submissions,
    deduplicated so each stock appears once (at its latest score), sorted by
    final_score desc.

    Use this for queries like:
      - "what's my best setup right now"          -> limit=1
      - "my top 5 scored stocks"                  -> limit=5
      - "show me all my excellent-rated picks"    -> min_score=8
      - "rank all my recent scores"               -> limit=20

    Params:
      fresh_days: int = 30      -- window of submissions considered (default 30 days
                                   because a user's scored universe spans weeks, not days;
                                   only tighten when the user specifies 'this week' etc.)
      limit: int = 10           -- how many top stocks to return (max 50)
      min_score: float | None   -- optional minimum final_score (0-10 scale)
    """
    try:
        fresh_days = int(params.get("fresh_days", 30))
    except (TypeError, ValueError):
        fresh_days = 30
    if fresh_days < 1:
        fresh_days = 30

    try:
        limit = int(params.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    min_score_raw = params.get("min_score")
    try:
        min_score = float(min_score_raw) if min_score_raw is not None else None
    except (TypeError, ValueError):
        min_score = None

    uid = _get_user_id()
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            _set_rls(cur, uid)
            since_iso = (
                datetime.now(timezone.utc) - timedelta(days=fresh_days)
            ).isoformat()

            # DISTINCT ON (stock_name) keeps only the latest submission per stock
            # within the fresh window. Outer query (handled in Python) re-sorts
            # by final_score and applies min_score + limit.
            cur.execute(
                """
                SELECT DISTINCT ON (LOWER(stock_name)) *
                FROM submissions
                WHERE "timestamp" >= %s
                ORDER BY LOWER(stock_name), "timestamp" DESC
                """,
                (since_iso,),
            )
            raw_rows = cur.fetchall() or []
            rows = []
            for row in raw_rows:
                try:
                    rows.append(dict(row))
                except (TypeError, ValueError):
                    cols = [d[0] for d in cur.description]
                    rows.append(dict(zip(cols, row)))

            if min_score is not None:
                rows = [
                    r for r in rows
                    if float(r.get("final_score") or 0) >= min_score
                ]

            rows.sort(key=lambda r: float(r.get("final_score") or 0), reverse=True)
            rows = rows[:limit]

            entries = []
            for row in rows:
                entry = _row_to_entry(row, fresh_days)
                entry["symbol"] = (
                    row.get("symbol") or row.get("stock_name") or "?"
                ).upper()
                entry["sector"] = row.get("sector")
                entries.append(entry)
    except Exception as db_exc:
        print(f"[get_top_scores] DB error: {db_exc}")
        return to_jsonable({
            "results": [],
            "error": "Could not retrieve top scores. Try again in a moment.",
            "retryable": True,
            "fresh_window_days": fresh_days,
            "limit": limit,
            "min_score": min_score,
        })
    finally:
        if conn is not None:
            try:
                close_db(conn)
            except Exception:
                pass

    return to_jsonable({
        "results": entries,
        "fresh_window_days": fresh_days,
        "limit": limit,
        "min_score": min_score,
        "total_found": len(entries),
        "user_scoped": uid is not None,
    })
    """Compose the ranked-entry dict with the pre-built one-liner updated with rank."""
    stale = not r.get("fresh", True)
    # Rebuild the one-liner with the rank prefix (the lookup one-liner has no rank)
    # We reconstruct from row-shape by using the stored fields
    symbol = r["symbol"]
    rating = r.get("rating") or "?"
    score_val = r.get("score")
    try:
        score_str = f"{float(score_val):.1f}" if score_val is not None else "N/A"
    except (TypeError, ValueError):
        score_str = "N/A"
    setup_lbl = _setup_label(r.get("setup_type"))
    contribs = r.get("top_contributors") or []
    top2 = [c["label"] for c in contribs[:2] if c.get("weighted_contribution", 0) > 0]
    top_str = ", ".join(top2) if top2 else "no standout contributors"
    stale_tag = " (stale)" if stale else ""
    one_line = f"{rank_str}. {symbol} — {rating} {score_str}{stale_tag} — {setup_lbl} · {top_str}"
    return {
        "rank": rank_str,
        "symbol": symbol,
        "score": r["score"],
        "rating": r.get("rating"),
        "setup_type": r.get("setup_type"),
        "sector": r.get("sector"),
        "scored_days_ago": r.get("scored_days_ago"),
        "stale": stale,
        "top_contributors": contribs,
        "one_line": one_line,
    }
