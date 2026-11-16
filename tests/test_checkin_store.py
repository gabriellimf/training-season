"""Tests for storing check-in answers and the athlete's free text."""

import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from training_season.checkin_store import (
    CheckinAnswers,
    CheckinStoreError,
    append_checkin_message,
    checkin_week_of,
    store_checkin_answers,
)
from training_season.coach_context_lines import checkin_text
from training_season.coach_context_store import read_latest_checkin_before

_WEEK = date(2026, 11, 16)
_SAO_PAULO_EVENING = datetime(2026, 11, 18, 18, 30, 5, tzinfo=ZoneInfo("Etc/GMT+3"))


def _stored_row(connection: sqlite3.Connection, checkin_id: int) -> tuple[object, ...]:
    row = connection.execute(
        "SELECT week_start, fatigue, has_pain, pain_location, pain_level, "
        "free_text, created_at FROM checkin WHERE id = ?",
        (checkin_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _store(connection: sqlite3.Connection, answers: CheckinAnswers) -> int:
    return store_checkin_answers(
        connection, answers, week_start=_WEEK, now=_SAO_PAULO_EVENING
    )


@pytest.mark.parametrize(
    "today",
    [date(2026, 11, 16), date(2026, 11, 18), date(2026, 11, 22)],
    ids=["monday", "wednesday", "sunday"],
)
def test_checkin_week_is_the_monday_of_the_local_week(today: date) -> None:
    assert checkin_week_of(today) == date(2026, 11, 16)


def test_answers_without_pain_store_null_pain_fields_and_utc_time(
    migrated_connection: sqlite3.Connection,
) -> None:
    checkin_id = _store(migrated_connection, CheckinAnswers(7, None, None))

    assert _stored_row(migrated_connection, checkin_id) == (
        "2026-11-16",
        7,
        0,
        None,
        None,
        None,
        "2026-11-18T21:30:05Z",
    )


def test_answers_with_pain_store_location_and_level(
    migrated_connection: sqlite3.Connection,
) -> None:
    checkin_id = _store(migrated_connection, CheckinAnswers(4, "joelho", 6))

    assert _stored_row(migrated_connection, checkin_id)[1:5] == (4, 1, "joelho", 6)


def test_each_checkin_is_a_new_row(migrated_connection: sqlite3.Connection) -> None:
    first = _store(migrated_connection, CheckinAnswers(3, None, None))
    second = _store(migrated_connection, CheckinAnswers(8, None, None))

    assert second > first


def test_messages_append_to_free_text_one_per_line(
    migrated_connection: sqlite3.Connection,
) -> None:
    checkin_id = _store(migrated_connection, CheckinAnswers(5, None, None))

    append_checkin_message(migrated_connection, checkin_id, "dormi mal")
    first = _stored_row(migrated_connection, checkin_id)[5]
    append_checkin_message(migrated_connection, checkin_id, "semana puxada")

    assert first == "dormi mal"
    assert _stored_row(migrated_connection, checkin_id)[5] == "dormi mal\nsemana puxada"


def test_blank_message_is_refused_and_free_text_stays(
    migrated_connection: sqlite3.Connection,
) -> None:
    checkin_id = _store(migrated_connection, CheckinAnswers(5, None, None))
    append_checkin_message(migrated_connection, checkin_id, "dormi mal")

    with pytest.raises(CheckinStoreError, match="non-blank"):
        append_checkin_message(migrated_connection, checkin_id, " \n ")

    assert _stored_row(migrated_connection, checkin_id)[5] == "dormi mal"


def test_message_for_a_missing_checkin_is_refused(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(CheckinStoreError, match="check-in 99 does not exist"):
        append_checkin_message(migrated_connection, 99, "oi")


def test_stored_checkin_reaches_the_next_week_context_line(
    migrated_connection: sqlite3.Connection,
) -> None:
    checkin_id = _store(migrated_connection, CheckinAnswers(6, "panturrilha", 3))
    append_checkin_message(migrated_connection, checkin_id, "cansada")

    row = read_latest_checkin_before(migrated_connection, date(2026, 11, 23))

    pain = "dor: sim, local panturrilha, nível 3/10"
    assert row is not None
    assert checkin_text(row).splitlines() == [
        f"Último check-in: semana 2026-11-16, fadiga 6/10, {pain}.",
        "Texto livre: cansada",
    ]
