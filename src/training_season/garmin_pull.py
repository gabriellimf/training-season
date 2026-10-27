"""
Pull Garmin history into SQLite using resumable write units.

Each activity and its splits commit together after network calls finish. Wellness
commits one date window at a time. Both cursors include a fixed overlap so a rerun
can resume after an outage without duplicating activities.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from garminconnect.exceptions import GarminConnectNotFoundError

from training_season.database import (
    DatabaseConfigurationError,
    MigrationError,
    connect,
    database_path_from_environment,
    initialize_database,
    transaction,
)
from training_season.garmin_auth import (
    LOCK_TIMEOUT_SECONDS,
    GarminAuthConfig,
    GarminConfigurationError,
    GarminError,
    GarminFactory,
    GarminLoginReason,
    GarminMfaRequiredError,
    GarminUnavailableError,
    default_garmin_factory,
    garmin_auth_config_from_environment,
    garmin_session,
)
from training_season.timeformat import (
    TrainingTimezoneError,
    training_date,
    training_zone_from_environment,
    utc_now,
    utc_timestamp,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from zoneinfo import ZoneInfo

    from garminconnect import Garmin

OVERLAP_DAYS = 14
WELLNESS_WINDOW_DAYS = 28
GARMIN_HISTORY_START_ENV = "GARMIN_HISTORY_START"
_MAX_ACTIVITY_TYPE_DEPTH = 10
_HISTORY_START_ERROR = (
    "GARMIN_HISTORY_START must be a valid YYYY-MM-DD date no later than today"
)
_LOGGER = logging.getLogger(__name__)

ActivitySport = Literal["running", "swimming"]
_ActivityTypeInfo = tuple[str, int | None]


@dataclass(frozen=True, slots=True)
class GarminPullResult:
    """Counts and date windows from one Garmin pull."""

    login_reason: GarminLoginReason | None
    activity_start: str
    activity_end: str
    activities_added: int
    activities_skipped: int
    activities_invalid: int
    wellness_start: str
    wellness_end: str
    wellness_days_written: int


@dataclass(frozen=True, slots=True)
class _ActivityRow:
    garmin_activity_id: str
    sport: ActivitySport | None
    activity_type: str
    start_time: str
    activity_date: str
    distance_m: float | None
    duration_s: float | None
    avg_hr: float | None
    max_hr: float | None
    avg_speed_mps: float | None
    avg_cadence_spm: float | None
    raw_json: str


@dataclass(frozen=True, slots=True)
class _WellnessRow:
    wellness_date: str
    resting_hr: float | None
    sleep_seconds: float | None
    body_battery_high: float | None
    body_battery_low: float | None
    stress_avg: float | None
    hrv_last_night_ms: float | None
    vo2max: float | None
    raw_json: str


@dataclass(frozen=True, slots=True)
class _ActivityPhase:
    start: str
    end: str
    added: int
    skipped: int
    invalid: int


@dataclass(frozen=True, slots=True)
class _WellnessPhase:
    start: str
    end: str
    written: int


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _history_start_date(value: str | None) -> date | None:
    if value is None or not value.strip():
        return None
    parsed = _parse_date(value)
    if parsed is None:
        raise GarminConfigurationError(_HISTORY_START_ERROR)
    return parsed


def history_start_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read an optional, strictly formatted first-pull start date."""
    environment = os.environ if environ is None else environ
    history_start = _history_start_date(environment.get(GARMIN_HISTORY_START_ENV))
    return None if history_start is None else history_start.isoformat()


def _non_negative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0:
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


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


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _build_type_roots(value: object) -> dict[int, _ActivityTypeInfo]:
    if not isinstance(value, list):
        raise GarminUnavailableError("Garmin returned an unexpected activity type list")

    type_roots: dict[int, _ActivityTypeInfo] = {}
    for entry in value:
        if not isinstance(entry, dict):
            raise GarminUnavailableError(
                "Garmin returned an unexpected activity type list"
            )
        type_id = entry.get("typeId")
        type_key = entry.get("typeKey")
        parent_type_id = entry.get("parentTypeId")
        if (
            isinstance(type_id, bool)
            or not isinstance(type_id, int)
            or not isinstance(type_key, str)
            or not type_key.strip()
            or (
                parent_type_id is not None
                and (
                    isinstance(parent_type_id, bool)
                    or not isinstance(parent_type_id, int)
                )
            )
            or type_id in type_roots
        ):
            raise GarminUnavailableError(
                "Garmin returned an unexpected activity type list"
            )
        type_roots[type_id] = (type_key, parent_type_id)
    return type_roots


