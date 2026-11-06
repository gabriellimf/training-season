"""Tests for the Garmin wellness pull."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from garminconnect.exceptions import GarminConnectNotFoundError

if TYPE_CHECKING:
    from garmin_auth_fakes import FakeGarminServer

from garmin_pull_fakes import (
    _SEED_TIMESTAMP,
    _TODAY,
    FakeGarminData,
    _activity,
    _connection_error,
    _daily_summary,
    _run_pull,
    _seed_wellness,
)
from training_season.garmin_auth import GarminAuthConfig, GarminUnavailableError
from training_season.garmin_wellness import _vo2max_from


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
