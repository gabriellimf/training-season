"""Helpers for storing UTC timestamps and training dates."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def utc_timestamp(moment: datetime) -> str:
    """Format an aware datetime as a UTC timestamp with second precision."""
    _require_aware(moment)
    return moment.astimezone(UTC).strftime(UTC_TIMESTAMP_FORMAT)


def utc_now() -> str:
    """Return the current UTC timestamp with second precision."""
    return utc_timestamp(datetime.now(UTC))


def training_date(moment: datetime, zone: ZoneInfo) -> str:
    """Return the date of an aware datetime in the supplied training timezone."""
    _require_aware(moment)
    return moment.astimezone(zone).date().isoformat()