def _resolve_sport(
    activity_type: dict[str, Any],
    type_roots: Mapping[int, _ActivityTypeInfo],
) -> ActivitySport | None:
    type_key = activity_type.get("typeKey")
    if type_key == "running":
        return "running"
    if type_key == "swimming":
        return "swimming"

    parent_type_id = activity_type.get("parentTypeId")
    current_type_id = activity_type.get("typeId")
    if (
        (
            isinstance(parent_type_id, bool)
            or not isinstance(parent_type_id, int)
            or parent_type_id not in type_roots
        )
        and isinstance(current_type_id, int)
        and not isinstance(current_type_id, bool)
    ):
        current_type = type_roots.get(current_type_id)
        if current_type is not None:
            parent_type_id = current_type[1]

    visited: set[int] = set()
    if isinstance(current_type_id, int) and not isinstance(current_type_id, bool):
        visited.add(current_type_id)

    for _ in range(_MAX_ACTIVITY_TYPE_DEPTH):
        if (
            isinstance(parent_type_id, bool)
            or not isinstance(parent_type_id, int)
            or parent_type_id in visited
        ):
            return None
        visited.add(parent_type_id)
        parent = type_roots.get(parent_type_id)
        if parent is None:
            return None
        parent_key, parent_parent_id = parent
        if parent_key == "running":
            return "running"
        if parent_key == "swimming":
            return "swimming"
        parent_type_id = parent_parent_id
    return None


def _activity_row(
    value: object,
    type_roots: Mapping[int, _ActivityTypeInfo],
    zone: ZoneInfo,
) -> _ActivityRow | None:
    if not isinstance(value, dict):
        return None

    activity_id = value.get("activityId")
    activity_type = value.get("activityType")
    if (
        isinstance(activity_id, bool)
        or not isinstance(activity_id, int)
        or activity_id <= 0
        or not isinstance(activity_type, dict)
    ):
        return None

    type_key = activity_type.get("typeKey")
    if not isinstance(type_key, str) or not type_key.strip():
        return None

    start = _parse_start_time(value.get("startTimeGMT"))
    if start is None:
        return None
    try:
        raw_json = _dumps(value)
    except (TypeError, ValueError):
        return None

    sport = _resolve_sport(activity_type, type_roots)
    return _ActivityRow(
        garmin_activity_id=str(activity_id),
        sport=sport,
        activity_type=type_key,
        start_time=utc_timestamp(start),
        activity_date=training_date(start, zone),
        distance_m=_non_negative(value.get("distance")),
        duration_s=_non_negative(value.get("duration")),
        avg_hr=_non_negative(value.get("averageHR")),
        max_hr=_non_negative(value.get("maxHR")),
        avg_speed_mps=_non_negative(value.get("averageSpeed")),
        avg_cadence_spm=(
            _non_negative(value.get("averageRunningCadenceInStepsPerMinute"))
            if sport == "running"
            else None
        ),
        raw_json=raw_json,
    )


def _has_data(value: object) -> bool:
    if value is None:
        return False
    return bool(value) if isinstance(value, dict | list) else True


