"""Tests for the Garmin history pull."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from garminconnect.exceptions import GarminConnectConnectionError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

from garmin_auth_fakes import (
    FakeGarminServer,
    _chain_rejected_token_error,
    _write_fake_token,
)
from garmin_pull_fakes import (
    _NOW,
    _TODAY,
    _ZONE,
    FakeGarminData,
    _activity,
    _activity_rows,
    _activity_types,
    _daily_summary,
    _factory_for,
    _run_pull,
)
from training_season.garmin_auth import (
    GarminAuthConfig,
    GarminLockTimeoutError,
    GarminLoginReason,
    GarminMfaRequiredError,
    GarminUnavailableError,
    start_login,
)
from training_season.garmin_pull import pull_garmin


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
