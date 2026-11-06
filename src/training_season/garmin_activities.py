"""
Pull Garmin running and swimming activities.

Each activity commits together with its splits after the network calls finish.
"""

from __future__ import annotations

from dataclasses import astuple, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, TypeGuard

from training_season.database import transaction
from training_season.garmin_auth import GarminUnavailableError
from training_season.garmin_pull_base import (
    canonical_json,
    has_garmin_value,
    non_negative,
    none_if_not_found,
    resume_start_date,
)
from training_season.timeformat import training_date, utc_now, utc_timestamp

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence
    from datetime import date
    from zoneinfo import ZoneInfo

    from garminconnect import Garmin

    from training_season.garmin_pull_base import GarminPullContext

_MAX_ACTIVITY_TYPE_DEPTH = 10
_TYPE_LIST_ERROR = "Garmin returned an unexpected activity type list"
_OLDEST_ACTIVITY_ERROR = "Garmin returned an unexpected oldest activity"
_LATEST_ACTIVITY_DATE_SQL = "SELECT max(activity_date) FROM activity"
_INSERT_ACTIVITY_SQL = """
    INSERT INTO activity (
        garmin_activity_id,
        sport,
        activity_type,
        start_time,
        activity_date,
        distance_m,
        duration_s,
        avg_hr,
        max_hr,
        avg_speed_mps,
        avg_cadence_spm,
        splits_json,
        raw_json,
        created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (garmin_activity_id) DO NOTHING
"""

ActivitySport = Literal["running", "swimming"]
_ActivityTypeInfo = tuple[str, int | None]


@dataclass(frozen=True, slots=True)
class ActivityPhase:
    """Date window and counts from one activity pull."""

    start: str
    end: str
    added: int = 0
    skipped: int = 0
    invalid: int = 0


@dataclass(frozen=True, slots=True)
class _ActivityMeasures:
    # Field order is the activity column order, the insert unpacks it.
    distance_m: float | None
    duration_s: float | None
    avg_hr: float | None
    max_hr: float | None
    avg_speed_mps: float | None
    avg_cadence_spm: float | None


@dataclass(frozen=True, slots=True)
class _ActivityRow:
    garmin_activity_id: str
    sport: ActivitySport | None
    activity_type: str
    start_time: str
    activity_date: str
    measures: _ActivityMeasures
    raw_json: str


@dataclass(frozen=True, slots=True)
class _ListedActivities:
    pending: list[_ActivityRow]
    skipped: int
    invalid: int


def _is_strict_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_start_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    # Public evidence: arpanghosh8453/garmin-grafana parses this field in
    # garmin_fetch.py:get_activity_summary and
    # garmin_bulk_importer.py:get_activities_by_date.
    timestamp = value.strip().replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _type_root_entry(entry: object) -> tuple[int, _ActivityTypeInfo] | None:
    if not isinstance(entry, dict):
        return None
    type_id = entry.get("typeId")
    type_key = entry.get("typeKey")
    parent_type_id = entry.get("parentTypeId")
    if not _is_strict_int(type_id):
        return None
    if not isinstance(type_key, str) or not type_key.strip():
        return None
    if parent_type_id is not None and not _is_strict_int(parent_type_id):
        return None
    return type_id, (type_key, parent_type_id)


def _build_type_roots(value: object) -> dict[int, _ActivityTypeInfo]:
    if not isinstance(value, list):
        raise GarminUnavailableError(_TYPE_LIST_ERROR)

    type_roots: dict[int, _ActivityTypeInfo] = {}
    for entry in value:
        root = _type_root_entry(entry)
        if root is None or root[0] in type_roots:
            raise GarminUnavailableError(_TYPE_LIST_ERROR)
        type_roots[root[0]] = root[1]
    return type_roots


def _sport_for_key(type_key: object) -> ActivitySport | None:
    if type_key == "running":
        return "running"
    if type_key == "swimming":
        return "swimming"
    return None


def _starting_parent_id(
    parent_type_id: object,
    current_type_id: object,
    type_roots: Mapping[int, _ActivityTypeInfo],
) -> object:
    if _is_strict_int(parent_type_id) and parent_type_id in type_roots:
        return parent_type_id
    if not _is_strict_int(current_type_id):
        return parent_type_id
    current_type = type_roots.get(current_type_id)
    return parent_type_id if current_type is None else current_type[1]


