"""Tests for the onboarding write, explicit revisions and the macro history."""

import json
import sqlite3
from datetime import date

import pytest

from onboarding_rows import (
    CLOSING_EXCHANGE,
    RACE_DAY,
    REASON,
    valid_macro_plan,
    valid_result,
)
from schema_rows import _CREATED_AT
from training_season.coach_context_store import read_active_macro, read_profile_json
from training_season.macro_store import (
    MacroHistoryEntry,
    RevisionReasonError,
    read_macro_history,
    read_macro_plan_json,
    revise_active_macro,
    store_onboarding_outcome,
)
from training_season.onboarding_store import (
    OnboardingSessionError,
    append_exchange,
    current_open_session,
    open_session,
    session_messages,
)
from training_season.push_store import NoActiveMacroPlanError

NEW_RACE_DAY = date(2027, 5, 30)
NEW_REASON = "Mudei a prova para maio."


def _complete_onboarding(
    connection: sqlite3.Connection,
    *,
    race_date: date = RACE_DAY,
    reason: str = "Primeira maratona.",
) -> int:
    return store_onboarding_outcome(
        connection,
        session_id=open_session(connection),
        result=valid_result(race_date=race_date, reason=reason),
        plan=valid_macro_plan(race_date=race_date),
        final_exchange=CLOSING_EXCHANGE,
    )


def _macro_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return connection.execute(
        "SELECT id, status, archived_at IS NOT NULL, race_date, plan_json "
        "FROM macro_plan ORDER BY id"
    ).fetchall()


def _revision_rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return connection.execute(
        "SELECT macro_plan_id, revision_number, reason FROM macro_revision "
        "ORDER BY macro_plan_id, revision_number"
    ).fetchall()


def test_first_onboarding_writes_profile_macro_revision_and_session(
    migrated_connection: sqlite3.Connection,
) -> None:
    session_id = open_session(migrated_connection)

    macro_plan_id = store_onboarding_outcome(
        migrated_connection,
        session_id=session_id,
        result=valid_result(),
        plan=valid_macro_plan(),
        final_exchange=CLOSING_EXCHANGE,
    )

    assert [m.content for m in session_messages(migrated_connection, session_id)] == [
        CLOSING_EXCHANGE.user_text,
        CLOSING_EXCHANGE.assistant_text,
    ]
    profile_json = read_profile_json(migrated_connection)
    assert profile_json is not None
    assert json.loads(profile_json)["athlete_name"] == "Gabi"
    active = read_active_macro(migrated_connection)
    assert active is not None
    assert (active.macro_plan_id, active.race_date) == (macro_plan_id, "2027-04-18")
    assert _revision_rows(migrated_connection) == [(macro_plan_id, 1, REASON)]
    assert current_open_session(migrated_connection) is None
    assert migrated_connection.execute(
        "SELECT status, macro_plan_id FROM onboarding_session WHERE id = ?",
        (session_id,),
    ).fetchone() == ("completed", macro_plan_id)


def test_reopen_archives_the_old_macro_and_keeps_its_plan(
    migrated_connection: sqlite3.Connection,
) -> None:
    old_id = _complete_onboarding(migrated_connection)
    old_plan_json = read_macro_plan_json(migrated_connection, old_id)
    profile_created_at = migrated_connection.execute(
        "SELECT created_at FROM profile"
    ).fetchone()

    new_id = _complete_onboarding(
        migrated_connection, race_date=NEW_RACE_DAY, reason=NEW_REASON
    )

    assert _macro_rows(migrated_connection) == [
        (old_id, "archived", 1, "2027-04-18", old_plan_json),
        (
            new_id,
            "active",
            0,
            "2027-05-30",
            read_macro_plan_json(migrated_connection, new_id),
        ),
    ]
    assert _revision_rows(migrated_connection) == [
        (old_id, 1, "Primeira maratona."),
        (new_id, 1, NEW_REASON),
    ]
    profile_json = read_profile_json(migrated_connection)
    assert profile_json is not None
    assert json.loads(profile_json)["goal"]["race_date"] == "2027-05-30"
    assert (
        migrated_connection.execute("SELECT created_at FROM profile").fetchone()
        == profile_created_at
    )