def _wellness_rows(
    days: Sequence[date],
    daily_summaries: Mapping[str, object],
    hrv_by_day: Mapping[str, object],
    max_metrics_by_day: Mapping[str, object],
) -> list[_WellnessRow]:
    rows: list[_WellnessRow] = []
    for day in days:
        day_key = day.isoformat()
        daily_summary = daily_summaries.get(day_key)
        hrv = hrv_by_day.get(day_key)
        max_metrics = max_metrics_by_day.get(day_key)
        if not any(_has_data(source) for source in (daily_summary, hrv, max_metrics)):
            continue

        daily_summary_value = daily_summary if _has_data(daily_summary) else None
        hrv_value = hrv if _has_data(hrv) else None
        max_metrics_value = max_metrics if _has_data(max_metrics) else None
        raw = {
            "daily_summary": daily_summary_value,
            "hrv": hrv_value,
            "max_metrics": max_metrics_value,
        }
        try:
            raw_json = _dumps(raw)
        except (TypeError, ValueError):
            raise GarminUnavailableError(
                "Garmin returned unexpected wellness data"
            ) from None

        daily_values = daily_summary_value
        if not isinstance(daily_values, dict):
            daily_values = {}
        hrv_summary = (
            hrv_value.get("hrvSummary") if isinstance(hrv_value, dict) else None
        )
        hrv_last_night = (
            hrv_summary.get("lastNightAvg") if isinstance(hrv_summary, dict) else None
        )
        rows.append(
            _WellnessRow(
                wellness_date=day_key,
                resting_hr=_non_negative(daily_values.get("restingHeartRate")),
                sleep_seconds=_non_negative(daily_values.get("sleepingSeconds")),
                body_battery_high=_non_negative(
                    daily_values.get("bodyBatteryHighestValue")
                ),
                body_battery_low=_non_negative(
                    daily_values.get("bodyBatteryLowestValue")
                ),
                stress_avg=_non_negative(daily_values.get("averageStressLevel")),
                hrv_last_night_ms=_non_negative(hrv_last_night),
                vo2max=_vo2max_from(max_metrics_value),
                raw_json=raw_json,
            )
        )
    return rows


def _vo2max_from(payload: object) -> float | None:
    # Public evidence: arpanghosh8453/garmin-grafana:garmin_fetch.py:get_vo2_max
    # reads this path. This shape is not verified against this account.
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0]
    if not isinstance(first, dict):
        return None
    generic = first.get("generic")
    if not isinstance(generic, dict):
        return None
    return _non_negative(generic.get("vo2MaxPreciseValue"))


def _pull_activities(
    garmin: Garmin,
    connection: sqlite3.Connection,
    zone: ZoneInfo,
    today: date,
    history_start: str | None,
) -> _ActivityPhase:
    type_roots = _build_type_roots(garmin.get_activity_types())
    cursor = connection.execute("SELECT max(activity_date) FROM activity").fetchone()
    latest = None if cursor is None else cursor[0]
    if latest is not None:
        activity_start = date.fromisoformat(latest) - timedelta(days=OVERLAP_DAYS)
    elif history_start is not None:
        activity_start = date.fromisoformat(history_start)
    else:
        activity_count = garmin.count_activities()
        if (
            isinstance(activity_count, bool)
            or not isinstance(activity_count, int)
            or activity_count < 0
        ):
            raise GarminUnavailableError("Garmin returned an unexpected activity count")
        if activity_count == 0:
            return _ActivityPhase(
                start=today.isoformat(),
                end=(today + timedelta(days=1)).isoformat(),
                added=0,
                skipped=0,
                invalid=0,
            )
        oldest_page = garmin.get_activities(start=activity_count - 1, limit=1)
        if not isinstance(oldest_page, list) or not oldest_page:
            raise GarminUnavailableError(
                "Garmin returned an unexpected oldest activity"
            )
        oldest_activity = oldest_page[0]
        if not isinstance(oldest_activity, dict):
            raise GarminUnavailableError(
                "Garmin returned an unexpected oldest activity"
            )
        oldest_start = _parse_start_time(oldest_activity.get("startTimeGMT"))
        if oldest_start is None:
            raise GarminUnavailableError(
                "Garmin returned an unexpected oldest activity"
            )
        activity_start = oldest_start.astimezone(zone).date() - timedelta(days=1)

    activity_end = today + timedelta(days=1)
    if activity_start > activity_end:
        return _ActivityPhase(
            start=activity_start.isoformat(),
            end=activity_end.isoformat(),
            added=0,
            skipped=0,
            invalid=0,
        )

    activities = garmin.get_activities_by_date(
        startdate=activity_start.isoformat(),
        enddate=activity_end.isoformat(),
        activitytype=None,
        sortorder="asc",
    )
    if not isinstance(activities, list):
        raise GarminUnavailableError("Garmin returned an unexpected activity list")

    pending: list[tuple[_ActivityRow, ActivitySport]] = []
    skipped = 0
    invalid = 0
    for activity in activities:
        row = _activity_row(activity, type_roots, zone)
        if row is None:
            invalid += 1
        elif row.sport is None:
            skipped += 1
        else:
            pending.append((row, row.sport))
    pending.sort(key=lambda item: item[0].start_time)

    added = 0
    for row, sport in pending:
        exists = connection.execute(
            "SELECT 1 FROM activity WHERE garmin_activity_id = ?",
            (row.garmin_activity_id,),
        ).fetchone()
        if exists is not None:
            continue

        try:
            splits = garmin.get_activity_laps(row.garmin_activity_id)
        except GarminConnectNotFoundError:
            splits = None
        splits_json = None
        if _has_data(splits):
            try:
                splits_json = _dumps(splits)
            except (TypeError, ValueError):
                raise GarminUnavailableError(
                    "Garmin returned unexpected activity laps"
                ) from None

        with transaction(connection):
            inserted = connection.execute(
                """
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
                """,
                (
                    row.garmin_activity_id,
                    sport,
                    row.activity_type,
                    row.start_time,
                    row.activity_date,
                    row.distance_m,
                    row.duration_s,
                    row.avg_hr,
                    row.max_hr,
                    row.avg_speed_mps,
                    row.avg_cadence_spm,
                    splits_json,
                    row.raw_json,
                    utc_now(),
                ),
            )
            if inserted.rowcount == 1:
                added += 1

    return _ActivityPhase(
        start=activity_start.isoformat(),
        end=activity_end.isoformat(),
        added=added,
        skipped=skipped,
        invalid=invalid,
    )


