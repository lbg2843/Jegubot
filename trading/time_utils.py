from __future__ import annotations

from datetime import datetime, timedelta, timezone


UTC = timezone.utc
SEOUL_TZ = timezone(timedelta(hours=9))


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc_now() -> str:
    return utc_now().isoformat()


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_iso_utc(value: str) -> datetime:
    return to_utc(datetime.fromisoformat(value))


def format_for_user(value: datetime | str | None) -> str:
    if value is None:
        return "-"
    dt = parse_iso_utc(value) if isinstance(value, str) else to_utc(value)
    return dt.astimezone(SEOUL_TZ).strftime("%Y-%m-%d %H:%M:%S KST")
