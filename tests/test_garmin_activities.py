"""Tests for the Garmin activity pull."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

if TYPE_CHECKING:
    from garmin_auth_fakes import FakeGarminServer

from garmin_pull_fakes import (
    FakeGarminData,
    _activity,
    _activity_rows,
    _connection_error,
    _run_pull,
    _seed_activity,
)
from training_season import garmin_activities, garmin_wellness
from training_season.garmin_auth import (
    GarminAuthConfig,
    GarminError,
    GarminRateLimitedError,
    GarminTokenExpiredError,
    GarminUnavailableError,
)


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
