"""Tests for the Garmin activity column mapping."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from garminconnect.exceptions import GarminConnectNotFoundError

if TYPE_CHECKING:
    import sqlite3

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig

from garmin_pull_fakes import FakeGarminData, _activity, _activity_rows, _run_pull
from training_season.garmin_pull_base import non_negative


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
