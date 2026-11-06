"""Fake Garmin data and pull helpers shared by the Garmin pull tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from garminconnect.exceptions import GarminConnectConnectionError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from garminconnect import Garmin

    from training_season.garmin_auth import GarminAuthConfig

from garmin_auth_fakes import FakeGarmin, FakeGarminServer, _write_fake_token
from training_season.garmin_pull import GarminPullResult, pull_garmin

_ZONE = ZoneInfo("America/Sao_Paulo")
_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
_TODAY = "2026-10-27"
_SEED_TIMESTAMP = "1970-01-01T00:00:00Z"


def _activity_types() -> list[dict[str, Any]]:
    return [
        {"typeId": 1, "typeKey": "running", "parentTypeId": None},
        {"typeId": 2, "typeKey": "swimming", "parentTypeId": None},
        {"typeId": 3, "typeKey": "treadmill_running", "parentTypeId": 1},
        {"typeId": 4, "typeKey": "trail_running", "parentTypeId": 3},
        {"typeId": 5, "typeKey": "lap_swimming", "parentTypeId": 2},
        {"typeId": 6, "typeKey": "open_water_swimming", "parentTypeId": 5},
        {"typeId": 7, "typeKey": "cycling", "parentTypeId": None},
        {"typeId": 8, "typeKey": "strength_training", "parentTypeId": None},
        {"typeId": 9, "typeKey": "walking", "parentTypeId": None},
        {"typeId": 10, "typeKey": "multi_sport", "parentTypeId": None},
    ]


def _activity(
    activity_id: int,
    start_time: str = "2026-10-24 08:00:00",
    type_key: str = "running",
    *,
    type_id: int = 1,
    parent_type_id: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "activityId": activity_id,
        "activityName": f"Fixture activity {activity_id}",
        "startTimeGMT": start_time,
        "activityType": {
            "typeId": type_id,
            "typeKey": type_key,
            "parentTypeId": parent_type_id,
        },
        "distance": 5000.0,
        "duration": 1500.0,
        "averageHR": 150,
        "maxHR": 165,
        "averageSpeed": 3.0,
        "averageRunningCadenceInStepsPerMinute": 170.0,
    }
    result.update(fields)
    return result


def _daily_summary(day: str, **fields: Any) -> dict[str, Any]:
    return {"calendarDate": day, **fields}


@dataclass
class FakeGarminData:
    activities: list[Any] = field(default_factory=list)
    activity_count_response: object | None = None
    activities_by_date_response: object | None = None
    oldest_activity: Any = None
    activity_types: Any = field(default_factory=_activity_types)
    activity_laps: dict[str, Any] = field(default_factory=dict)
    daily_summaries: list[Any] = field(default_factory=list)
    daily_summaries_response: object | None = None
    hrv_by_day: dict[str, Any] = field(default_factory=dict)
    max_metrics_by_day: dict[str, Any] = field(default_factory=dict)
    errors: dict[tuple[str, int], BaseException] = field(default_factory=dict)
    callbacks: dict[str, Callable[[], None]] = field(default_factory=dict)


class PullFakeGarmin(FakeGarmin):
    """Fake authenticated Garmin client with scripted pull responses."""

    def __init__(
        self,
        server: FakeGarminServer,
        email: str | None,
        password: str | None,
        data: FakeGarminData,
    ) -> None:
        super().__init__(server, email, password)
        self.data = data
        self.calls: dict[str, list[tuple[object, ...]]] = {}
        self.ordered_calls: list[tuple[str, tuple[object, ...]]] = []

    def _record(self, method: str, args: tuple[object, ...]) -> None:
        self.ordered_calls.append((method, args))
        calls = self.calls.setdefault(method, [])
        call_index = len(calls)
        calls.append(args)
        callback = self.data.callbacks.get(method)
        if callback is not None:
            callback()
        error = self.data.errors.get((method, call_index))
        if error is not None:
            raise error

    def count_activities(self) -> int:
        self._record("count_activities", ())
        if self.data.activity_count_response is not None:
            return cast("int", self.data.activity_count_response)
        return len(self.data.activities)

    def get_activities(
        self,
        start: int = 0,
        limit: int = 20,
        activitytype: str | None = None,
        activitysubtype: str | None = None,
    ) -> dict[str, Any] | list[Any]:
        self._record(
            "get_activities",
            (start, limit, activitytype, activitysubtype),
        )
        return [] if self.data.oldest_activity is None else [self.data.oldest_activity]

    # This fake does not filter by window or offset; tests inspect recorded arguments.
    def get_activities_by_date(
        self,
        startdate: str,
        enddate: str | None = None,
        activitytype: str | None = None,
        sortorder: str | None = None,
    ) -> list[dict[str, Any]]:
        self._record(
            "get_activities_by_date",
            (startdate, enddate, activitytype, sortorder),
        )
        if self.data.activities_by_date_response is not None:
            return cast(
                "list[dict[str, Any]]",
                self.data.activities_by_date_response,
            )
        return cast("list[dict[str, Any]]", self.data.activities)

    def get_activity_types(self) -> dict[str, Any]:
        self._record("get_activity_types", ())
        return cast("dict[str, Any]", self.data.activity_types)

    def get_activity_laps(self, activity_id: int | str) -> dict[str, Any]:
        self._record("get_activity_laps", (activity_id,))
        return cast(
            "dict[str, Any]",
            self.data.activity_laps.get(str(activity_id), {"lapDTOs": []}),
        )

    def get_daily_summaries(self, start: str, end: str) -> dict[str, Any]:
        self._record("get_daily_summaries", (start, end))
        if self.data.daily_summaries_response is not None:
            return cast("dict[str, Any]", self.data.daily_summaries_response)
        summaries = [
            item
            for item in self.data.daily_summaries
            if isinstance(item, dict)
            and isinstance(item.get("calendarDate"), str)
            and start <= item["calendarDate"] <= end
        ]
        return {"data": summaries}

    def get_hrv_data(self, cdate: str) -> dict[str, Any] | None:
        self._record("get_hrv_data", (cdate,))
        return self.data.hrv_by_day.get(cdate)

    def get_max_metrics(self, cdate: str) -> dict[str, Any]:
        self._record("get_max_metrics", (cdate,))
        return cast("dict[str, Any]", self.data.max_metrics_by_day.get(cdate, {}))


def _factory_for(
    fake_server: FakeGarminServer,
    data: FakeGarminData,
) -> tuple[Callable[[str | None, str | None], Garmin], list[PullFakeGarmin]]:
    clients: list[PullFakeGarmin] = []

    def create_garmin(email: str | None, password: str | None) -> PullFakeGarmin:
        client = PullFakeGarmin(fake_server, email, password, data)
        clients.append(client)
        return client

    return create_garmin, clients


def _run_pull(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    data: FakeGarminData,
    *,
    now: datetime = _NOW,
    history_start: str | None = None,
    lock_timeout: float = 10.0,
) -> tuple[GarminPullResult, list[PullFakeGarmin]]:
    _write_fake_token(auth_config)
    factory, clients = _factory_for(fake_server, data)
    result = pull_garmin(
        connection,
        auth_config,
        _ZONE,
        factory=factory,
        lock_timeout=lock_timeout,
        now=now,
        history_start=history_start,
    )
    return result, clients


def _seed_activity(
    connection: sqlite3.Connection,
    activity_id: str,
    activity_date: str,
) -> None:
    connection.execute(
        """
        INSERT INTO activity (
            garmin_activity_id, sport, activity_type, start_time, activity_date,
            raw_json, created_at
        ) VALUES (?, 'running', 'running', ?, ?, '{}', ?)
        """,
        (
            activity_id,
            f"{activity_date}T12:00:00Z",
            activity_date,
            _SEED_TIMESTAMP,
        ),
    )


def _seed_wellness(
    connection: sqlite3.Connection,
    wellness_date: str,
    *,
    resting_hr: float = 50.0,
    created_at: str = _SEED_TIMESTAMP,
) -> None:
    connection.execute(
        """
        INSERT INTO wellness (
            wellness_date, resting_hr, raw_json, created_at, updated_at
        ) VALUES (?, ?, '{}', ?, ?)
        """,
        (wellness_date, resting_hr, created_at, created_at),
    )


def _activity_rows(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return list(
        connection.execute(
            """
            SELECT garmin_activity_id, sport, activity_type, start_time, activity_date,
                   distance_m, duration_s, avg_hr, max_hr, avg_speed_mps,
                   avg_cadence_spm, splits_json, raw_json, created_at
            FROM activity
            ORDER BY garmin_activity_id
            """
        )
    )


def _connection_error(status_code: int) -> GarminConnectConnectionError:
    error = GarminConnectConnectionError("simulated connection failure")
    error.response = type("Response", (), {"status_code": status_code})()
    return error
