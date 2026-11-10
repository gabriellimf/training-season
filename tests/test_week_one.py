"""Tests for the week-1 range and push after onboarding."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from garmin_auth_fakes import _write_fake_token
from garmin_push_fakes import (
    FakeGarminCalendar,
    push_factory_for,
    run_push,
    seed_active_macro,
    week_text,
    workout_rows,
)
from llm_fakes import QueuedReplyAdapter
from training_season.push_result import PushResult
from training_season.push_store import NoActiveMacroPlanError
from training_season.validated_reply import Rejected
from training_season.week_one import push_week_one, week_one_range

if TYPE_CHECKING:
    import sqlite3

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig

_WEDNESDAY = date(2027, 1, 6)


@pytest.mark.parametrize(
    ("today", "last_day"),
    [
        (date(2027, 1, 6), date(2027, 1, 10)),
        (date(2027, 1, 10), date(2027, 1, 10)),
        (date(2027, 1, 11), date(2027, 1, 17)),
    ],
)
def test_week_one_runs_from_today_to_the_coming_sunday(
    today: date, last_day: date
) -> None:
    assert week_one_range(today) == (today, last_day)


def _push(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    adapter: QueuedReplyAdapter,
    calendar: FakeGarminCalendar | None = None,
) -> tuple[PushResult | Rejected, int]:
    _write_fake_token(auth_config)
    factory, clients = push_factory_for(fake_server, calendar or FakeGarminCalendar())
    outcome = push_week_one(
        connection,
        adapter,
        auth_config,
        instruction_text="Instrução",
        today=_WEDNESDAY,
        factory=factory,
    )
    return outcome, len(clients)


def test_week_one_keeps_the_workouts_after_its_sunday(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    seed_active_macro(migrated_connection)
    calendar = FakeGarminCalendar()
    next_week = week_text(["2027-01-11"], week_start="2027-01-11")
    run_push(migrated_connection, auth_config, fake_server, calendar, next_week)
    reply = week_text(["2027-01-07", "2027-01-09"], week_start="2027-01-06")
    adapter = QueuedReplyAdapter([reply])

    outcome, _ = _push(migrated_connection, auth_config, fake_server, adapter, calendar)

    assert isinstance(outcome, PushResult)
    assert outcome.landed == (date(2027, 1, 7), date(2027, 1, 9))
    dates = [row[1] for row in workout_rows(migrated_connection)]
    assert dates == ["2027-01-07", "2027-01-09", "2027-01-11"]
    week_end = migrated_connection.execute(
        "SELECT week_end FROM weekly_plan WHERE week_start = '2027-01-06'"
    ).fetchone()[0]
    assert week_end == "2027-01-10"


def test_rejected_week_pushes_nothing(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    seed_active_macro(migrated_connection)

    outcome, client_count = _push(
        migrated_connection, auth_config, fake_server, QueuedReplyAdapter(["{}", "{}"])
    )

    assert isinstance(outcome, Rejected)
    assert outcome.chat_message.startswith("O treinador devolveu uma semana inválida")
    assert client_count == 0
    assert workout_rows(migrated_connection) == []


def test_no_active_macro_fails_before_any_request(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    adapter = QueuedReplyAdapter([])

    with pytest.raises(NoActiveMacroPlanError):
        _push(migrated_connection, auth_config, fake_server, adapter)

    assert adapter.requests == []
