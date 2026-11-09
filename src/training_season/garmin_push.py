"""
Replace one week of this app's workouts on the Garmin Connect calendar.

`schedule_workout` adds one calendar entry and never overwrites the calendar,
so the push first removes the workouts it stored for that week. It never
touches calendar entries it did not create.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from garminconnect.exceptions import GarminConnectNotFoundError

from training_season.garmin_auth import (
    LOCK_TIMEOUT_SECONDS,
    GarminFactory,
    GarminRateLimitedError,
    GarminTokenExpiredError,
    GarminUnavailableError,
    default_garmin_factory,
    garmin_request_error,
    garmin_session,
)
from training_season.garmin_login import GARMIN_REQUEST_ERRORS, garmin_response_status
from training_season.garmin_workout_payload import running_workout
from training_season.push_result import PushFailure, PushResult, PushStep
from training_season.push_store import (
    active_macro_plan_id,
    delete_stored_workout,
    insert_pushed_workout,
    store_schedule_id,
    stored_workouts,
    upsert_weekly_plan,
)
from training_season.push_week import WEEK_DAYS
from training_season.workout_validation import bounded_line

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import date

    from garminconnect import Garmin

    from training_season.garmin_auth import GarminAuthConfig
    from training_season.push_store import StoredWorkout
    from training_season.push_week import PushEntry, PushWeek

WORKOUT_ID_KEY = "workoutId"
SCHEDULE_ID_KEY = "workoutScheduleId"
_UNAVAILABLE_REASON = "o Garmin não respondeu"
_REQUEST_REASONS: dict[type[Exception], str] = {
    GarminTokenExpiredError: "o Garmin recusou a sessão salva, faça login de novo",
    GarminRateLimitedError: "o Garmin limitou as requisições, tente mais tarde",
}
_MISSING_ID_REASONS = {
    WORKOUT_ID_KEY: "o Garmin não devolveu o id do treino",
    SCHEDULE_ID_KEY: "o Garmin não devolveu o id do agendamento",
}
_GARMIN_CALL_ERRORS = (*GARMIN_REQUEST_ERRORS, OSError)
_LOGGER = logging.getLogger(__name__)


class _GarminCallFailedError(Exception):
    def __init__(self, step: PushStep, reason: str) -> None:
        super().__init__(reason)
        self.step = step
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _PushRun:
    connection: sqlite3.Connection
    garmin: Garmin = field(repr=False)
    weekly_plan_id: int


def _request_reason(error: Exception) -> str:
    known_reason = _REQUEST_REASONS.get(type(garmin_request_error(error)))
    if known_reason is not None:
        return known_reason
    status = garmin_response_status(error)
    if status is None:
        return _UNAVAILABLE_REASON
    return f"o Garmin devolveu erro HTTP {status}"


def _garmin_call[Response](step: PushStep, call: Callable[[], Response]) -> Response:
    try:
        return call()
    except _GARMIN_CALL_ERRORS as error:
        raise _GarminCallFailedError(step, _request_reason(error)) from error


def _call_unless_gone(call: Callable[[], object]) -> None:
    try:
        _garmin_call(PushStep.REMOVE, call)
    except _GarminCallFailedError as failed:
        if not isinstance(failed.__cause__, GarminConnectNotFoundError):
            raise
        _LOGGER.info("garmin item already gone during removal")


def _response_id(step: PushStep, response: object, key: str) -> str:
    value = response.get(key) if isinstance(response, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _GarminCallFailedError(step, _MISSING_ID_REASONS[key])
    return str(value)


def _remove_from_garmin(garmin: Garmin, stored: StoredWorkout) -> None:
    schedule_id = stored.garmin_schedule_id
    if schedule_id is not None:
        _call_unless_gone(lambda: garmin.unschedule_workout(schedule_id))
    _call_unless_gone(lambda: garmin.delete_workout(stored.garmin_workout_id))


def _remove_stored_week(run: _PushRun, week: PushWeek) -> PushFailure | None:
    for stored in stored_workouts(run.connection, week.week_start, week.week_end):
        try:
            _remove_from_garmin(run.garmin, stored)
        except _GarminCallFailedError as failed:
            _LOGGER.warning("removal failed for %s", stored.schedule_date.isoformat())
            return PushFailure(stored.schedule_date, failed.step, failed.reason)
        delete_stored_workout(run.connection, stored.row_id)
        _LOGGER.info("removed workout %s", stored.garmin_workout_id)
    return None


def _upload_and_schedule(run: _PushRun, entry: PushEntry) -> None:
    uploaded = _garmin_call(
        PushStep.UPLOAD,
        lambda: run.garmin.upload_running_workout(running_workout(entry.workout)),
    )
    workout_id = _response_id(PushStep.UPLOAD, uploaded, WORKOUT_ID_KEY)
    row_id = insert_pushed_workout(
        run.connection, run.weekly_plan_id, entry.date, entry.workout.name, workout_id
    )
    scheduled = _garmin_call(
        PushStep.SCHEDULE,
        lambda: run.garmin.schedule_workout(workout_id, entry.date.isoformat()),
    )
    schedule_id = _response_id(PushStep.SCHEDULE, scheduled, SCHEDULE_ID_KEY)
    store_schedule_id(run.connection, row_id, schedule_id)
    _LOGGER.info(
        "pushed %s: workout %s, schedule %s",
        entry.date.isoformat(),
        workout_id,
        schedule_id,
    )


def _push_entry(run: _PushRun, entry: PushEntry) -> PushFailure | None:
    try:
        _upload_and_schedule(run, entry)
    except _GarminCallFailedError as failed:
        _LOGGER.warning("%s failed for %s", failed.step, entry.date.isoformat())
        return PushFailure(entry.date, failed.step, failed.reason)
    return None


def _push_entries(run: _PushRun, week: PushWeek) -> PushResult:
    entries = week.ordered_entries()
    landed: list[date] = []
    for position, entry in enumerate(entries):
        failure = _push_entry(run, entry)
        if failure is not None:
            not_tried = tuple(later.date for later in entries[position + 1 :])
            return PushResult(week.week_start, tuple(landed), failure, not_tried)
        landed.append(entry.date)
    return PushResult(week.week_start, tuple(landed), None, ())


def push_week(
    connection: sqlite3.Connection,
    garmin: Garmin,
    weekly_plan_id: int,
    week: PushWeek,
) -> PushResult:
    """
    Remove this app's stored workouts on the 7 days, then push the new ones.

    Stops at the first failed Garmin call. Each Garmin change commits its row
    change at once, so the database always matches what Garmin holds.

    Used as `push_week(connection, session.garmin, weekly_plan_id, week)`
    """
    run = _PushRun(connection, garmin, weekly_plan_id)
    removal_failure = _remove_stored_week(run, week)
    if removal_failure is not None:
        not_tried = tuple(entry.date for entry in week.ordered_entries())
        return PushResult(week.week_start, (), removal_failure, not_tried)
    result = _push_entries(run, week)
    _LOGGER.info(
        "week %s: %d landed, %d not attempted",
        week.week_start.isoformat(),
        len(result.landed),
        len(result.not_attempted),
    )
    return result


def push_garmin_week(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    week: PushWeek,
    *,
    factory: GarminFactory = default_garmin_factory,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> PushResult:
    """
    Store the week under the active macro plan and push it in one locked session.

    Used as `push_garmin_week(connection, garmin_auth_config_from_environment(), week)`
    """
    macro_plan_id = active_macro_plan_id(connection)
    with garmin_session(
        auth_config, lock_timeout=lock_timeout, factory=factory
    ) as session:
        weekly_plan_id = upsert_weekly_plan(connection, macro_plan_id, week)
        return push_week(connection, session.garmin, weekly_plan_id, week)


def _entry_line(entry: object) -> tuple[str, str] | None:
    if not isinstance(entry, dict):
        return None
    workout = entry.get("workout")
    name = workout.get("workoutName") if isinstance(workout, dict) else None
    day = entry.get("calendarDate")
    if not isinstance(day, str):
        return None
    return bounded_line(day), bounded_line(name) if isinstance(name, str) else "?"


def _scheduled_entries(garmin: Garmin, week_start: date) -> list[object]:
    week_end = week_start + timedelta(days=WEEK_DAYS - 1)
    try:
        entries: object = garmin.get_scheduled_workouts_by_period(
            week_start.isoformat(), week_end.isoformat()
        )
    except OSError as error:
        raise garmin_request_error(error) from error
    # garminconnect 0.3.20 turns a 204 response into `{}` (client/transport.py).
    if entries == {}:
        return []
    if not isinstance(entries, list):
        raise GarminUnavailableError(
            f"Garmin returned a calendar of type {type(entries).__name__}, "
            "expected a list of entries"
        )
    return entries


def scheduled_week_lines(garmin: Garmin, week_start: date) -> tuple[str, ...]:
    """
    Read the calendar entries of the 7 days with one call, as `date name` lines.

    Used as `scheduled_week_lines(session.garmin, date(2027, 1, 4))`
    """
    found = [_entry_line(entry) for entry in _scheduled_entries(garmin, week_start)]
    return tuple(f"{day} {name}" for day, name in sorted(filter(None, found)))


def show_garmin_week(
    auth_config: GarminAuthConfig,
    week_start: date,
    *,
    factory: GarminFactory = default_garmin_factory,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """
    Read back one week of the Garmin calendar in one locked session.

    Used as `show_garmin_week(garmin_auth_config_from_environment(), date(2027, 1, 4))`
    """
    with garmin_session(
        auth_config, lock_timeout=lock_timeout, factory=factory
    ) as session:
        return scheduled_week_lines(session.garmin, week_start)
