"""Tests for the week push to the Garmin Connect calendar."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

import pytest
import requests
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
)

from garmin_pull_fakes import _connection_error
from garmin_push_fakes import (
    PRIVATE_NOTE,
    PRIVATE_PACE,
    FakeGarminCalendar,
    push_factory_for,
    run_push,
    seed_active_macro,
    week_text,
    workout_rows,
)
from training_season.garmin_auth import GarminUnavailableError
from training_season.garmin_push import scheduled_week_lines
from training_season.push_result import PushFailure, PushStep
from training_season.push_store import NoActiveMacroPlanError

if TYPE_CHECKING:
    import sqlite3

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig

_THREE_DAYS = ["2027-01-04", "2027-01-06", "2027-01-08"]


@pytest.fixture
def connection(migrated_connection: sqlite3.Connection) -> sqlite3.Connection:
    seed_active_macro(migrated_connection)
    return migrated_connection


def test_first_push_stores_ids_and_dates(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    result, client = run_push(
        connection,
        auth_config,
        fake_server,
        FakeGarminCalendar(),
        week_text(_THREE_DAYS),
    )

    assert result.landed == (date(2027, 1, 4), date(2027, 1, 6), date(2027, 1, 8))
    assert result.failed is None
    assert result.not_attempted == ()
    assert result.chat_message == (
        "Semana de 04/01 enviada ao Garmin. Agendados: 04/01, 06/01, 08/01."
    )
    assert client.calls["schedule_workout"] == [
        ("1001", "2027-01-04"),
        ("1003", "2027-01-06"),
        ("1005", "2027-01-08"),
    ]
    assert workout_rows(connection) == [
        ("2027-01-04", "2027-01-04", "04/01 Leve", "1001", "1002"),
        ("2027-01-04", "2027-01-06", "06/01 Leve", "1003", "1004"),
        ("2027-01-04", "2027-01-08", "08/01 Leve", "1005", "1006"),
    ]


def test_push_stores_the_canonical_week_json(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    run_push(connection, auth_config, fake_server, FakeGarminCalendar(), week_text([]))

    row = connection.execute(
        "SELECT week_start, week_end, raw_json FROM weekly_plan"
    ).fetchone()
    assert row == (
        "2027-01-04",
        "2027-01-10",
        '{"week_start":"2027-01-04","workouts":[]}',
    )


def test_repush_removes_old_rows_first_and_leaves_no_duplicate(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    calendar = FakeGarminCalendar()
    run_push(connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS[:2]))

    _, client = run_push(
        connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS[1:])
    )

    assert client.ordered_calls == [
        ("unschedule_workout", ("1002",)),
        ("delete_workout", ("1001",)),
        ("unschedule_workout", ("1004",)),
        ("delete_workout", ("1003",)),
        ("upload_running_workout", ("06/01 Leve",)),
        ("schedule_workout", ("1005", "2027-01-06")),
        ("upload_running_workout", ("08/01 Leve",)),
        ("schedule_workout", ("1007", "2027-01-08")),
    ]
    assert workout_rows(connection) == [
        ("2027-01-04", "2027-01-06", "06/01 Leve", "1005", "1006"),
        ("2027-01-04", "2027-01-08", "08/01 Leve", "1007", "1008"),
    ]
    assert connection.execute("SELECT count(*) FROM weekly_plan").fetchone() == (1,)


def test_removal_not_found_counts_as_already_gone(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    calendar = FakeGarminCalendar()
    run_push(connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS[:1]))
    calendar.errors = {
        ("unschedule_workout", 0): GarminConnectNotFoundError("gone"),
        ("delete_workout", 0): GarminConnectNotFoundError("gone"),
    }

    result, _ = run_push(
        connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS[:1])
    )

    assert result.failed is None
    assert workout_rows(connection) == [
        ("2027-01-04", "2027-01-04", "04/01 Leve", "1003", "1004"),
    ]


def test_first_failure_stops_and_names_the_days(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    calendar = FakeGarminCalendar(
        errors={("schedule_workout", 1): _connection_error(503)}
    )

    result, client = run_push(
        connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS)
    )

    assert result.landed == (date(2027, 1, 4),)
    assert result.failed == PushFailure(
        date(2027, 1, 6), PushStep.SCHEDULE, "o Garmin devolveu erro HTTP 503"
    )
    assert result.not_attempted == (date(2027, 1, 8),)
    assert result.chat_message == (
        "Envio da semana de 04/01 incompleto. Agendados: 04/01. "
        "Falhou: 06/01, no agendamento: o Garmin devolveu erro HTTP 503. "
        "Não tentados: 08/01."
    )
    assert len(client.calls["upload_running_workout"]) == 2
    assert workout_rows(connection) == [
        ("2027-01-04", "2027-01-04", "04/01 Leve", "1001", "1002"),
        ("2027-01-04", "2027-01-06", "06/01 Leve", "1003", None),
    ]
    calendar.errors = {}

    _, repush_client = run_push(
        connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS)
    )

    assert repush_client.ordered_calls[:3] == [
        ("unschedule_workout", ("1002",)),
        ("delete_workout", ("1001",)),
        ("delete_workout", ("1003",)),
    ]


def test_push_removes_our_rows_on_its_days_from_another_week_start(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    calendar = FakeGarminCalendar()
    first_week = week_text(["2027-01-05", "2027-01-06"])
    run_push(connection, auth_config, fake_server, calendar, first_week)

    _, client = run_push(
        connection,
        auth_config,
        fake_server,
        calendar,
        week_text(["2027-01-06"], week_start="2027-01-06"),
    )

    assert client.ordered_calls[:2] == [
        ("unschedule_workout", ("1004",)),
        ("delete_workout", ("1003",)),
    ]
    assert workout_rows(connection) == [
        ("2027-01-04", "2027-01-05", "05/01 Leve", "1001", "1002"),
        ("2027-01-06", "2027-01-06", "06/01 Leve", "1005", "1006"),
    ]


def test_removal_failure_sends_no_new_workout(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    calendar = FakeGarminCalendar()
    run_push(connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS[:1]))
    calendar.errors = {("delete_workout", 0): _connection_error(429)}

    result, client = run_push(
        connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS[1:])
    )

    assert result.chat_message == (
        "Envio da semana de 04/01 incompleto. Agendados: nenhum. "
        "Falhou: 04/01, na remoção do treino anterior: "
        "o Garmin limitou as requisições, tente mais tarde. "
        "Não tentados: 06/01, 08/01."
    )
    assert "upload_running_workout" not in client.calls
    assert len(workout_rows(connection)) == 1


@pytest.mark.parametrize(
    ("errors", "upload_responses", "schedule_responses", "failure"),
    [
        (
            {("upload_running_workout", 0): GarminConnectAuthenticationError("401")},
            {},
            {},
            PushFailure(
                date(2027, 1, 4),
                PushStep.UPLOAD,
                "o Garmin recusou a sessão salva, faça login de novo",
            ),
        ),
        (
            {("schedule_workout", 0): requests.ReadTimeout("timeout")},
            {},
            {},
            PushFailure(date(2027, 1, 4), PushStep.SCHEDULE, "o Garmin não respondeu"),
        ),
        (
            {("upload_running_workout", 0): _connection_error(400)},
            {},
            {},
            PushFailure(
                date(2027, 1, 4),
                PushStep.UPLOAD,
                "o Garmin devolveu erro HTTP 400",
            ),
        ),
        (
            {},
            {0: {"workoutId": 0}},
            {},
            PushFailure(
                date(2027, 1, 4),
                PushStep.UPLOAD,
                "o Garmin não devolveu o id do treino",
            ),
        ),
        (
            {},
            {},
            {0: {"id": 77}},
            PushFailure(
                date(2027, 1, 4),
                PushStep.SCHEDULE,
                "o Garmin não devolveu o id do agendamento",
            ),
        ),
    ],
)
def test_failure_reason_is_fixed_text(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    errors: dict[tuple[str, int], BaseException],
    upload_responses: dict[int, object],
    schedule_responses: dict[int, object],
    failure: PushFailure,
) -> None:
    calendar = FakeGarminCalendar(
        errors=errors,
        upload_responses=upload_responses,
        schedule_responses=schedule_responses,
    )

    result, _ = run_push(
        connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS[:1])
    )

    assert result.failed == failure


def test_empty_week_removes_only_our_rows_of_that_week(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    calendar = FakeGarminCalendar()
    run_push(
        connection,
        auth_config,
        fake_server,
        calendar,
        week_text(["2027-01-11"], week_start="2027-01-11"),
    )
    run_push(connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS[:1]))

    result, client = run_push(
        connection, auth_config, fake_server, calendar, week_text([])
    )

    assert client.ordered_calls == [
        ("unschedule_workout", ("1004",)),
        ("delete_workout", ("1003",)),
    ]
    assert result.chat_message == (
        "Semana de 04/01 sem treinos. Os treinos que este app enviou para a "
        "semana foram removidos do Garmin."
    )
    assert workout_rows(connection) == [
        ("2027-01-11", "2027-01-11", "11/01 Leve", "1001", "1002"),
    ]


def test_no_active_macro_plan_makes_no_garmin_call(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    with pytest.raises(NoActiveMacroPlanError):
        run_push(
            migrated_connection,
            auth_config,
            fake_server,
            FakeGarminCalendar(),
            week_text(_THREE_DAYS),
        )

    assert fake_server.login_calls == []


def test_logs_carry_no_workout_content_or_response_body(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = _connection_error(500)
    body.args = ("private response body",)
    calendar = FakeGarminCalendar(errors={("schedule_workout", 1): body})
    caplog.set_level(logging.DEBUG)

    run_push(connection, auth_config, fake_server, calendar, week_text(_THREE_DAYS))

    assert "2027-01-04" in caplog.text
    assert "Leve" not in caplog.text
    assert PRIVATE_NOTE not in caplog.text
    assert str(PRIVATE_PACE) not in caplog.text
    assert "private response body" not in caplog.text


def test_scheduled_week_lines_read_one_period(fake_server: FakeGarminServer) -> None:
    calendar = FakeGarminCalendar(
        by_period_entries=[
            {"calendarDate": "2027-01-06", "workout": {"workoutName": "06/01 Leve"}},
            {"calendarDate": "2027-01-04", "workout": {"workoutName": "04/01 Tiros"}},
            {"calendarDate": "2027-01-05", "workout": None},
            "not an entry",
        ]
    )
    factory, clients = push_factory_for(fake_server, calendar)
    client = factory(None, None)

    lines = scheduled_week_lines(client, date(2027, 1, 4))

    assert lines == ("2027-01-04 04/01 Tiros", "2027-01-05 ?", "2027-01-06 06/01 Leve")
    assert clients[0].ordered_calls == [
        ("get_scheduled_workouts_by_period", ("2027-01-04", "2027-01-10")),
    ]


def test_scheduled_week_lines_treat_an_empty_response_as_no_entry(
    fake_server: FakeGarminServer,
) -> None:
    factory, _ = push_factory_for(fake_server, FakeGarminCalendar(by_period_entries={}))

    assert scheduled_week_lines(factory(None, None), date(2027, 1, 4)) == ()


@pytest.mark.parametrize("response", [{"calendarDate": "2027-01-04"}, 7, None])
def test_scheduled_week_lines_reject_a_response_that_is_not_a_list(
    fake_server: FakeGarminServer, response: object
) -> None:
    calendar = FakeGarminCalendar(by_period_entries=response)
    factory, _ = push_factory_for(fake_server, calendar)

    with pytest.raises(GarminUnavailableError, match="expected a list"):
        scheduled_week_lines(factory(None, None), date(2027, 1, 4))


def test_scheduled_week_lines_map_a_network_error(
    fake_server: FakeGarminServer,
) -> None:
    calendar = FakeGarminCalendar(
        errors={("get_scheduled_workouts_by_period", 0): requests.ConnectionError()}
    )
    factory, _ = push_factory_for(fake_server, calendar)

    with pytest.raises(GarminUnavailableError, match="unreachable"):
        scheduled_week_lines(factory(None, None), date(2027, 1, 4))
