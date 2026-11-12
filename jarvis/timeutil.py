"""Date helpers for the mirrors."""

from __future__ import annotations

import calendar
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def shift_months(moment: datetime, months: int) -> datetime:
    """``moment`` moved by whole months (negative = back), day clamped."""
    index = moment.year * 12 + (moment.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(ts: int | None) -> str:
    """Local calendar date for a Unix timestamp, or '-'."""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def minute(ts: int | None) -> str:
    """Local date and time for a Unix timestamp, or '-'."""
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
