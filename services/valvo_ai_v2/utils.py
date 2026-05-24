from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal


DEFAULT_BASE_CAPITAL = 50_000_000


def to_jsonable(value):
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def current_fy_start(today: date | None = None) -> date:
    today = today or date.today()
    year = today.year if today.month >= 4 else today.year - 1
    return date(year, 4, 1)


def current_fy_label(today: date | None = None) -> str:
    start = current_fy_start(today)
    end_year = str((start.year + 1) % 100).zfill(2)
    return f"{start.year}-{end_year}"


def compact_whitespace(value: str) -> str:
    return " ".join((value or "").split())


def money_text(amount) -> str:
    amount = float(amount or 0)
    sign = "+" if amount > 0 else "-" if amount < 0 else ""
    amount = abs(amount)
    if amount >= 10_000_000:
        return f"{sign}Rs {amount / 10_000_000:.2f}Cr"
    if amount >= 100_000:
        return f"{sign}Rs {amount / 100_000:.2f}L"
    if amount >= 1_000:
        return f"{sign}Rs {amount / 1_000:.1f}K"
    return f"{sign}Rs {amount:.0f}"


def pct_text(value, digits: int = 1) -> str:
    value = float(value or 0)
    return f"{value:+.{digits}f}%"
