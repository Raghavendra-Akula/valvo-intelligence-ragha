"""
Market holiday endpoints — reads public.market_holidays (populated daily from NSE).

The upstream table stores one row per (date, segment, holiday_type) combo which
produces ~11x duplication for the same calendar day. These endpoints collapse by
calendar date so the UI can render clean "3 May — Maharashtra Day" entries with
the segments that close noted as metadata.
"""
from datetime import date, timedelta
from flask import Blueprint, jsonify, request
from extensions import limiter

market_holidays_bp = Blueprint("market_holidays", __name__)


def _get_db():
    from database.database import get_db
    return get_db()


def _close_db(conn):
    from database.database import close_db
    close_db(conn)


def _parse_date(s, default):
    if not s:
        return default
    try:
        return date.fromisoformat(s)
    except ValueError:
        return default


@market_holidays_bp.route("/api/market-holidays", methods=["GET"])
@limiter.limit("120 per minute")
def list_holidays():
    """
    Holidays grouped by calendar date.

    Query params:
      ?from=YYYY-MM-DD   (default: today)
      ?to=YYYY-MM-DD     (default: +365d)
      ?days=N            (convenience — overrides `to` with today + N)
      ?type=trading|clearing|all  (default: all)

    Returns:
      {
        "holidays": [
          {
            "date": "2026-05-01",
            "weekday": "Friday",
            "description": "Maharashtra Day",
            "types": ["trading", "clearing"],
            "segments": ["CM", "FO", ...],
            "is_trading_holiday": true,
            "is_clearing_holiday": true,
            "morning_session": null,
            "evening_session": null,
            "days_until": 8
          },
          ...
        ],
        "total": N,
        "from": "...",
        "to": "...",
      }
    """
    today = date.today()
    days_arg = request.args.get("days", type=int)
    start = _parse_date(request.args.get("from"), today)
    if days_arg and days_arg > 0:
        end = today + timedelta(days=min(days_arg, 1825))  # cap at ~5y
    else:
        end = _parse_date(request.args.get("to"), today + timedelta(days=365))
    if end < start:
        end = start

    holiday_type = (request.args.get("type", "all") or "all").lower()
    if holiday_type not in ("all", "trading", "clearing"):
        holiday_type = "all"

    type_clause = "" if holiday_type == "all" else "AND holiday_type = %(htype)s"
    params = {"start": start, "end": end, "htype": holiday_type}

    conn = None
    try:
        conn = _get_db()
        if not conn:
            return jsonify({"error": "DB unavailable", "holidays": []}), 503
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5000'")
        cur.execute(
            f"""
            SELECT holiday_date,
                   MAX(weekday)                              AS weekday,
                   MAX(description)                          AS description,
                   array_agg(DISTINCT holiday_type ORDER BY holiday_type) AS types,
                   array_agg(DISTINCT segment ORDER BY segment)           AS segments,
                   MAX(morning_session)                      AS morning_session,
                   MAX(evening_session)                      AS evening_session
            FROM market_holidays
            WHERE holiday_date BETWEEN %(start)s AND %(end)s
              {type_clause}
            GROUP BY holiday_date
            ORDER BY holiday_date ASC
            """,
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

        holidays = []
        for r in rows:
            types = list(r["types"] or [])
            d = r["holiday_date"]
            holidays.append({
                "date": d.isoformat() if d else None,
                "weekday": r["weekday"],
                "description": r["description"],
                "types": types,
                "segments": list(r["segments"] or []),
                "is_trading_holiday": "trading" in types,
                "is_clearing_holiday": "clearing" in types,
                "morning_session": r["morning_session"],
                "evening_session": r["evening_session"],
                "days_until": (d - today).days if d else None,
            })

        return jsonify({
            "holidays": holidays,
            "total": len(holidays),
            "from": start.isoformat(),
            "to": end.isoformat(),
            "type": holiday_type,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[market_holidays] error: {e}")
        return jsonify({"error": "Internal error", "holidays": []}), 500
    finally:
        if conn:
            _close_db(conn)
