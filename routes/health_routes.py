"""
Health & data-freshness endpoints

Exposes read-only operational telemetry for uptime monitors, on-call
dashboards, and Slack-bot alert pollers. No auth — see app.py:67
AUTH_EXEMPT_ENDPOINTS for the exempt list.

The data here is operational metadata only: timestamps, row counts,
last-cron-run status. No user data, no PII, no sensitive values.

Endpoints:
    GET /api/health/data-freshness
        Three checks rolled into one:
          1. stock_daily_summary freshness  (cron + DB function)
          2. summary_refresh_log status      (cron success / failure)
          3. candles_daily ingestion         (listener writing today;
                                              coverage vs active universe)
        Returns ok=true only if all three are healthy. Suitable for
        an external monitor that alerts when ok=false.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify

from config.settings import is_market_open as _cfg_is_market_open
from config.settings import is_trading_day as _cfg_is_trading_day
from database.database import close_db, get_db


health_bp = Blueprint("health", __name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Summary should be refreshed at least every ~26 hours (the safety cron runs
# at 6 AM IST daily). Anything older than this counts as a degraded warning.
SUMMARY_AGE_WARN_MINUTES = 26 * 60
# Past 30 hours and we treat it as a hard error — three cron windows missed.
SUMMARY_AGE_ERROR_MINUTES = 30 * 60

# Listener health (during market hours only)
# If the most recent candles_daily UPDATE is older than this, the listener
# is probably stuck or crashed. Listener writes every ~10s, so 5 min is
# very conservative.
LISTENER_TICK_AGE_WARN_SECONDS = 5 * 60
LISTENER_TICK_AGE_ERROR_SECONDS = 15 * 60

# Post-close coverage thresholds. If by EOD the listener (+ finalize_candles
# at 4:05 PM) didn't manage to write today's row for at least this fraction
# of the active universe, something is wrong.
COVERAGE_WARN_PCT = 90.0
COVERAGE_ERROR_PCT = 70.0


@health_bp.route("/api/health/llm-heartbeat", methods=["GET"])
def llm_heartbeat():
    """Ping every configured LLM provider; return aggregate status.

    Public endpoint (in AUTH_EXEMPT_ENDPOINTS so uptime monitors can hit it
    without a JWT). Returns:
      - HTTP 200 + {"ok": true, ...}  when all configured providers respond
      - HTTP 503 + {"ok": false, ...} when any configured provider fails

    Expensive than data-freshness (does a real API call) so cache via the
    monitoring cadence — once per minute is fine; once per 5 min is plenty.
    """
    from services.llm_heartbeat import check_all
    status = check_all()
    return jsonify(status), (200 if status.get("ok") else 503)


@health_bp.route("/api/health/data-freshness", methods=["GET"])
def data_freshness():
    """Return operational state of the daily summary pipeline."""
    checked_at = datetime.now(timezone.utc)
    warnings: list[str] = []
    errors: list[str] = []

    summary_block = None
    candles_block = None
    indices_block = None
    last_run_block = None
    recent_failed = 0

    conn = get_db()
    try:
        cur = conn.cursor()

        # ── stock_daily_summary freshness ──────────────────────────────────
        cur.execute("""
            SELECT
                MAX(computed_at)                                            AS max_computed_at,
                MAX(computed_date)                                          AS max_computed_date,
                COUNT(*)                                                    AS rows_total,
                COUNT(*) FILTER (WHERE ma200 IS NOT NULL AND ma200 > 0)     AS rows_with_ma200,
                COUNT(*) FILTER (WHERE live_close IS NOT NULL)              AS rows_with_live_close
            FROM stock_daily_summary
        """)
        srow = cur.fetchone() or {}
        max_computed_at = srow.get("max_computed_at")
        if max_computed_at is None:
            errors.append("stock_daily_summary is empty")
            age_minutes = None
        else:
            age_minutes = round((checked_at - max_computed_at).total_seconds() / 60.0, 1)
            if age_minutes >= SUMMARY_AGE_ERROR_MINUTES:
                errors.append(f"stock_daily_summary is {age_minutes:.0f}m old (>{SUMMARY_AGE_ERROR_MINUTES}m)")
            elif age_minutes >= SUMMARY_AGE_WARN_MINUTES:
                warnings.append(f"stock_daily_summary is {age_minutes:.0f}m old (>{SUMMARY_AGE_WARN_MINUTES}m)")

        summary_block = {
            "computed_at": _iso(max_computed_at),
            "computed_date": _date_str(srow.get("max_computed_date")),
            "age_minutes": age_minutes,
            "rows_total": int(srow.get("rows_total") or 0),
            "rows_with_ma200": int(srow.get("rows_with_ma200") or 0),
            "rows_with_live_close": int(srow.get("rows_with_live_close") or 0),
        }

        # ── candles_daily today coverage + listener freshness ──────────────
        # Coverage tells us "did the listener + finalize_candles manage to
        # write a row for every active stock today". Listener tick age
        # tells us if the listener is currently flowing (during market
        # hours).
        cur.execute("""
            SELECT
                MAX(date) AS max_date,
                COUNT(*) FILTER (WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date)
                    AS rows_today,
                COUNT(*) FILTER (
                    WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                      AND volume > 0
                ) AS rows_today_with_volume,
                MAX(updated_at) FILTER (
                    WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                ) AS last_listener_write
            FROM candles_daily
            WHERE date >= CURRENT_DATE - 7
        """)
        crow = cur.fetchone() or {}

        cur.execute("""
            SELECT COUNT(*) AS n
            FROM stock_universe
            WHERE is_active = TRUE
              AND COALESCE(is_etf, FALSE) = FALSE
        """)
        active_stocks = int((cur.fetchone() or {}).get("n") or 0)

        rows_today = int(crow.get("rows_today") or 0)
        rows_today_with_volume = int(crow.get("rows_today_with_volume") or 0)
        last_listener_write = crow.get("last_listener_write")
        listener_tick_age_seconds = None
        if last_listener_write is not None:
            if last_listener_write.tzinfo is None:
                last_listener_write = last_listener_write.replace(tzinfo=timezone.utc)
            listener_tick_age_seconds = round(
                (checked_at - last_listener_write).total_seconds(), 1
            )

        coverage_pct = None
        if active_stocks > 0:
            coverage_pct = round(rows_today / active_stocks * 100.0, 1)

        candles_block = {
            "max_date": _date_str(crow.get("max_date")),
            "rows_today": rows_today,
            "rows_today_with_volume": rows_today_with_volume,
            "active_universe": active_stocks,
            "coverage_pct": coverage_pct,
            "last_listener_write": _iso(last_listener_write),
            "listener_tick_age_seconds": listener_tick_age_seconds,
        }

        is_trading_day_today = _cfg_is_trading_day()
        market_is_open = _cfg_is_market_open()

        # During market hours: complain if the listener has gone silent.
        if market_is_open and listener_tick_age_seconds is not None:
            if listener_tick_age_seconds >= LISTENER_TICK_AGE_ERROR_SECONDS:
                errors.append(
                    f"listener silent for {listener_tick_age_seconds:.0f}s "
                    f"(>{LISTENER_TICK_AGE_ERROR_SECONDS}s) during market hours"
                )
            elif listener_tick_age_seconds >= LISTENER_TICK_AGE_WARN_SECONDS:
                warnings.append(
                    f"listener tick age {listener_tick_age_seconds:.0f}s "
                    f"(>{LISTENER_TICK_AGE_WARN_SECONDS}s)"
                )
        if market_is_open and rows_today == 0:
            errors.append("market is open but candles_daily has zero rows for today")

        # Post-close: complain if today's coverage of the active universe
        # is too low. We only check this on trading days, after market
        # close, to avoid false positives during pre-market / mid-session
        # (when coverage naturally builds up).
        if is_trading_day_today and not market_is_open and coverage_pct is not None:
            now_ist_hour = datetime.now(IST).hour
            if now_ist_hour >= 16:  # past 4 PM IST — finalize_candles has run
                if coverage_pct < COVERAGE_ERROR_PCT:
                    errors.append(
                        f"candles_daily coverage {coverage_pct:.1f}% "
                        f"(<{COVERAGE_ERROR_PCT}%) — listener or finalize "
                        f"likely failed today"
                    )
                elif coverage_pct < COVERAGE_WARN_PCT:
                    warnings.append(
                        f"candles_daily coverage {coverage_pct:.1f}% "
                        f"(<{COVERAGE_WARN_PCT}%)"
                    )

        # ── candles_indices today coverage ──────────────────────────────────
        cur.execute("""
            SELECT
                MAX(date) AS max_date,
                COUNT(*) FILTER (WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date)
                    AS rows_today,
                COUNT(*) FILTER (
                    WHERE date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                      AND volume > 0
                ) AS rows_today_with_volume
            FROM candles_indices
            WHERE date >= CURRENT_DATE - 7
        """)
        irow = cur.fetchone() or {}
        indices_block = {
            "max_date": _date_str(irow.get("max_date")),
            "rows_today": int(irow.get("rows_today") or 0),
            "rows_today_with_volume": int(irow.get("rows_today_with_volume") or 0),
        }
        # During market hours we expect listener_indices to be writing rows;
        # volume comes later from index_volume_sync at 4 PM IST.
        if market_is_open and indices_block["rows_today"] == 0:
            warnings.append(
                "market is open but candles_indices has zero rows for today"
            )

        # ── last refresh run + failure count ────────────────────────────────
        cur.execute("""
            SELECT trigger_source, started_at, finished_at, status,
                   stocks_refreshed, error_message
            FROM summary_refresh_log
            ORDER BY started_at DESC
            LIMIT 1
        """)
        lrow = cur.fetchone()
        if lrow:
            duration = None
            if lrow.get("finished_at") and lrow.get("started_at"):
                duration = round(
                    (lrow["finished_at"] - lrow["started_at"]).total_seconds(), 2
                )
            last_run_block = {
                "trigger_source": lrow.get("trigger_source"),
                "started_at": _iso(lrow.get("started_at")),
                "finished_at": _iso(lrow.get("finished_at")),
                "duration_seconds": duration,
                "status": lrow.get("status"),
                "stocks_refreshed": lrow.get("stocks_refreshed"),
                "error_message": lrow.get("error_message"),
            }
            if lrow.get("status") == "failed":
                errors.append(f"most recent refresh failed: {lrow.get('error_message')}")
            elif lrow.get("status") == "running":
                # Still in progress — only worry if it's been running > 10 min.
                started = lrow.get("started_at")
                if started and (checked_at - started).total_seconds() > 600:
                    warnings.append("a refresh run has been 'running' for >10m — possibly stuck")
        else:
            warnings.append("summary_refresh_log has no entries — cron may not be wired up")

        cur.execute("""
            SELECT COUNT(*) AS n
            FROM summary_refresh_log
            WHERE status = 'failed'
              AND started_at > NOW() - INTERVAL '24 hours'
        """)
        frow = cur.fetchone() or {}
        recent_failed = int(frow.get("n") or 0)
        if recent_failed > 0:
            errors.append(f"{recent_failed} failed refresh run(s) in the last 24h")

    except Exception as exc:
        errors.append(f"healthcheck query failed: {type(exc).__name__}: {exc}")
    finally:
        close_db(conn)

    ok = not errors
    payload = {
        "ok": ok,
        "checked_at": _iso(checked_at),
        "summary": summary_block,
        "candles": candles_block,
        "indices": indices_block,
        "last_refresh_run": last_run_block,
        "recent_failed_runs_24h": recent_failed,
        "warnings": warnings,
        "errors": errors,
    }
    # Always return 200 so external monitors can rely on the JSON body
    # for status; HTTP-level errors should mean the service itself is down.
    return jsonify(payload), 200


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _date_str(value):
    if value is None:
        return None
    return str(value)