def _sport_from_parents(
    parent_type_id: object,
    type_roots: Mapping[int, _ActivityTypeInfo],
    visited: set[int],
) -> ActivitySport | None:
    for _ in range(_MAX_ACTIVITY_TYPE_DEPTH):
        if not _is_strict_int(parent_type_id) or parent_type_id in visited:
            return None
        visited.add(parent_type_id)
        parent = type_roots.get(parent_type_id)
        if parent is None:
            return None
        parent_key, parent_type_id = parent
        sport = _sport_for_key(parent_key)
        if sport is not None:
            return sport
    return None


def _resolve_sport(
    activity_type: dict[str, Any],
    type_roots: Mapping[int, _ActivityTypeInfo],
) -> ActivitySport | None:
    sport = _sport_for_key(activity_type.get("typeKey"))
    if sport is not None:
        return sport

    current_type_id = activity_type.get("typeId")
    parent_type_id = _starting_parent_id(
        activity_type.get("parentTypeId"), current_type_id, type_roots
    )
    visited: set[int] = set()
    if _is_strict_int(current_type_id):
        visited.add(current_type_id)
    return _sport_from_parents(parent_type_id, type_roots, visited)


def _has_activity_identity(value: dict[str, Any]) -> bool:
    activity_id = value.get("activityId")
    activity_type = value.get("activityType")
    if not _is_strict_int(activity_id) or activity_id <= 0:
        return False
    if not isinstance(activity_type, dict):
        return False
    type_key = activity_type.get("typeKey")
    return isinstance(type_key, str) and bool(type_key.strip())


def _activity_raw_json(value: dict[str, Any]) -> str | None:
    try:
        return canonical_json(value)
    except (TypeError, ValueError):
        return None


def _activity_measures(
    value: dict[str, Any], sport: ActivitySport | None
) -> _ActivityMeasures:
    cadence_key = "averageRunningCadenceInStepsPerMinute"
    return _ActivityMeasures(
        distance_m=non_negative(value.get("distance")),
        duration_s=non_negative(value.get("duration")),
        avg_hr=non_negative(value.get("averageHR")),
        max_hr=non_negative(value.get("maxHR")),
        avg_speed_mps=non_negative(value.get("averageSpeed")),
        avg_cadence_spm=(
            non_negative(value.get(cadence_key)) if sport == "running" else None
        ),
    )


def _new_activity_row(
    value: dict[str, Any],
    start: datetime,
    raw_json: str,
    type_roots: Mapping[int, _ActivityTypeInfo],
    zone: ZoneInfo,
) -> _ActivityRow:
    activity_type = value["activityType"]
    sport = _resolve_sport(activity_type, type_roots)
    return _ActivityRow(
        garmin_activity_id=str(value["activityId"]),
        sport=sport,
        activity_type=activity_type["typeKey"],
        start_time=utc_timestamp(start),
        activity_date=training_date(start, zone),
        measures=_activity_measures(value, sport),
        raw_json=raw_json,
    )


def _activity_row(
    value: object,
    type_roots: Mapping[int, _ActivityTypeInfo],
    zone: ZoneInfo,
) -> _ActivityRow | None:
    if not isinstance(value, dict) or not _has_activity_identity(value):
        return None
    start = _parse_start_time(value.get("startTimeGMT"))
    if start is None:
        return None
    raw_json = _activity_raw_json(value)
    if raw_json is None:
        return None
    return _new_activity_row(value, start, raw_json, type_roots, zone)


def _oldest_page_start(oldest_page: object) -> datetime:
    if not isinstance(oldest_page, list) or not oldest_page:
        raise GarminUnavailableError(_OLDEST_ACTIVITY_ERROR)
    oldest_activity = oldest_page[0]
    if not isinstance(oldest_activity, dict):
        raise GarminUnavailableError(_OLDEST_ACTIVITY_ERROR)
    oldest_start = _parse_start_time(oldest_activity.get("startTimeGMT"))
    if oldest_start is None:
        raise GarminUnavailableError(_OLDEST_ACTIVITY_ERROR)
    return oldest_start


