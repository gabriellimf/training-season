"""Fake Garmin calendar, week builders and push helpers shared by the push tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from garmin_auth_fakes import FakeGarmin, FakeGarminServer, _write_fake_token
from schema_rows import _insert_macro
from training_season.garmin_push import push_garmin_week
from training_season.push_week import push_week_from_text

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from garminconnect import Garmin
    from garminconnect.workout import RunningWorkout

    from training_season.garmin_auth import GarminAuthConfig
    from training_season.push_result import PushResult

WEEK_START = "2027-01-04"
PRIVATE_NOTE = "private-step-note"
PRIVATE_PACE = 2.7713


def dated_workout(day: str, content: str = "Leve") -> dict[str, object]:
    return {
        "name": f"{day[8:10]}/{day[5:7]} {content}",
        "steps": [
            {
                "type": "warmup",
                "duration": {"meters": 1000},
                "pace_mps": {"min": PRIVATE_PACE, "max": 2.9},
                "notes": PRIVATE_NOTE,
            }
        ],
    }


def week_text(days: list[str], week_start: str = WEEK_START) -> str:
    workouts = [{"date": day, "workout": dated_workout(day)} for day in days]
    return json.dumps({"week_start": week_start, "workouts": workouts})


@dataclass
class FakeGarminCalendar:
    next_id: int = 1000
    upload_responses: dict[int, object] = field(default_factory=dict)
    schedule_responses: dict[int, object] = field(default_factory=dict)
    by_period_entries: object = field(default_factory=list)
    errors: dict[tuple[str, int], BaseException] = field(default_factory=dict)

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id


class PushFakeGarmin(FakeGarmin):
    """Fake authenticated Garmin client that records calendar writes in order."""

    def __init__(
        self,
        server: FakeGarminServer,
        email: str | None,
        password: str | None,
        calendar: FakeGarminCalendar,
    ) -> None:
        super().__init__(server, email, password)
        self.calendar = calendar
        self.calls: dict[str, list[tuple[object, ...]]] = {}
        self.ordered_calls: list[tuple[str, tuple[object, ...]]] = []

    def _record(self, method: str, args: tuple[object, ...]) -> int:
        self.ordered_calls.append((method, args))
        calls = self.calls.setdefault(method, [])
        call_index = len(calls)
        calls.append(args)
        error = self.calendar.errors.get((method, call_index))
        if error is not None:
            raise error
        return call_index

    def upload_running_workout(self, workout: RunningWorkout) -> dict[str, Any]:
        call_index = self._record("upload_running_workout", (workout.workoutName,))
        default = {"workoutId": self.calendar.new_id()}
        response = self.calendar.upload_responses.get(call_index, default)
        return cast("dict[str, Any]", response)

    def schedule_workout(self, workout_id: int | str, date_str: str) -> dict[str, Any]:
        call_index = self._record("schedule_workout", (workout_id, date_str))
        default = {"workoutScheduleId": self.calendar.new_id()}
        response = self.calendar.schedule_responses.get(call_index, default)
        return cast("dict[str, Any]", response)

    def unschedule_workout(self, scheduled_workout_id: int | str) -> None:
        self._record("unschedule_workout", (scheduled_workout_id,))

    def delete_workout(self, workout_id: int | str) -> None:
        self._record("delete_workout", (workout_id,))

    def get_scheduled_workouts_by_period(
        self, start: str, end: str
    ) -> list[dict[str, Any]]:
        self._record("get_scheduled_workouts_by_period", (start, end))
        return cast("list[dict[str, Any]]", self.calendar.by_period_entries)


def push_factory_for(
    fake_server: FakeGarminServer,
    calendar: FakeGarminCalendar,
) -> tuple[Callable[[str | None, str | None], Garmin], list[PushFakeGarmin]]:
    clients: list[PushFakeGarmin] = []

    def create_garmin(email: str | None, password: str | None) -> PushFakeGarmin:
        client = PushFakeGarmin(fake_server, email, password, calendar)
        clients.append(client)
        return client

    return create_garmin, clients


def run_push(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    calendar: FakeGarminCalendar,
    text: str,
) -> tuple[PushResult, PushFakeGarmin]:
    _write_fake_token(auth_config)
    factory, clients = push_factory_for(fake_server, calendar)
    result = push_garmin_week(
        connection,
        auth_config,
        push_week_from_text(text),
        factory=factory,
        lock_timeout=10.0,
    )
    return result, clients[-1]


def seed_active_macro(connection: sqlite3.Connection) -> int:
    return _insert_macro(connection, start_date="2026-12-28", race_date="2027-06-01")


def workout_rows(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return list(
        connection.execute(
            """
            SELECT weekly_plan.week_start, workout.schedule_date, workout.name,
                   workout.garmin_workout_id, workout.garmin_schedule_id
            FROM workout JOIN weekly_plan ON weekly_plan.id = workout.weekly_plan_id
            ORDER BY workout.schedule_date
            """
        )
    )
