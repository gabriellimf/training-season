"""Helpers for storing UTC timestamps and training dates."""

import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TRAINING_TIMEZONE_ENV = "TZ"


class TrainingTimezoneError(ValueError):
    """Raised when the training timezone is missing or invalid."""


def training_zone_from_environment(
    environ: Mapping[str, str] | None = None,
) -> ZoneInfo:
    """Read the training timezone from the process environment."""
    environment = os.environ if environ is None else environ
    timezone_name = environment.get(TRAINING_TIMEZONE_ENV)
    if timezone_name is None or not timezone_name.strip():
        raise TrainingTimezoneError("TZ must name an IANA timezone")

    try:
        return ZoneInfo(timezone_name.strip())
    except (ValueError, ZoneInfoNotFoundError):
        raise TrainingTimezoneError("TZ must name an IANA timezone") from None


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


def parse_iso_date(value: object) -> date | None:
    """Read a strict YYYY-MM-DD string, or None for any other value."""
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def training_date(moment: datetime, zone: ZoneInfo) -> str:
    """Return the date of an aware datetime in the supplied training timezone."""
    _require_aware(moment)
    return moment.astimezone(zone).date().isoformat()
