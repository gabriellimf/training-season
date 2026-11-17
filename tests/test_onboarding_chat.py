"""Tests for the onboarding chat steps that the console and the web share."""

from __future__ import annotations

from datetime import UTC, date, datetime
from io import StringIO
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from console_fakes import ScriptedConsole
from garmin_auth_fakes import _write_fake_token
from garmin_push_fakes import (
    FakeGarminCalendar,
    push_factory_for,
    seed_active_macro,
    week_text,
)
from llm_fakes import QueuedReplyAdapter
from training_season.coach_instruction import load_coach_instruction
from training_season.onboarding_chat import (
    FIRST_INTRO,
    LEAVE_COMMAND,
    REOPEN_INTRO,
    Coach,
    CoachSetup,
    cleaned_athlete_text,
    converse,
    first_week_outcome,
    rendered_for_now,
    send_onboarding_message,
)
from training_season.onboarding_store import (
    SessionMessage,
    open_session,
    session_messages,
)
from training_season.push_result import PushResult

if TYPE_CHECKING:
    import sqlite3

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory

_ZONE = ZoneInfo("America/Sao_Paulo")
_WEDNESDAY_NOON = datetime(2027, 1, 6, 15, 0, tzinfo=UTC)


def _setup(
    auth_config: GarminAuthConfig,
    garmin_factory: GarminFactory,
    now: datetime = _WEDNESDAY_NOON,
) -> CoachSetup:
    return CoachSetup(
        instruction=load_coach_instruction({}),
        zone=_ZONE,
        auth_config=auth_config,
        garmin_factory=garmin_factory,
        clock=lambda: now,
    )


def _calendar_setup(
    auth_config: GarminAuthConfig, fake_server: FakeGarminServer
) -> CoachSetup:
    factory, _ = push_factory_for(fake_server, FakeGarminCalendar())
    return _setup(auth_config, factory)


def test_rendered_for_now_uses_the_training_date_of_the_zone(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    factory, _ = push_factory_for(fake_server, FakeGarminCalendar())
    late_evening = datetime(2027, 1, 7, 1, 30, tzinfo=UTC)
    setup = _setup(auth_config, factory, now=late_evening)

    instruction_text, today = rendered_for_now(setup, migrated_connection)

    assert today == date(2027, 1, 6)
    assert "2027-01-06" in instruction_text


def test_send_onboarding_message_stores_the_exchange(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    session_id = open_session(migrated_connection)
    adapter = QueuedReplyAdapter(["Qual é a sua prova?"])
    setup = _calendar_setup(auth_config, fake_server)

    result = send_onboarding_message(
        migrated_connection, adapter, setup, session_id, "Quero correr"
    )

    assert (result.status, result.chat_message) == ("asking", "Qual é a sua prova?")
    assert session_messages(migrated_connection, session_id) == (
        SessionMessage("user", "Quero correr"),
        SessionMessage("assistant", "Qual é a sua prova?"),
    )


def test_first_week_outcome_pushes_today_to_sunday(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    seed_active_macro(migrated_connection)
    _write_fake_token(auth_config)
    reply = week_text(["2027-01-07", "2027-01-09"], week_start="2027-01-06")
    adapter = QueuedReplyAdapter([reply])
    setup = _calendar_setup(auth_config, fake_server)

    outcome = first_week_outcome(migrated_connection, adapter, setup)

    assert isinstance(outcome, PushResult)
    assert outcome.landed == (date(2027, 1, 7), date(2027, 1, 9))


@pytest.mark.parametrize("intro", [FIRST_INTRO, REOPEN_INTRO])
def test_intros_carry_no_console_command(intro: str) -> None:
    assert LEAVE_COMMAND not in intro


def test_console_intro_ends_with_the_pause_hint(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    out = StringIO()
    setup = _calendar_setup(auth_config, fake_server)
    coach = Coach(migrated_connection, QueuedReplyAdapter([]), setup, out)

    code = converse(
        coach, open_session(migrated_connection), ScriptedConsole([]).read_line
    )

    assert code == 0
    assert out.getvalue() == f"Treinador: {FIRST_INTRO} Digite /sair para pausar.\n\n"


def test_cleaned_athlete_text_drops_control_characters_and_outer_spaces() -> None:
    assert cleaned_athlete_text("  bom\x1b dia\nok \t") == "bom dia\nok"
