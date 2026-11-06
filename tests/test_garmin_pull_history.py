"""Tests for the Garmin pull history start bound."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import sqlite3

from garmin_auth_fakes import FakeGarminServer, _write_fake_token
from garmin_pull_fakes import (
    _NOW,
    _TODAY,
    _ZONE,
    FakeGarminData,
    _activity,
    _factory_for,
    _run_pull,
    _seed_activity,
    _seed_wellness,
)
from training_season.garmin_auth import GarminAuthConfig, GarminConfigurationError
from training_season.garmin_pull import history_start_from_environment, pull_garmin

_HISTORY_START_MESSAGE = (
    "GARMIN_HISTORY_START must be a valid YYYY-MM-DD date no later than today"
)


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
