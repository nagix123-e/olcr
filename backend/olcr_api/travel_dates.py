"""Deterministic travel-date parsing anchored to the chat client's timezone."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_DATE_TOKEN = re.compile(
    r"(?<!\d)(?:\d{4}年\s*\d{1,2}月\s*\d{1,2}日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}\s*[/.-]\s*\d{1,2}|\d{1,2}月\d{1,2}日)(?!\d)"
)


@dataclass(frozen=True)
class DateResolution:
    start_date: str | None
    end_date: str | None
    clarification: str | None


@dataclass(frozen=True)
class _ParsedDate:
    year: int | None
    month: int
    day: int


def extract_trip_date_tokens(text: str) -> tuple[str | None, str | None]:
    """Return the first two explicit date tokens in a user message."""
    found = [match.group(0).strip() for match in _DATE_TOKEN.finditer(text or "")]
    return (found[0] if found else None, found[1] if len(found) > 1 else None)


def _parse(value: str | None) -> _ParsedDate | None:
    if not isinstance(value, str) or not value.strip():
        return None
    match = _DATE_TOKEN.search(value.strip())
    if not match:
        return None
    token = match.group(0).strip()
    full = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", token)
    if full:
        return _ParsedDate(int(full[1]), int(full[2]), int(full[3]))
    japanese_full = re.fullmatch(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", token)
    if japanese_full:
        return _ParsedDate(int(japanese_full[1]), int(japanese_full[2]), int(japanese_full[3]))
    japanese = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", token)
    if japanese:
        return _ParsedDate(None, int(japanese[1]), int(japanese[2]))
    partial = re.fullmatch(r"(\d{1,2})\s*[/.-]\s*(\d{1,2})", token)
    if partial:
        return _ParsedDate(None, int(partial[1]), int(partial[2]))
    return None


def _valid_calendar_day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def resolve_trip_dates(
    start_text: str,
    end_text: str,
    client_time_zone: str | None,
    now: datetime | None = None,
) -> DateResolution:
    """Resolve explicit ISO dates and partial month/day dates without host TZ guesses.

    Partial starts resolve to the next future occurrence in the client's local
    date. A partial end resolves in the start year, or the following year when
    it falls before the start (for trips spanning New Year). If timezone data is
    unavailable, partial dates need an explicit year from the user.
    """
    start = _parse(start_text)
    end = _parse(end_text)
    if not start and not end:
        return DateResolution(None, None, None)

    zone = None
    if client_time_zone:
        try:
            zone = ZoneInfo(client_time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = None
    needs_zone = bool((start and start.year is None) or (end and end.year is None))
    if needs_zone and zone is None:
        return DateResolution(None, None, "日付に年を付けて指定してください。")

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    local_today = instant.astimezone(zone).date() if zone else instant.date()

    resolved_start: date | None = None
    if start:
        if start.year is not None:
            resolved_start = _valid_calendar_day(start.year, start.month, start.day)
        else:
            candidate = _valid_calendar_day(local_today.year, start.month, start.day)
            if candidate is None:
                return DateResolution(None, None, "有効な開始日を指定してください。")
            if candidate < local_today:
                candidate = _valid_calendar_day(local_today.year + 1, start.month, start.day)
            resolved_start = candidate
        if resolved_start is None:
            return DateResolution(None, None, "有効な開始日を指定してください。")

    resolved_end: date | None = None
    if end:
        if end.year is not None:
            resolved_end = _valid_calendar_day(end.year, end.month, end.day)
        else:
            year = resolved_start.year if resolved_start else local_today.year
            candidate = _valid_calendar_day(year, end.month, end.day)
            if candidate is None:
                return DateResolution(
                    resolved_start.isoformat() if resolved_start else None,
                    None,
                    "有効な終了日を指定してください。",
                )
            if resolved_start and candidate < resolved_start:
                candidate = _valid_calendar_day(year + 1, end.month, end.day)
            resolved_end = candidate
        if resolved_end is None:
            return DateResolution(
                resolved_start.isoformat() if resolved_start else None,
                None,
                "有効な終了日を指定してください。",
            )

    if resolved_start and resolved_end:
        days = (resolved_end - resolved_start).days
        if not 0 <= days < 31:
            return DateResolution(None, None, "開始日以降、最大31日間の日程を指定してください。")
    return DateResolution(
        resolved_start.isoformat() if resolved_start else None,
        resolved_end.isoformat() if resolved_end else None,
        None,
    )