def _oldest_activity_start(garmin: Garmin, zone: ZoneInfo) -> date | None:
    activity_count = garmin.count_activities()
    if not _is_strict_int(activity_count) or activity_count < 0:
        raise GarminUnavailableError("Garmin returned an unexpected activity count")
    if activity_count == 0:
        return None
    oldest_page = garmin.get_activities(start=activity_count - 1, limit=1)
    oldest_start = _oldest_page_start(oldest_page)
    return oldest_start.astimezone(zone).date() - timedelta(days=1)


def _activity_start(run: GarminPullContext) -> date | None:
    start = resume_start_date(run, _LATEST_ACTIVITY_DATE_SQL)
    if start is not None:
        return start
    return _oldest_activity_start(run.garmin, run.zone)


def _sorted_activity_rows(
    activities: Sequence[object],
    type_roots: Mapping[int, _ActivityTypeInfo],
    zone: ZoneInfo,
) -> _ListedActivities:
    pending: list[_ActivityRow] = []
    skipped = 0
    invalid = 0
    for activity in activities:
        row = _activity_row(activity, type_roots, zone)
        if row is None:
            invalid += 1
        elif row.sport is None:
            skipped += 1
        else:
            pending.append(row)
    pending.sort(key=lambda pending_row: pending_row.start_time)
    return _ListedActivities(pending, skipped, invalid)


def _listed_activities(
    run: GarminPullContext,
    type_roots: Mapping[int, _ActivityTypeInfo],
    start: date,
    end: date,
) -> _ListedActivities:
    activities = run.garmin.get_activities_by_date(
        startdate=start.isoformat(),
        enddate=end.isoformat(),
        activitytype=None,
        sortorder="asc",
    )
    if not isinstance(activities, list):
        raise GarminUnavailableError("Garmin returned an unexpected activity list")
    return _sorted_activity_rows(activities, type_roots, run.zone)


def _activity_is_stored(
    connection: sqlite3.Connection, garmin_activity_id: str
) -> bool:
    exists = connection.execute(
        "SELECT 1 FROM activity WHERE garmin_activity_id = ?",
        (garmin_activity_id,),
    ).fetchone()
    return exists is not None


def _activity_splits_json(garmin: Garmin, activity_id: str) -> str | None:
    splits = none_if_not_found(garmin.get_activity_laps, activity_id)
    if not has_garmin_value(splits):
        return None
    try:
        return canonical_json(splits)
    except (TypeError, ValueError):
        raise GarminUnavailableError(
            "Garmin returned unexpected activity laps"
        ) from None


def _insert_pulled_activity(
    connection: sqlite3.Connection, row: _ActivityRow, splits_json: str | None
) -> int:
    inserted = connection.execute(
        _INSERT_ACTIVITY_SQL,
        (
            row.garmin_activity_id,
            row.sport,
            row.activity_type,
            row.start_time,
            row.activity_date,
            *astuple(row.measures),
            splits_json,
            row.raw_json,
            utc_now(),
        ),
    )
    return 1 if inserted.rowcount == 1 else 0


def _store_new_activities(
    run: GarminPullContext, pending: Sequence[_ActivityRow]
) -> int:
    added = 0
    for row in pending:
        if _activity_is_stored(run.connection, row.garmin_activity_id):
            continue
        splits_json = _activity_splits_json(run.garmin, row.garmin_activity_id)
        with transaction(run.connection):
            added += _insert_pulled_activity(run.connection, row, splits_json)
    return added


def pull_activities(run: GarminPullContext) -> ActivityPhase:
    """Pull activities from the resume cursor to tomorrow and count the result."""
    type_roots = _build_type_roots(run.garmin.get_activity_types())
    start = _activity_start(run)
    end = run.today + timedelta(days=1)
    if start is None:
        return ActivityPhase(run.today.isoformat(), end.isoformat())
    if start > end:
        return ActivityPhase(start.isoformat(), end.isoformat())
    listed = _listed_activities(run, type_roots, start, end)
    added = _store_new_activities(run, listed.pending)
    return ActivityPhase(
        start.isoformat(), end.isoformat(), added, listed.skipped, listed.invalid
    )
