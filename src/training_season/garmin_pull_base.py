"""Value readers and resume cursor shared by the Garmin activity and wellness pulls."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, LiteralString

from garminconnect.exceptions import GarminConnectNotFoundError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from zoneinfo import ZoneInfo

    from garminconnect import Garmin

OVERLAP_DAYS = 14


@dataclass(frozen=True, slots=True)
class GarminPullContext:
    """Garmin client, database and dates shared by one pull run."""

    garmin: Garmin = field(repr=False)
    connection: sqlite3.Connection
    zone: ZoneInfo
    today: date
    history_start: str | None


def non_negative(value: object) -> float | None:
    """Read a finite number of zero or more, or None for any other value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0:
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def canonical_json(value: object) -> str:
    """Serialize with sorted keys and no spaces, and reject NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def has_garmin_value(value: object) -> bool:
    """Tell whether a Garmin response holds data: not None, not empty."""
    if value is None:
        return False
    return bool(value) if isinstance(value, dict | list) else True


def none_if_not_found(fetch: Callable[[str], object], argument: str) -> object:
    """Call a Garmin fetch and return None when Garmin says not found."""
    try:
        return fetch(argument)
    except GarminConnectNotFoundError:
        return None


def resume_start_date(
    run: GarminPullContext, latest_date_query: LiteralString
) -> date | None:
    """Return the overlap-adjusted latest stored date, else the history start."""
    cursor = run.connection.execute(latest_date_query).fetchone()
    latest = None if cursor is None else cursor[0]
    if latest is not None:
        return date.fromisoformat(latest) - timedelta(days=OVERLAP_DAYS)
    if run.history_start is not None:
        return date.fromisoformat(run.history_start)
    return None
