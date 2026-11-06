"""Tests for the Garmin history pull."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from garminconnect import Garmin

from test_garmin_auth import (
    FakeGarmin,
    FakeGarminServer,
    _chain_rejected_token_error,
    _write_fake_token,
)
from training_season import garmin_activities, garmin_pull, garmin_wellness
from training_season.garmin_activities import (
    _MAX_ACTIVITY_TYPE_DEPTH,
    _build_type_roots,
    _resolve_sport,
)
from training_season.garmin_auth import (
    GarminAuthConfig,
    GarminConfigurationError,
    GarminError,
    GarminLockTimeoutError,
    GarminLoginReason,
    GarminMfaRequiredError,
    GarminRateLimitedError,
    GarminTokenExpiredError,
    GarminUnavailableError,
    start_login,
)
from training_season.garmin_pull import (
    GarminPullResult,
    history_start_from_environment,
    pull_garmin,
)
from training_season.garmin_pull_base import non_negative
from training_season.garmin_wellness import _vo2max_from

_ZONE = ZoneInfo("America/Sao_Paulo")
_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
_TODAY = "2026-10-27"
_HISTORY_START_MESSAGE = (
    "GARMIN_HISTORY_START must be a valid YYYY-MM-DD date no later than today"
)
_SEED_TIMESTAMP = "1970-01-01T00:00:00Z"
pytest_plugins = ("test_garmin_auth",)


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


def _wellness_row(
    connection: sqlite3.Connection,
    wellness_date: str,
) -> tuple[Any, ...] | None:
    row = connection.execute(
        """
        SELECT resting_hr, sleep_seconds, body_battery_high, body_battery_low,
               stress_avg, hrv_last_night_ms, vo2max, raw_json, created_at, updated_at
        FROM wellness
        WHERE wellness_date = ?
        """,
        (wellness_date,),
    ).fetchone()
    return cast("tuple[Any, ...] | None", row)


def test_first_run_lists_from_oldest_activity_with_margin(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    oldest = _activity(10, "2026-10-20 08:00:00")
    data = FakeGarminData(
        activities=[oldest, _activity(11, "2026-10-24 08:00:00")],
        oldest_activity=oldest,
    )

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
    )

    assert result.activity_start == "2026-10-19"
    assert result.activity_end == "2026-10-28"
    assert clients[0].calls["count_activities"] == [()]
    assert clients[0].calls["get_activities"] == [(1, 1, None, None)]
    assert clients[0].calls["get_activities_by_date"] == [
        ("2026-10-19", "2026-10-28", None, "asc")
    ]


def test_first_run_stores_running_and_swimming_activities(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[
            _activity(10, "2026-10-24 08:00:00"),
            _activity(11, "2026-10-25 08:00:00", "swimming", type_id=2),
        ]
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    assert result.activities_added == 2
    assert migrated_connection.execute(
        "SELECT sport FROM activity ORDER BY garmin_activity_id"
    ).fetchall() == [("running",), ("swimming",)]


def test_child_types_resolve_to_sport_via_parent_chain(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[
            _activity(
                1,
                type_key="treadmill_running",
                type_id=3,
                parent_type_id=1,
            ),
            _activity(
                2,
                type_key="trail_running",
                type_id=4,
                parent_type_id=3,
            ),
            _activity(
                3,
                type_key="lap_swimming",
                type_id=5,
                parent_type_id=2,
            ),
            _activity(
                4,
                type_key="open_water_swimming",
                type_id=6,
                parent_type_id=5,
            ),
        ]
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    assert result.activities_added == 4
    assert migrated_connection.execute(
        "SELECT sport FROM activity ORDER BY garmin_activity_id"
    ).fetchall() == [
        ("running",),
        ("running",),
        ("swimming",),
        ("swimming",),
    ]


def test_child_type_without_parent_uses_type_map() -> None:
    type_roots = _build_type_roots(_activity_types())
    activity_type: dict[str, Any] = {
        "typeId": 3,
        "typeKey": "treadmill_running",
    }

    assert _resolve_sport(activity_type, type_roots) == "running"


@pytest.mark.parametrize("parent_type_id", ["invalid", 999])
def test_child_type_with_invalid_parent_uses_type_map(parent_type_id: object) -> None:
    type_roots = _build_type_roots(_activity_types())
    activity_type: dict[str, Any] = {
        "typeId": 3,
        "typeKey": "treadmill_running",
        "parentTypeId": parent_type_id,
    }

    assert _resolve_sport(activity_type, type_roots) == "running"


def test_unknown_type_without_parent_is_skipped() -> None:
    type_roots = _build_type_roots(_activity_types())
    activity_type: dict[str, Any] = {
        "typeId": 999,
        "typeKey": "unknown_activity",
    }

    assert _resolve_sport(activity_type, type_roots) is None


def test_parent_cycle_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(garmin_activities, "_MAX_ACTIVITY_TYPE_DEPTH", 1000)

    class CountingTypeRoots(Mapping[int, tuple[str, int | None]]):
        def __init__(self) -> None:
            self.values_by_id = {
                1: ("custom_a", 2),
                2: ("custom_b", 1),
            }
            self.lookups = 0

        def __getitem__(self, key: int) -> tuple[str, int | None]:
            self.lookups += 1
            return self.values_by_id[key]

        def __iter__(self) -> Iterator[int]:
            return iter(self.values_by_id)

        def __len__(self) -> int:
            return len(self.values_by_id)

        def __contains__(self, key: object) -> bool:
            return key in self.values_by_id

    type_roots = CountingTypeRoots()
    activity_type: dict[str, Any] = {
        "typeId": 99,
        "typeKey": "custom",
        "parentTypeId": 1,
    }

    assert _resolve_sport(activity_type, type_roots) is None
    assert type_roots.lookups == 2


def test_parent_chain_at_hop_limit_resolves() -> None:
    type_roots: dict[int, tuple[str, int | None]] = {
        type_id: (f"custom_{type_id}", type_id + 1)
        for type_id in range(1, _MAX_ACTIVITY_TYPE_DEPTH)
    }
    type_roots[_MAX_ACTIVITY_TYPE_DEPTH] = ("running", None)
    activity_type: dict[str, Any] = {
        "typeId": 0,
        "typeKey": "custom",
        "parentTypeId": 1,
    }

    assert _resolve_sport(activity_type, type_roots) == "running"


def test_parent_chain_beyond_hop_limit_is_skipped() -> None:
    type_roots: dict[int, tuple[str, int | None]] = {
        type_id: (f"custom_{type_id}", type_id + 1)
        for type_id in range(1, _MAX_ACTIVITY_TYPE_DEPTH + 1)
    }
    type_roots[_MAX_ACTIVITY_TYPE_DEPTH + 1] = ("running", None)
    activity_type: dict[str, Any] = {
        "typeId": 0,
        "typeKey": "custom",
        "parentTypeId": 1,
    }

    assert _resolve_sport(activity_type, type_roots) is None


def test_other_sports_are_skipped_and_counted(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[
            _activity(1, type_key="cycling", type_id=7),
            _activity(2, type_key="strength_training", type_id=8),
            _activity(3, type_key="walking", type_id=9),
            _activity(4, type_key="multi_sport", type_id=10),
            _activity(
                5,
                type_key="unresolved_activity",
                type_id=100,
                parent_type_id=999,
            ),
        ]
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    assert result.activities_skipped == 5
    assert result.activities_invalid == 0
    assert migrated_connection.execute("SELECT count(*) FROM activity").fetchone() == (
        0,
    )


def test_activity_columns_map_from_list_item(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    activity = _activity(1)
    laps = {"lapDTOs": [{"distance": 500.0}]}
    data = FakeGarminData(
        activities=[activity],
        activity_laps={"1": laps},
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    stored = _activity_rows(migrated_connection)[0]
    assert result.activities_added == 1
    assert stored[:11] == (
        "1",
        "running",
        "running",
        "2026-10-24T08:00:00Z",
        "2026-10-24",
        5000.0,
        1500.0,
        150.0,
        165.0,
        3.0,
        170.0,
    )
    assert json.loads(stored[11]) == laps
    assert json.loads(stored[12]) == activity


def test_activity_date_uses_training_zone_across_midnight(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[_activity(1, "2026-10-27 02:30:00")],
    )

    _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-26",
    )

    stored = _activity_rows(migrated_connection)[0]
    assert stored[3:5] == ("2026-10-27T02:30:00Z", "2026-10-26")


def test_start_time_with_offset_is_converted_to_utc(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[_activity(1, "2026-10-27T02:30:00+02:00")],
    )

    _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-26",
    )

    stored = _activity_rows(migrated_connection)[0]
    assert stored[3:5] == ("2026-10-27T00:30:00Z", "2026-10-26")


def test_invalid_activity_items_are_counted_not_stored(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    invalid_items: list[Any] = [
        None,
        "not-an-object",
        {"startTimeGMT": "2026-10-24 08:00:00"},
        _activity(3, "not-a-time"),
        {
            "activityId": 4,
            "startTimeGMT": "2026-10-24 08:00:00",
            "activityType": {},
        },
        _activity(5, extra=object()),
    ]
    data = FakeGarminData(activities=invalid_items)

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    assert result.activities_invalid == len(invalid_items)
    assert migrated_connection.execute("SELECT count(*) FROM activity").fetchone() == (
        0,
    )


def test_bad_numeric_values_store_null(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[
            _activity(
                1,
                distance=-1,
                duration=True,
                averageHR=-1,
                maxHR="unknown",
                averageSpeed="unknown",
                averageRunningCadenceInStepsPerMinute=-2,
            )
        ]
    )

    _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    stored = _activity_rows(migrated_connection)[0]
    assert stored[5:11] == (None, None, None, None, None, None)
    assert non_negative(float("nan")) is None
    assert non_negative(float("inf")) is None


def test_laps_not_found_stores_null_splits(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[_activity(1)],
        errors={
            ("get_activity_laps", 0): GarminConnectNotFoundError("missing resource")
        },
    )

    _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    assert _activity_rows(migrated_connection)[0][11] is None


@pytest.mark.parametrize(
    ("scenario", "expected_message"),
    [
        (
            "activity_type_list",
            "Garmin returned an unexpected activity type list",
        ),
        (
            "activity_count_string",
            "Garmin returned an unexpected activity count",
        ),
        (
            "activity_count_negative",
            "Garmin returned an unexpected activity count",
        ),
        (
            "activity_count_boolean",
            "Garmin returned an unexpected activity count",
        ),
        (
            "oldest_empty_page",
            "Garmin returned an unexpected oldest activity",
        ),
        (
            "oldest_non_dict",
            "Garmin returned an unexpected oldest activity",
        ),
        (
            "oldest_unparseable",
            "Garmin returned an unexpected oldest activity",
        ),
        (
            "activities_not_list",
            "Garmin returned an unexpected activity list",
        ),
        (
            "activity_laps_not_serializable",
            "Garmin returned unexpected activity laps",
        ),
        (
            "daily_summaries_bad_shape",
            "Garmin returned an unexpected daily summary list",
        ),
        (
            "wellness_not_serializable",
            "Garmin returned unexpected wellness data",
        ),
    ],
)
def test_fixed_message_garmin_errors_write_no_rows(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    scenario: str,
    expected_message: str,
) -> None:
    data = FakeGarminData()
    history_start: str | None = _TODAY
    if scenario == "activity_type_list":
        data.activity_types = {"activityTypes": _activity_types()}
    elif scenario.startswith("activity_count_"):
        history_start = None
        data.activities = [_activity(1)]
        if scenario == "activity_count_string":
            data.activity_count_response = "invalid"
        elif scenario == "activity_count_negative":
            data.activity_count_response = -1
        else:
            data.activity_count_response = True
    elif scenario.startswith("oldest_"):
        history_start = None
        data.activities = [_activity(1)]
        if scenario == "oldest_non_dict":
            data.oldest_activity = "not-an-object"
        elif scenario == "oldest_unparseable":
            data.oldest_activity = {"startTimeGMT": "not-a-time"}
    elif scenario == "activities_not_list":
        data.activities = [_activity(1)]
        data.activities_by_date_response = {"unexpected": "shape"}
    elif scenario == "activity_laps_not_serializable":
        data.activities = [
            _activity(1, start_time=f"{_TODAY} 08:00:00"),
        ]
        data.activity_laps = {"1": object()}
    elif scenario == "daily_summaries_bad_shape":
        data.daily_summaries_response = {"data": "not-a-list"}
    else:
        data.hrv_by_day = {_TODAY: object()}

    with pytest.raises(GarminUnavailableError) as raised:
        _run_pull(
            migrated_connection,
            auth_config,
            fake_server,
            data,
            history_start=history_start,
        )

    assert str(raised.value) == expected_message
    assert migrated_connection.execute("SELECT count(*) FROM activity").fetchone() == (
        0,
    )
    assert migrated_connection.execute("SELECT count(*) FROM wellness").fetchone() == (
        0,
    )


def test_first_run_without_activities_writes_no_activity(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData()

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
    )

    assert result.activities_added == 0
    assert clients[0].calls["count_activities"] == [()]
    assert "get_activities_by_date" not in clients[0].calls
    assert migrated_connection.execute("SELECT count(*) FROM activity").fetchone() == (
        0,
    )


def test_first_run_wellness_starts_at_earliest_activity_date(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    oldest = _activity(1, "2026-10-22 08:00:00")
    data = FakeGarminData(
        activities=[
            oldest,
            _activity(2, "2026-10-24 08:00:00"),
        ],
        oldest_activity=oldest,
    )

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
    )

    assert result.wellness_start == "2026-10-22"
    assert clients[0].calls["get_daily_summaries"][0][0] == "2026-10-22"


def test_wellness_row_merges_three_sources_into_raw_json(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    summary = _daily_summary(
        _TODAY,
        restingHeartRate=48,
        sleepingSeconds=28800,
        bodyBatteryHighestValue=84,
        bodyBatteryLowestValue=20,
        averageStressLevel=32,
    )
    hrv = {"hrvSummary": {"lastNightAvg": 55.5}}
    max_metrics = [{"generic": {"vo2MaxPreciseValue": 48.2}}]
    data = FakeGarminData(
        daily_summaries=[summary],
        hrv_by_day={_TODAY: hrv},
        max_metrics_by_day={_TODAY: max_metrics},
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start=_TODAY,
    )

    row = _wellness_row(migrated_connection, _TODAY)
    assert row is not None
    assert row[:7] == (48.0, 28800.0, 84.0, 20.0, 32.0, 55.5, 48.2)
    assert json.loads(row[7]) == {
        "daily_summary": summary,
        "hrv": hrv,
        "max_metrics": max_metrics,
    }
    assert result.wellness_days_written == 1


def test_wellness_windows_are_at_most_28_days(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    history_start = (date.fromisoformat(_TODAY) - timedelta(days=60)).isoformat()
    data = FakeGarminData()

    _, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start=history_start,
    )

    windows = clients[0].calls["get_daily_summaries"]
    assert windows == [
        ("2026-08-28", "2026-09-24"),
        ("2026-09-25", "2026-10-22"),
        ("2026-10-23", "2026-10-27"),
    ]
    expected_order: list[tuple[str, tuple[object, ...]]] = []
    for start, end in windows:
        expected_order.append(("get_daily_summaries", (start, end)))
        day = date.fromisoformat(cast("str", start))
        while day <= date.fromisoformat(cast("str", end)):
            expected_order.append(("get_hrv_data", (day.isoformat(),)))
            expected_order.append(("get_max_metrics", (day.isoformat(),)))
            day += timedelta(days=1)
    wellness_methods = {"get_daily_summaries", "get_hrv_data", "get_max_metrics"}
    assert [
        call for call in clients[0].ordered_calls if call[0] in wellness_methods
    ] == expected_order
    assert all(
        isinstance(start, str)
        and isinstance(end, str)
        and (date.fromisoformat(end) - date.fromisoformat(start)).days < 28
        for start, end in windows
    )


def test_hrv_or_max_metrics_missing_or_not_found_means_null(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        daily_summaries=[_daily_summary(_TODAY, restingHeartRate=50)],
        errors={
            ("get_hrv_data", 0): GarminConnectNotFoundError("missing HRV"),
            ("get_max_metrics", 0): GarminConnectNotFoundError("missing metrics"),
        },
    )

    _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start=_TODAY,
    )

    row = _wellness_row(migrated_connection, _TODAY)
    assert row is not None
    assert row[5:7] == (None, None)


def test_wellness_date_without_any_source_is_not_written(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData()

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start=_TODAY,
    )

    assert result.wellness_days_written == 0
    assert migrated_connection.execute("SELECT count(*) FROM wellness").fetchone() == (
        0,
    )


def test_second_run_lists_from_cursor_minus_overlap(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    _seed_activity(migrated_connection, "stored", "2026-10-22")
    data = FakeGarminData()

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-25",
    )

    assert result.activity_start == "2026-10-08"
    assert clients[0].calls["get_activities_by_date"][0][0] == "2026-10-08"
    assert "count_activities" not in clients[0].calls
    assert "get_activities" not in clients[0].calls


def test_second_run_adds_only_new_activities_and_skips_known_laps(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    _seed_activity(migrated_connection, "1", "2026-10-22")
    data = FakeGarminData(
        activities=[
            _activity(1, "2026-10-22 08:00:00"),
            _activity(2, "2026-10-24 08:00:00"),
        ]
    )

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
    )

    assert result.activities_added == 1
    assert clients[0].calls["get_activity_laps"] == [("2",)]
    assert migrated_connection.execute("SELECT count(*) FROM activity").fetchone() == (
        2,
    )


def test_late_synced_activity_inside_overlap_is_added(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    _seed_activity(migrated_connection, "1", "2026-10-22")
    data = FakeGarminData(
        activities=[_activity(2, "2026-10-15 08:00:00")],
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
    )

    assert result.activity_start == "2026-10-08"
    assert result.activities_added == 1
    assert migrated_connection.execute(
        "SELECT garmin_activity_id FROM activity ORDER BY garmin_activity_id"
    ).fetchall() == [("1",), ("2",)]


def test_wellness_overlap_updates_rows_and_keeps_created_at(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    old_created_at = _SEED_TIMESTAMP
    _seed_wellness(
        migrated_connection,
        "2026-10-22",
        resting_hr=60,
        created_at=old_created_at,
    )
    data = FakeGarminData(
        daily_summaries=[_daily_summary("2026-10-22", restingHeartRate=48)],
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
    )

    row = _wellness_row(migrated_connection, "2026-10-22")
    assert row is not None
    assert row[0] == 48.0
    assert row[8] == old_created_at
    assert row[9] != old_created_at
    assert result.wellness_days_written == 1


def test_repeat_run_is_idempotent(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[_activity(1, "2026-10-25 08:00:00")],
        daily_summaries=[_daily_summary("2026-10-25", restingHeartRate=50)],
    )

    first, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-25",
    )
    activity_rows = _activity_rows(migrated_connection)
    activity_count = migrated_connection.execute(
        "SELECT count(*) FROM activity"
    ).fetchone()
    wellness_count = migrated_connection.execute(
        "SELECT count(*) FROM wellness"
    ).fetchone()

    second, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-25",
    )

    assert first.activities_added == 1
    assert second.activities_added == 0
    assert _activity_rows(migrated_connection) == activity_rows
    assert migrated_connection.execute("SELECT count(*) FROM activity").fetchone() == (
        activity_count
    )
    assert migrated_connection.execute("SELECT count(*) FROM wellness").fetchone() == (
        wellness_count
    )


def _connection_error(status_code: int) -> GarminConnectConnectionError:
    error = GarminConnectConnectionError("simulated connection failure")
    error.response = type("Response", (), {"status_code": status_code})()
    return error


@pytest.mark.parametrize(
    ("api_error", "expected_error"),
    [
        (
            GarminConnectTooManyRequestsError("simulated rate limit"),
            GarminRateLimitedError,
        ),
        (_connection_error(503), GarminUnavailableError),
        (
            GarminConnectAuthenticationError("simulated authentication failure"),
            GarminTokenExpiredError,
        ),
    ],
)
def test_failure_mid_list_writes_nothing(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    api_error: BaseException,
    expected_error: type[GarminError],
) -> None:
    data = FakeGarminData(
        activities=[_activity(1)],
        errors={("get_activities_by_date", 0): api_error},
    )

    with pytest.raises(expected_error):
        _run_pull(
            migrated_connection,
            auth_config,
            fake_server,
            data,
            history_start="2026-10-24",
        )

    assert migrated_connection.execute("SELECT count(*) FROM activity").fetchone() == (
        0,
    )
    assert migrated_connection.execute("SELECT count(*) FROM wellness").fetchone() == (
        0,
    )


def test_failure_mid_laps_keeps_complete_prefix_and_rerun_resumes(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[
            _activity(1, "2026-10-24 08:00:00"),
            _activity(2, "2026-10-25 08:00:00"),
        ],
        errors={("get_activity_laps", 1): _connection_error(503)},
    )

    with pytest.raises(GarminUnavailableError):
        _run_pull(
            migrated_connection,
            auth_config,
            fake_server,
            data,
            history_start="2026-10-24",
        )

    assert migrated_connection.execute(
        "SELECT garmin_activity_id FROM activity"
    ).fetchall() == [("1",)]
    data.errors.clear()

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
    )

    assert result.activities_added == 1
    assert clients[0].calls["get_activity_laps"] == [("2",)]
    assert migrated_connection.execute("SELECT count(*) FROM activity").fetchone() == (
        2,
    )


def test_failure_mid_wellness_keeps_committed_windows_and_rerun_resumes(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    history_start = "2026-09-01"
    data = FakeGarminData(
        daily_summaries=[
            _daily_summary(history_start, restingHeartRate=50),
            _daily_summary("2026-09-29", restingHeartRate=51),
        ],
        errors={("get_max_metrics", 28): _connection_error(503)},
    )

    with pytest.raises(GarminUnavailableError):
        _run_pull(
            migrated_connection,
            auth_config,
            fake_server,
            data,
            history_start=history_start,
        )

    assert _wellness_row(migrated_connection, history_start) is not None
    assert _wellness_row(migrated_connection, "2026-09-29") is None
    data.errors.clear()

    _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start=history_start,
    )

    assert _wellness_row(migrated_connection, "2026-09-29") is not None


def test_commit_failure_rolls_back_only_current_unit(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    history_start = (date.fromisoformat(_TODAY) - timedelta(days=28)).isoformat()
    migrated_connection.execute(
        f"""
        CREATE TRIGGER reject_current_wellness
        BEFORE INSERT ON wellness
        WHEN NEW.wellness_date = '{_TODAY}'
        BEGIN
            SELECT RAISE(ABORT, 'test');
        END
        """
    )
    data = FakeGarminData(
        daily_summaries=[
            _daily_summary(history_start, restingHeartRate=50),
            _daily_summary(_TODAY, restingHeartRate=51),
        ],
    )

    with pytest.raises(sqlite3.IntegrityError):
        _run_pull(
            migrated_connection,
            auth_config,
            fake_server,
            data,
            history_start=history_start,
        )

    assert _wellness_row(migrated_connection, history_start) is not None
    assert _wellness_row(migrated_connection, _TODAY) is None
    assert not migrated_connection.in_transaction


def test_activity_commit_failure_rolls_back_current_unit_and_rerun_matches_clean(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_start = "2026-10-24"
    for module in (garmin_activities, garmin_wellness):
        monkeypatch.setattr(module, "utc_now", lambda: "2026-10-27T12:00:00Z")
    migrated_connection.execute(
        """
        CREATE TRIGGER reject_activity
        BEFORE INSERT ON activity
        WHEN NEW.garmin_activity_id = '2'
        BEGIN
            SELECT RAISE(ABORT, 'test');
        END
        """
    )
    data = FakeGarminData(
        activities=[
            _activity(1, "2026-10-24 08:00:00"),
            _activity(2, "2026-10-25 08:00:00"),
        ]
    )

    with pytest.raises(sqlite3.IntegrityError):
        _run_pull(
            migrated_connection,
            auth_config,
            fake_server,
            data,
            history_start=history_start,
        )

    partial_rows = _activity_rows(migrated_connection)
    assert [row[0] for row in partial_rows] == ["1"]
    assert partial_rows[0][11] is not None
    assert not migrated_connection.in_transaction

    migrated_connection.execute("DROP TRIGGER reject_activity")
    _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start=history_start,
    )
    recovered_rows = _activity_rows(migrated_connection)

    migrated_connection.execute("DELETE FROM activity")
    _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start=history_start,
    )
    clean_rows = _activity_rows(migrated_connection)

    assert recovered_rows == clean_rows


def test_errors_and_logs_contain_no_health_data(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    activity = _activity(
        1,
        type_key="private-type-key",
        type_id=100,
        parent_type_id=1,
        extraHealthValue=171.37,
        locationName="private-location",
    )
    activity["activityName"] = "private-activity-name"
    data = FakeGarminData(
        activities=[activity],
        errors={
            ("get_activity_laps", 0): GarminConnectConnectionError(
                "private health value 171.37"
            )
        },
    )
    caplog.set_level(logging.DEBUG)

    with pytest.raises(GarminUnavailableError) as raised:
        _run_pull(
            migrated_connection,
            auth_config,
            fake_server,
            data,
            history_start="2026-10-24",
        )

    assert "171.37" not in caplog.text
    assert "private-activity-name" not in caplog.text
    assert "private-type-key" not in caplog.text
    assert "private-location" not in caplog.text
    assert "171.37" not in str(raised.value)
    assert "private-activity-name" not in str(raised.value)
    assert "private-type-key" not in str(raised.value)
    assert "private-location" not in str(raised.value)


def test_pending_mfa_fails_before_any_data_call(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData()
    factory, clients = _factory_for(fake_server, data)
    fake_server.credential_outcomes.append("mfa")
    with pytest.raises(GarminMfaRequiredError):
        start_login(auth_config, factory=factory)

    _write_fake_token(auth_config)
    with pytest.raises(GarminMfaRequiredError):
        pull_garmin(
            migrated_connection,
            auth_config,
            _ZONE,
            factory=factory,
            now=_NOW,
            history_start=_TODAY,
        )

    assert all(not client.calls for client in clients)


def test_no_write_transaction_is_open_during_garmin_calls(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    method_names = (
        "count_activities",
        "get_activities",
        "get_activities_by_date",
        "get_activity_types",
        "get_activity_laps",
        "get_daily_summaries",
        "get_hrv_data",
        "get_max_metrics",
    )
    callback_counts = dict.fromkeys(method_names, 0)

    def transaction_check_for(method_name: str) -> Callable[[], None]:
        def check() -> None:
            callback_counts[method_name] += 1
            assert not migrated_connection.in_transaction

        return check

    activities = [
        _activity(1, "2026-10-24 08:00:00"),
        _activity(2, "2026-10-25 08:00:00", "swimming", type_id=2),
    ]
    data = FakeGarminData(
        activities=activities,
        oldest_activity=activities[0],
        activity_laps={
            "1": {"lapDTOs": [{"lapIndex": 1}]},
            "2": {"lapDTOs": [{"lapIndex": 1}]},
        },
        daily_summaries=[_daily_summary("2026-10-24", restingHeartRate=50)],
        hrv_by_day={"2026-10-24": {"hrvSummary": {"lastNightAvg": 45}}},
        max_metrics_by_day={
            "2026-10-24": [{"generic": {"vo2MaxPreciseValue": 48.0}}],
        },
        callbacks={name: transaction_check_for(name) for name in method_names},
    )

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
    )

    assert result.activities_added == 2
    assert result.wellness_days_written == 1
    for method_name in method_names:
        assert callback_counts[method_name] > 0
        assert callback_counts[method_name] == len(clients[0].calls[method_name])


def test_pull_calls_garmin_in_fixed_order(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    activities = [
        _activity(1, "2026-10-24 08:00:00"),
        _activity(2, "2026-10-25 08:00:00", "swimming", type_id=2),
    ]
    data = FakeGarminData(
        activities=activities,
        oldest_activity=activities[0],
        activity_laps={
            "1": {"lapDTOs": [{"lapIndex": 1}]},
            "2": {"lapDTOs": [{"lapIndex": 1}]},
        },
        daily_summaries=[_daily_summary("2026-10-24", restingHeartRate=50)],
    )

    _, clients = _run_pull(migrated_connection, auth_config, fake_server, data)

    days = ("2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27")
    assert clients[0].ordered_calls == [
        ("get_activity_types", ()),
        ("count_activities", ()),
        ("get_activities", (1, 1, None, None)),
        ("get_activities_by_date", ("2026-10-23", "2026-10-28", None, "asc")),
        ("get_activity_laps", ("1",)),
        ("get_activity_laps", ("2",)),
        ("get_daily_summaries", ("2026-10-24", "2026-10-27")),
        *[
            (method, (day,))
            for day in days
            for method in ("get_hrv_data", "get_max_metrics")
        ],
    ]


def test_pull_logs_activity_and_wellness_counts(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="training_season.garmin_pull")
    data = FakeGarminData(
        activities=[
            _activity(1),
            _activity(2),
            _activity(3, type_key="cycling", type_id=7),
            "not-an-object",
            _activity(4, start_time="not-a-time"),
            _activity(5, activityId=True),
        ],
        daily_summaries=[_daily_summary("2026-10-25", restingHeartRate=50)],
    )

    _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "training_season.garmin_pull"
    ] == [
        (
            logging.INFO,
            "activities 2026-10-24..2026-10-28: added 2, skipped 1, invalid 3",
        ),
        (logging.INFO, "wellness 2026-10-24..2026-10-27: wrote 1 days"),
    ]


@pytest.mark.parametrize(
    "entries",
    [
        ["not-a-dict"],
        [{"typeId": True, "typeKey": "running", "parentTypeId": None}],
        [{"typeId": 1, "typeKey": " ", "parentTypeId": None}],
        [{"typeId": 1, "typeKey": "running", "parentTypeId": True}],
        [{"typeId": 1, "typeKey": "running", "parentTypeId": None}] * 2,
    ],
    ids=["not-dict", "bool-type-id", "blank-type-key", "bool-parent", "duplicate-id"],
)
def test_build_type_roots_rejects_bad_entries(entries: list[object]) -> None:
    with pytest.raises(GarminUnavailableError) as raised:
        _build_type_roots(entries)

    assert str(raised.value) == "Garmin returned an unexpected activity type list"


def test_lock_is_held_for_the_whole_pull(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData()
    factory, clients = _factory_for(fake_server, data)
    lock_checks: list[str] = []

    def try_login() -> None:
        lock_checks.append("get_max_metrics")
        with pytest.raises(GarminLockTimeoutError):
            start_login(auth_config, factory=factory, lock_timeout=0)

    data.callbacks["get_max_metrics"] = try_login
    _write_fake_token(auth_config)

    pull_garmin(
        migrated_connection,
        auth_config,
        _ZONE,
        factory=factory,
        now=_NOW,
        history_start=_TODAY,
    )

    assert clients[0].calls["get_max_metrics"] == [(_TODAY,)]
    assert lock_checks == ["get_max_metrics"]


def test_login_reason_is_passed_through(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    fake_server.token_login_errors.append(_chain_rejected_token_error())

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        FakeGarminData(),
    )

    assert result.login_reason is GarminLoginReason.TOKEN_REJECTED


def _configure_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "training.db"))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.delenv("GARMIN_HISTORY_START", raising=False)
    monkeypatch.setattr(garmin_pull, "initialize_database", lambda: None)
    monkeypatch.setattr(
        garmin_pull,
        "connect",
        lambda _path: sqlite3.connect(":memory:", autocommit=True),
    )


def test_cli_prints_counts_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    result = GarminPullResult(
        login_reason=None,
        activity_start="2026-10-20",
        activity_end="2026-10-28",
        activities_added=2,
        activities_skipped=1,
        activities_invalid=0,
        wellness_start="2026-10-20",
        wellness_end="2026-10-27",
        wellness_days_written=3,
    )
    monkeypatch.setattr(garmin_pull, "pull_garmin", lambda *_args, **_kwargs: result)

    assert garmin_pull.main([]) == 0
    assert capsys.readouterr().out == (
        "activities 2026-10-20..2026-10-28: added 2, skipped 1, invalid 0; "
        "wellness 2026-10-20..2026-10-27: 3 days\n"
    )


@pytest.mark.parametrize(
    ("error", "environment", "expected_code"),
    [
        (
            GarminMfaRequiredError(GarminLoginReason.MANUAL, "requested"),
            {},
            3,
        ),
        (GarminConfigurationError("invalid configuration"), {}, 2),
        (None, {"TZ": "not/a-timezone"}, 2),
        (None, {"GARMIN_HISTORY_START": "not-a-date"}, 2),
        (GarminRateLimitedError("rate limited"), {}, 1),
        (sqlite3.Error("database failure"), {}, 1),
    ],
)
def test_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: BaseException | None,
    environment: dict[str, str],
    expected_code: int,
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    def fail(*_args: Any, **_kwargs: Any) -> GarminPullResult:
        if error is None:
            raise AssertionError("pull should not run for invalid configuration")
        raise error

    monkeypatch.setattr(garmin_pull, "pull_garmin", fail)

    assert garmin_pull.main([]) == expected_code


def test_history_start_from_environment_parses_or_rejects() -> None:
    assert history_start_from_environment({}) is None
    assert history_start_from_environment({"GARMIN_HISTORY_START": "   "}) is None
    assert (
        history_start_from_environment({"GARMIN_HISTORY_START": "2026-02-01"})
        == "2026-02-01"
    )

    for value in ("2026-2-1", "2026-02-30", " 2026-02-01 ", "nope"):
        with pytest.raises(GarminConfigurationError) as raised:
            history_start_from_environment({"GARMIN_HISTORY_START": value})
        assert value not in str(raised.value)
        assert str(raised.value) == _HISTORY_START_MESSAGE


def test_history_start_bounds_first_run_without_oldest_lookup(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(activities=[_activity(1)])

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-20",
    )

    assert result.activity_start == "2026-10-20"
    assert result.wellness_start == "2026-10-20"
    assert "count_activities" not in clients[0].calls
    assert "get_activities" not in clients[0].calls
    assert clients[0].calls["get_activities_by_date"][0][0] == "2026-10-20"
    assert clients[0].calls["get_daily_summaries"][0][0] == "2026-10-20"


def test_history_start_is_ignored_when_tables_have_rows(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    _seed_activity(migrated_connection, "stored", "2026-10-22")
    _seed_wellness(migrated_connection, "2026-10-21")
    data = FakeGarminData()

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-25",
    )

    assert result.activity_start == "2026-10-08"
    assert result.wellness_start == "2026-10-07"
    assert clients[0].calls["get_activities_by_date"][0][0] == "2026-10-08"
    assert clients[0].calls["get_daily_summaries"][0][0] == "2026-10-07"


@pytest.mark.parametrize(
    (
        "seed_activity",
        "seed_wellness",
        "expected_activity_start",
        "expected_wellness_start",
    ),
    [
        (True, False, "2026-10-08", "2026-10-25"),
        (False, True, "2026-10-25", "2026-10-07"),
    ],
)
def test_history_start_applies_per_table(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    seed_activity: bool,
    seed_wellness: bool,
    expected_activity_start: str,
    expected_wellness_start: str,
) -> None:
    if seed_activity:
        _seed_activity(migrated_connection, "stored", "2026-10-22")
    if seed_wellness:
        _seed_wellness(migrated_connection, "2026-10-21")

    result, clients = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        FakeGarminData(),
        history_start="2026-10-25",
    )

    assert result.activity_start == expected_activity_start
    assert result.wellness_start == expected_wellness_start
    assert clients[0].calls["get_activities_by_date"][0][0] == (expected_activity_start)
    assert clients[0].calls["get_daily_summaries"][0][0] == (expected_wellness_start)


def test_history_start_in_the_future_fails_before_garmin_calls(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData()
    factory, clients = _factory_for(fake_server, data)
    future_date = (date.fromisoformat(_TODAY) + timedelta(days=1)).isoformat()
    _write_fake_token(auth_config)

    with pytest.raises(GarminConfigurationError) as raised:
        pull_garmin(
            migrated_connection,
            auth_config,
            _ZONE,
            factory=factory,
            now=_NOW,
            history_start=future_date,
        )

    assert future_date not in str(raised.value)
    assert str(raised.value) == _HISTORY_START_MESSAGE
    assert clients == []
    assert data.errors == {}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([{"generic": {"vo2MaxPreciseValue": 49.1}}], 49.1),
        ([], None),
        ({}, None),
        ([{}], None),
        ([{"generic": {"vo2MaxPreciseValue": float("inf")}}], None),
    ],
)
def test_vo2max_extracts_only_supported_shape(
    payload: object,
    expected: float | None,
) -> None:
    assert _vo2max_from(payload) == expected
