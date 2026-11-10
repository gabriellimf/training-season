"""Tests for the shared week generation: context, contract, validation, one retry."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from garmin_push_fakes import seed_active_macro, week_text
from llm_fakes import QueuedReplyAdapter
from training_season.coach_context import assemble_coach_context
from training_season.push_week import PushWeekError
from training_season.validated_reply import Accepted, Rejected
from training_season.week_generation import (
    WEEK_OUTPUT_CONTRACT,
    generate_week,
    week_from_text,
    week_system_text,
)
from training_season.workout_request import WORKOUT_FIELD_RULES

if TYPE_CHECKING:
    import sqlite3

_WEDNESDAY = date(2027, 1, 6)
_SUNDAY = date(2027, 1, 10)
_VALID_WEEK = week_text(["2027-01-06", "2027-01-09"], week_start="2027-01-06")
_LATE_WEEK = week_text(["2027-01-11"], week_start="2027-01-06")


@pytest.fixture
def connection(migrated_connection: sqlite3.Connection) -> sqlite3.Connection:
    seed_active_macro(migrated_connection)
    return migrated_connection


def run_generation(
    connection: sqlite3.Connection, replies: list[str]
) -> tuple[object, QueuedReplyAdapter]:
    adapter = QueuedReplyAdapter(replies)
    outcome = generate_week(
        connection,
        adapter,
        instruction_text="Instrução",
        first_day=_WEDNESDAY,
        last_day=_SUNDAY,
    )
    return outcome, adapter


def test_valid_week_ends_on_the_last_day(connection: sqlite3.Connection) -> None:
    outcome, adapter = run_generation(connection, [_VALID_WEEK])

    assert isinstance(outcome, Accepted)
    assert (outcome.value.week_start, outcome.value.week_end) == (_WEDNESDAY, _SUNDAY)
    request = adapter.requests[0]
    assert request.job.value == "plan"
    assert request.system == week_system_text("Instrução")
    context_text = assemble_coach_context(connection, _WEDNESDAY).text
    assert request.turns[0].text.startswith(context_text)
    assert "de 2027-01-06 a 2027-01-10" in request.turns[0].text


def test_invalid_then_valid_echoes_the_reply_and_the_errors(
    connection: sqlite3.Connection,
) -> None:
    outcome, adapter = run_generation(connection, [_LATE_WEEK, _VALID_WEEK])

    assert isinstance(outcome, Accepted)
    assert outcome.attempts == 2
    retry = adapter.requests[1].turns
    assert [turn.role for turn in retry] == ["user", "assistant", "user"]
    assert retry[1].text == _LATE_WEEK
    assert "outside the week 2027-01-06..2027-01-10" in retry[2].text


def test_invalid_twice_is_rejected_and_stores_nothing(
    connection: sqlite3.Connection,
) -> None:
    outcome, _ = run_generation(connection, [_LATE_WEEK, "not json"])

    assert isinstance(outcome, Rejected)
    assert outcome.chat_message.startswith(
        "O treinador devolveu uma semana inválida duas vezes. "
        "Nada foi enviado ao Garmin."
    )
    assert connection.execute("SELECT count(*) FROM weekly_plan").fetchone() == (0,)


def test_fenced_week_is_accepted() -> None:
    week = week_from_text(f"```json\n{_VALID_WEEK}\n```", _WEDNESDAY, _SUNDAY)

    assert week.week_end == _SUNDAY


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_LATE_WEEK, "outside the week 2027-01-06..2027-01-10"),
        (
            week_text([], week_start="2027-01-04"),
            "week_start is 2027-01-04, expected 2027-01-06",
        ),
        ("x" * 100_001, "week text has 100001 characters, the maximum is 100000"),
    ],
)
def test_week_outside_the_requested_days_is_rejected(text: str, expected: str) -> None:
    with pytest.raises(PushWeekError) as raised:
        week_from_text(text, _WEDNESDAY, _SUNDAY)

    assert expected in str(raised.value)


def test_contract_reuses_the_workout_field_rules() -> None:
    assert WEEK_OUTPUT_CONTRACT.endswith(WORKOUT_FIELD_RULES)
    assert "DD/MM" in WEEK_OUTPUT_CONTRACT
    assert week_system_text("Instrução").endswith(WEEK_OUTPUT_CONTRACT)
