"""Tests for onboarding sessions and their stored chat turns."""

import sqlite3

import pytest

from schema_rows import _CREATED_AT, _insert_macro
from training_season.onboarding_store import (
    OnboardingSessionError,
    SessionMessage,
    append_exchange,
    current_open_session,
    discard_open_session,
    open_session,
    session_messages,
)


def test_no_open_session_on_a_fresh_install(
    migrated_connection: sqlite3.Connection,
) -> None:
    assert current_open_session(migrated_connection) is None


def test_open_session_becomes_the_current_one(
    migrated_connection: sqlite3.Connection,
) -> None:
    session_id = open_session(migrated_connection)

    assert current_open_session(migrated_connection) == session_id


def test_second_open_session_is_refused(
    migrated_connection: sqlite3.Connection,
) -> None:
    session_id = open_session(migrated_connection)

    with pytest.raises(OnboardingSessionError, match=f"session {session_id} is"):
        open_session(migrated_connection)


def test_schema_allows_one_open_session(
    migrated_connection: sqlite3.Connection,
) -> None:
    open_session(migrated_connection)

    with pytest.raises(sqlite3.IntegrityError, match=r"onboarding_session\.status"):
        migrated_connection.execute(
            "INSERT INTO onboarding_session (status, started_at) VALUES ('open', ?)",
            (_CREATED_AT,),
        )


def test_completed_session_needs_a_macro_plan(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_connection.execute(
            "INSERT INTO onboarding_session (status, started_at, completed_at) "
            "VALUES ('completed', ?, ?)",
            (_CREATED_AT, _CREATED_AT),
        )


def test_completed_session_with_macro_plan_is_accepted(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)

    migrated_connection.execute(
        "INSERT INTO onboarding_session "
        "(status, started_at, completed_at, macro_plan_id) "
        "VALUES ('completed', ?, ?, ?)",
        (_CREATED_AT, _CREATED_AT, macro_plan_id),
    )


@pytest.mark.parametrize("started_at", ["2026-10-26T00:00:00", "2026-10-26"])
def test_started_at_must_be_a_utc_timestamp(
    migrated_connection: sqlite3.Connection, started_at: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_connection.execute(
            "INSERT INTO onboarding_session (status, started_at) VALUES ('open', ?)",
            (started_at,),
        )


def test_exchanges_come_back_in_order(
    migrated_connection: sqlite3.Connection,
) -> None:
    session_id = open_session(migrated_connection)

    append_exchange(migrated_connection, session_id, "Oi", "Qual é a sua prova?")
    append_exchange(migrated_connection, session_id, "Maratona", "Quando?")

    assert session_messages(migrated_connection, session_id) == (
        SessionMessage("user", "Oi"),
        SessionMessage("assistant", "Qual é a sua prova?"),
        SessionMessage("user", "Maratona"),
        SessionMessage("assistant", "Quando?"),
    )


def test_messages_of_other_sessions_and_weeks_are_left_out(
    migrated_connection: sqlite3.Connection,
) -> None:
    migrated_connection.execute(
        "INSERT INTO message (role, content, created_at) VALUES ('user', 'x', ?)",
        (_CREATED_AT,),
    )
    session_id = open_session(migrated_connection)

    assert session_messages(migrated_connection, session_id) == ()


def test_append_to_a_missing_session_is_refused(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(OnboardingSessionError, match="session 99 is missing"):
        append_exchange(migrated_connection, 99, "Oi", "Olá")

    assert migrated_connection.execute("SELECT COUNT(*) FROM message").fetchone() == (
        0,
    )


def test_discard_closes_the_open_session_and_keeps_its_messages(
    migrated_connection: sqlite3.Connection,
) -> None:
    session_id = open_session(migrated_connection)
    append_exchange(migrated_connection, session_id, "Oi", "Qual é a sua prova?")

    assert discard_open_session(migrated_connection) == session_id

    assert current_open_session(migrated_connection) is None
    assert len(session_messages(migrated_connection, session_id)) == 2
    assert migrated_connection.execute(
        "SELECT status, discarded_at IS NOT NULL, completed_at, macro_plan_id "
        "FROM onboarding_session WHERE id = ?",
        (session_id,),
    ).fetchone() == ("discarded", 1, None, None)
    assert open_session(migrated_connection) != session_id


def test_discard_without_an_open_session_gives_none(
    migrated_connection: sqlite3.Connection,
) -> None:
    assert discard_open_session(migrated_connection) is None


def test_discarded_session_needs_its_timestamp_and_no_macro_plan(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_connection.execute(
            "INSERT INTO onboarding_session (status, started_at) "
            "VALUES ('discarded', ?)",
            (_CREATED_AT,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        migrated_connection.execute(
            "INSERT INTO onboarding_session "
            "(status, started_at, discarded_at, macro_plan_id) "
            "VALUES ('discarded', ?, ?, ?)",
            (_CREATED_AT, _CREATED_AT, _insert_macro(migrated_connection)),
        )


def test_failed_exchange_stores_neither_turn(
    migrated_connection: sqlite3.Connection,
) -> None:
    session_id = open_session(migrated_connection)

    with pytest.raises(sqlite3.IntegrityError):
        append_exchange(migrated_connection, session_id, "Oi", "")

    assert session_messages(migrated_connection, session_id) == ()
