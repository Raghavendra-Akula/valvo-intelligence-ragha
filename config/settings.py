from datetime import datetime, timedelta, date as _date

DATABASE = "stock_scoring.db"
UPLOAD_FOLDER = "uploads"
GEMINI_MODEL = "gemini-flash-latest"

# ═══════════════════════════════════════════════════
# NSE MARKET HOLIDAYS — dates when exchange is closed
# The WebSocket VM may still insert duplicate candles on these days.
# On startup, app.py deletes any candles on these dates to keep data clean.
# Update this list each year when NSE publishes the new calendar.
# ═══════════════════════════════════════════════════

NSE_HOLIDAYS = {
    # 2021 — verified from candles_daily (zero candles on these weekdays)
    "2021-01-26",  # Republic Day
    "2021-03-11",  # Maha Shivaratri
    "2021-03-29",  # Holi
    "2021-04-02",  # Good Friday
    "2021-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2021-04-21",  # Ram Navami
    "2021-05-13",  # Id-Ul-Fitr
    "2021-07-21",  # Bakri Id
    "2021-08-19",  # Muharram
    "2021-09-10",  # Ganesh Chaturthi
    "2021-10-15",  # Dussehra
    "2021-11-05",  # Diwali-Balipratipada (Nov 4 had Muhurat trading)
    "2021-11-19",  # Guru Nanak Jayanti
    # 2022 — verified from candles_daily
    "2022-01-26",  # Republic Day
    "2022-03-01",  # Maha Shivaratri
    "2022-03-18",  # Holi
    "2022-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2022-04-15",  # Good Friday
    "2022-05-03",  # Id-Ul-Fitr
    "2022-08-09",  # Muharram
    "2022-08-15",  # Independence Day
    "2022-08-31",  # Ganesh Chaturthi
    "2022-10-05",  # Dussehra
    "2022-10-26",  # Diwali-Balipratipada (Oct 24 had Muhurat trading)
    "2022-11-08",  # Guru Nanak Jayanti
    # 2023 — verified from candles_daily
    "2023-01-26",  # Republic Day
    "2023-03-07",  # Holi
    "2023-03-30",  # Ram Navami
    "2023-04-04",  # Mahavir Jayanti
    "2023-04-07",  # Good Friday
    "2023-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2023-05-01",  # Maharashtra Day
    "2023-06-29",  # Bakri Id
    "2023-08-15",  # Independence Day
    "2023-09-19",  # Ganesh Chaturthi
    "2023-10-02",  # Mahatma Gandhi Jayanti
    "2023-10-24",  # Dussehra
    "2023-11-14",  # Diwali-Balipratipada (Nov 13 had Muhurat trading)
    "2023-11-27",  # Guru Nanak Jayanti
    "2023-12-25",  # Christmas
    # 2024 — verified from candles_daily
    "2024-01-22",  # Ram Temple Consecration (special holiday)
    "2024-01-26",  # Republic Day
    "2024-03-08",  # Maha Shivaratri
    "2024-03-25",  # Holi
    "2024-03-29",  # Good Friday
    "2024-04-11",  # Id-Ul-Fitr
    "2024-04-17",  # Ram Navami
    "2024-05-01",  # Maharashtra Day
    "2024-05-20",  # Maharashtra Elections (special holiday)
    "2024-06-17",  # Bakri Id
    "2024-07-17",  # Muharram
    "2024-08-15",  # Independence Day
    "2024-10-02",  # Mahatma Gandhi Jayanti
    "2024-11-15",  # Guru Nanak Jayanti
    "2024-11-20",  # Maharashtra Elections (special holiday)
    "2024-12-25",  # Christmas
    # 2025 — verified from candles_daily
    "2025-02-26",  # Maha Shivaratri
    "2025-03-14",  # Holi
    "2025-03-31",  # Id-Ul-Fitr (Ramadan)
    "2025-04-10",  # Mahavir Jayanti
    "2025-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2025-04-18",  # Good Friday
    "2025-05-01",  # Maharashtra Day
    "2025-08-15",  # Independence Day
    "2025-08-27",  # Ganesh Chaturthi
    "2025-10-02",  # Mahatma Gandhi Jayanti
    "2025-10-21",  # Dussehra
    "2025-10-22",  # Dussehra (day 2)
    "2025-11-05",  # Diwali (Laxmi Pujan)
    "2025-12-25",  # Christmas
    # 2026 — NSE official holidays (verify against nseindia.com circular each year)
    "2026-01-15",  # Makar Sankranti / Municipal Election
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Maha Shivaratri
    "2026-03-17",  # Holi
    "2026-03-20",  # Id-Ul-Fitr (Ramadan) — date subject to moon sighting
    "2026-03-26",  # Shri Ram Navami
    "2026-03-31",  # Shri Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-28",  # Buddha Purnima
    "2026-07-25",  # Muharram — date subject to moon sighting
    "2026-08-15",  # Independence Day
    "2026-08-18",  # Ganesh Chaturthi
    "2026-09-02",  # Eid ul-Adha (Bakri Id) — date subject to moon sighting
    "2026-09-14",  # Milad-Un-Nabi — date subject to moon sighting
    "2026-10-02",  # Mahatma Gandhi Jayanti
    "2026-10-20",  # Dussehra (Maha Navami)
    "2026-10-21",  # Dussehra (Vijaya Dashami)
    "2026-11-10",  # Diwali (Laxmi Pujan)
    "2026-11-24",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
}