def test_failed_step_rolls_back_the_whole_outcome(
    migrated_connection: sqlite3.Connection,
) -> None:
    old_id = _complete_onboarding(migrated_connection)
    profile_before = read_profile_json(migrated_connection)
    session_id = open_session(migrated_connection)
    migrated_connection.execute(
        "CREATE TEMP TRIGGER refuse_revision BEFORE INSERT ON macro_revision "
        "BEGIN SELECT RAISE(ABORT, 'revision refused by test'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="revision refused by test"):
        store_onboarding_outcome(
            migrated_connection,
            session_id=session_id,
            result=valid_result(race_date=NEW_RACE_DAY),
            plan=valid_macro_plan(race_date=NEW_RACE_DAY),
            final_exchange=CLOSING_EXCHANGE,
        )

    active = read_active_macro(migrated_connection)
    assert active is not None
    assert active.macro_plan_id == old_id
    assert len(_macro_rows(migrated_connection)) == 1
    assert read_profile_json(migrated_connection) == profile_before
    assert current_open_session(migrated_connection) == session_id
    assert session_messages(migrated_connection, session_id) == ()


def test_session_that_is_not_open_cannot_complete(
    migrated_connection: sqlite3.Connection,
) -> None:
    old_id = _complete_onboarding(migrated_connection)
    completed_session = migrated_connection.execute(
        "SELECT id FROM onboarding_session"
    ).fetchone()[0]

    with pytest.raises(
        OnboardingSessionError, match=f"session {completed_session} is completed"
    ):
        store_onboarding_outcome(
            migrated_connection,
            session_id=completed_session,
            result=valid_result(),
            plan=valid_macro_plan(),
            final_exchange=CLOSING_EXCHANGE,
        )

    assert [row[0] for row in _macro_rows(migrated_connection)] == [old_id]


def test_completed_session_takes_no_more_turns(
    migrated_connection: sqlite3.Connection,
) -> None:
    _complete_onboarding(migrated_connection)

    with pytest.raises(OnboardingSessionError, match="session 1 is completed"):
        append_exchange(migrated_connection, 1, "Oi", "Olá")


def test_revisions_are_numbered_from_two(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _complete_onboarding(migrated_connection)

    second = revise_active_macro(
        migrated_connection, valid_macro_plan(), "Ajuste após dor"
    )
    third = revise_active_macro(
        migrated_connection, valid_macro_plan(race_date=NEW_RACE_DAY), NEW_REASON
    )

    assert (second, third) == (2, 3)
    assert _revision_rows(migrated_connection) == [
        (macro_plan_id, 1, "Primeira maratona."),
        (macro_plan_id, 2, "Ajuste após dor"),
        (macro_plan_id, 3, NEW_REASON),
    ]
    active = read_active_macro(migrated_connection)
    assert active is not None
    assert active.race_date == "2027-05-30"
    assert json.loads(active.plan_json)["race_date"] == "2027-05-30"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("", "at least 1 character"),
        ("  \n", "at least 1 character"),
        ("x" * 501, "at most 500 characters"),
        ("Ajuste\x1b]52;c;x\x07", "U\\+001B is not allowed"),
    ],
)
def test_invalid_revision_reason_is_rejected(
    migrated_connection: sqlite3.Connection, reason: str, expected: str
) -> None:
    _complete_onboarding(migrated_connection)

    with pytest.raises(RevisionReasonError, match=expected):
        revise_active_macro(migrated_connection, valid_macro_plan(), reason)

    assert len(_revision_rows(migrated_connection)) == 1


def test_revision_reason_is_stored_trimmed_with_its_lines(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _complete_onboarding(migrated_connection)

    revise_active_macro(migrated_connection, valid_macro_plan(), "  Dor.\nAjuste. ")

    assert _revision_rows(migrated_connection)[-1] == (
        macro_plan_id,
        2,
        "Dor.\nAjuste.",
    )


class TickingUtcClock:
    """Gives a later second on every call, so two calls never match."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"2099-01-01T00:00:{self.calls:02d}Z"


def test_onboarding_outcome_uses_one_timestamp(
    migrated_connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _complete_onboarding(migrated_connection)
    monkeypatch.setattr("training_season.macro_store.utc_now", TickingUtcClock())

    new_id = _complete_onboarding(migrated_connection, race_date=NEW_RACE_DAY)

    stamps = migrated_connection.execute(
        "SELECT old.archived_at, new.created_at, revision.created_at, "
        "session.completed_at FROM macro_plan AS old, macro_plan AS new, "
        "macro_revision AS revision, onboarding_session AS session "
        "WHERE old.id = 1 AND new.id = ? AND revision.macro_plan_id = new.id "
        "AND session.macro_plan_id = new.id",
        (new_id,),
    ).fetchone()
    assert stamps == ("2099-01-01T00:00:01Z",) * 4


def test_revision_needs_an_active_macro(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(NoActiveMacroPlanError):
        revise_active_macro(migrated_connection, valid_macro_plan(), "Ajuste")


def test_history_lists_archived_and_active_macros_with_reasons(
    migrated_connection: sqlite3.Connection,
) -> None:
    old_id = _complete_onboarding(migrated_connection)
    new_id = _complete_onboarding(
        migrated_connection, race_date=NEW_RACE_DAY, reason=NEW_REASON
    )

    history = read_macro_history(migrated_connection)

    assert [entry.macro_plan_id for entry in history] == [old_id, new_id]
    assert [entry.status for entry in history] == ["archived", "active"]
    assert history[0].archived_at is not None
    assert history[1].archived_at is None
    assert [entry.race_date for entry in history] == ["2027-04-18", "2027-05-30"]
    assert [
        [(revision.revision_number, revision.reason) for revision in entry.revisions]
        for entry in history
    ] == [[(1, "Primeira maratona.")], [(1, NEW_REASON)]]


def test_history_entry_shape(migrated_connection: sqlite3.Connection) -> None:
    migrated_connection.execute(
        "INSERT INTO macro_plan (status, start_date, race_date, plan_json, created_at) "
        "VALUES ('active', '2026-11-10', '2027-04-18', '{}', ?)",
        (_CREATED_AT,),
    )

    assert read_macro_history(migrated_connection) == (
        MacroHistoryEntry(
            macro_plan_id=1,
            status="active",
            start_date="2026-11-10",
            race_date="2027-04-18",
            created_at=_CREATED_AT,
            archived_at=None,
            revisions=(),
        ),
    )


def test_plan_json_of_an_unknown_macro_is_none(
    migrated_connection: sqlite3.Connection,
) -> None:
    assert read_macro_plan_json(migrated_connection, 42) is None