def _pull_wellness(
    garmin: Garmin,
    connection: sqlite3.Connection,
    today: date,
    history_start: str | None,
) -> _WellnessPhase:
    cursor = connection.execute("SELECT max(wellness_date) FROM wellness").fetchone()
    latest = None if cursor is None else cursor[0]
    if latest is not None:
        wellness_start = date.fromisoformat(latest) - timedelta(days=OVERLAP_DAYS)
    elif history_start is not None:
        wellness_start = date.fromisoformat(history_start)
    else:
        earliest = connection.execute(
            "SELECT min(activity_date) FROM activity"
        ).fetchone()
        earliest_activity = None if earliest is None else earliest[0]
        wellness_start = (
            today
            if earliest_activity is None
            else date.fromisoformat(earliest_activity)
        )

    if wellness_start > today:
        return _WellnessPhase(
            start=wellness_start.isoformat(),
            end=today.isoformat(),
            written=0,
        )

    written = 0
    current = wellness_start
    while current <= today:
        window_end = min(
            current + timedelta(days=WELLNESS_WINDOW_DAYS - 1),
            today,
        )
        start_text = current.isoformat()
        end_text = window_end.isoformat()
        summary_response = garmin.get_daily_summaries(start_text, end_text)
        if not isinstance(summary_response, dict) or not isinstance(
            summary_response.get("data"), list
        ):
            raise GarminUnavailableError(
                "Garmin returned an unexpected daily summary list"
            )

        daily_summaries: dict[str, object] = {}
        for summary in summary_response["data"]:
            if not isinstance(summary, dict):
                continue
            summary_date = _parse_date(summary.get("calendarDate"))
            if summary_date is not None and current <= summary_date <= window_end:
                daily_summaries[summary_date.isoformat()] = summary

        days = [
            current + timedelta(days=offset)
            for offset in range((window_end - current).days + 1)
        ]
        hrv_by_day: dict[str, object] = {}
        max_metrics_by_day: dict[str, object] = {}
        for day in days:
            day_key = day.isoformat()
            try:
                hrv = garmin.get_hrv_data(day_key)
            except GarminConnectNotFoundError:
                hrv = None
            try:
                max_metrics = garmin.get_max_metrics(day_key)
            except GarminConnectNotFoundError:
                max_metrics = None
            hrv_by_day[day_key] = hrv
            max_metrics_by_day[day_key] = max_metrics

        rows = _wellness_rows(
            days,
            daily_summaries,
            hrv_by_day,
            max_metrics_by_day,
        )
        if rows:
            updated_at = utc_now()
            with transaction(connection):
                for row in rows:
                    result = connection.execute(
                        """
                        INSERT INTO wellness (
                            wellness_date,
                            resting_hr,
                            sleep_seconds,
                            body_battery_high,
                            body_battery_low,
                            stress_avg,
                            hrv_last_night_ms,
                            vo2max,
                            raw_json,
                            created_at,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (wellness_date) DO UPDATE SET
                            resting_hr = excluded.resting_hr,
                            sleep_seconds = excluded.sleep_seconds,
                            body_battery_high = excluded.body_battery_high,
                            body_battery_low = excluded.body_battery_low,
                            stress_avg = excluded.stress_avg,
                            hrv_last_night_ms = excluded.hrv_last_night_ms,
                            vo2max = excluded.vo2max,
                            raw_json = excluded.raw_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            row.wellness_date,
                            row.resting_hr,
                            row.sleep_seconds,
                            row.body_battery_high,
                            row.body_battery_low,
                            row.stress_avg,
                            row.hrv_last_night_ms,
                            row.vo2max,
                            row.raw_json,
                            updated_at,
                            updated_at,
                        ),
                    )
                    written += result.rowcount
        current = window_end + timedelta(days=1)

    return _WellnessPhase(
        start=wellness_start.isoformat(),
        end=today.isoformat(),
        written=written,
    )


def pull_garmin(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    zone: ZoneInfo,
    *,
    factory: GarminFactory = default_garmin_factory,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    now: datetime | None = None,
    history_start: str | None = None,
) -> GarminPullResult:
    """Pull activities and wellness data through one locked Garmin session."""
    current_time = datetime.now(UTC) if now is None else now
    today = date.fromisoformat(training_date(current_time, zone))
    history_start_date = _history_start_date(history_start)
    if history_start_date is not None and history_start_date > today:
        raise GarminConfigurationError(_HISTORY_START_ERROR)
    normalized_history_start = (
        None if history_start_date is None else history_start_date.isoformat()
    )

    with garmin_session(
        auth_config,
        lock_timeout=lock_timeout,
        factory=factory,
    ) as session:
        activities = _pull_activities(
            session.garmin,
            connection,
            zone,
            today,
            normalized_history_start,
        )
        _LOGGER.info(
            "activities %s..%s: added %d, skipped %d, invalid %d",
            activities.start,
            activities.end,
            activities.added,
            activities.skipped,
            activities.invalid,
        )
        wellness = _pull_wellness(
            session.garmin,
            connection,
            today,
            normalized_history_start,
        )
        _LOGGER.info(
            "wellness %s..%s: wrote %d days",
            wellness.start,
            wellness.end,
            wellness.written,
        )
        return GarminPullResult(
            login_reason=session.login_reason,
            activity_start=activities.start,
            activity_end=activities.end,
            activities_added=activities.added,
            activities_skipped=activities.skipped,
            activities_invalid=activities.invalid,
            wellness_start=wellness.start,
            wellness_end=wellness.end,
            wellness_days_written=wellness.written,
        )


def _print_usage() -> None:
    sys.stderr.write("Usage: training-season-pull [--help]\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the manual Garmin pull command."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--help"], ["-h"]):
        _print_usage()
        return 0
    if arguments:
        _print_usage()
        return 2

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        initialize_database()
        auth_config = garmin_auth_config_from_environment()
        zone = training_zone_from_environment()
        history_start = history_start_from_environment()
        path = database_path_from_environment()
        with closing(connect(path)) as connection:
            result = pull_garmin(
                connection,
                auth_config,
                zone,
                history_start=history_start,
            )
        sys.stdout.write(
            f"activities {result.activity_start}..{result.activity_end}: "
            f"added {result.activities_added}, skipped {result.activities_skipped}, "
            f"invalid {result.activities_invalid}; wellness "
            f"{result.wellness_start}..{result.wellness_end}: "
            f"{result.wellness_days_written} days\n"
        )
    except GarminMfaRequiredError as error:
        sys.stderr.write(f"{error}\n")
        return 3
    except (
        DatabaseConfigurationError,
        GarminConfigurationError,
        TrainingTimezoneError,
    ) as error:
        sys.stderr.write(f"{error}\n")
        return 2
    except (GarminError, MigrationError, sqlite3.Error) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