# ═══════════════════════════════════════════════════
# TRADING DAY UTILITIES
# Use these instead of checking candles_daily for CURRENT_DATE.
# They check weekends + NSE_HOLIDAYS so the answer is instant
# and doesn't depend on whether the WebSocket VM inserted data.
# ═══════════════════════════════════════════════════

def _today_ist():
    """Current date in IST."""
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()


def is_nse_holiday(d=None):
    """True if the given date (or today IST) is a weekend or NSE holiday."""
    if d is None:
        d = _today_ist()
    if isinstance(d, str):
        d = _date.fromisoformat(d)
    # Saturday=5, Sunday=6
    if d.weekday() >= 5:
        return True
    return d.isoformat() in NSE_HOLIDAYS


def is_trading_day(d=None):
    """True if the given date (or today IST) is an NSE trading day."""
    return not is_nse_holiday(d)


def last_trading_date(before=None):
    """Return the most recent trading date strictly before `before` (default: today IST).
    Walks backwards from the day before `before` until a trading day is found."""
    if before is None:
        before = _today_ist()
    elif isinstance(before, str):
        before = _date.fromisoformat(before)
    d = before - timedelta(days=1)
    # Walk back at most 10 days (covers long weekends + consecutive holidays)
    for _ in range(10):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d  # fallback


def is_market_open():
    """True if NSE market is currently open (trading day, 9:15–15:30 IST)."""
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    if not is_trading_day(now_ist.date()):
        return False
    minutes = now_ist.hour * 60 + now_ist.minute
    return 555 <= minutes <= 930   # 9:15 to 15:30


def prev_trading_date_or_today():
    """If today is a trading day, return today. Otherwise return the last trading day."""
    today = _today_ist()
    if is_trading_day(today):
        return today
    return last_trading_date(today)


def count_trading_days_between(start_d, end_d):
    """Count NSE trading days strictly AFTER start_d, up to and including end_d.

    Used by event-risk countdowns ("results in N trading days") so the
    number reflects how many market sessions a trader actually has left,
    not raw calendar days. Weekends + NSE_HOLIDAYS are skipped.

    Returns 0 if end_d <= start_d. The "strictly after start_d" rule means
    that a meeting_date == today reads as 0 trading days left (i.e. it's
    happening this session).
    """
    if start_d is None or end_d is None:
        return None
    if isinstance(start_d, str):
        start_d = _date.fromisoformat(start_d[:10])
    if isinstance(end_d, str):
        end_d = _date.fromisoformat(end_d[:10])
    if end_d <= start_d:
        return 0
    count = 0
    d = start_d + timedelta(days=1)
    # Cap at ~120 iterations so a malformed far-future date can't hang us.
    for _ in range(120):
        if d > end_d:
            break
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def trading_days_until(target_d):
    """Trading days from today IST until `target_d` (inclusive)."""
    return count_trading_days_between(_today_ist(), target_d)