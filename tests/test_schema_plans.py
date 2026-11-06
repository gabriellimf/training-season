"""Tests for macro_plan, macro_revision, weekly_plan and workout constraints."""

import sqlite3

import pytest

from schema_rows import (
    _CREATED_AT,
    _RACE_DATE,
    _WEEK_START,
    _insert_macro,
    _insert_weekly_plan,
    _insert_workout,
)


def test_macro_plan_allows_one_active(
    migrated_connection: sqlite3.Connection,
) -> None:
    _insert_macro(migrated_connection)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_macro(migrated_connection)


def test_macro_plan_archived_requires_archived_at(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_macro(migrated_connection, status="archived")


def test_macro_plan_race_date_not_before_start(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_macro(
            migrated_connection,
            start_date=_RACE_DATE,
            race_date=_WEEK_START,
        )


def test_archived_macro_stays_readable(
    migrated_connection: sqlite3.Connection,
) -> None:
    archived_macro_id = _insert_macro(migrated_connection)
    archived_week_id = _insert_weekly_plan(
        migrated_connection,
        archived_macro_id,
    )
    migrated_connection.execute(
        "UPDATE macro_plan SET status = 'archived', archived_at = ? WHERE id = ?",
        (_CREATED_AT, archived_macro_id),
    )
    active_macro_id = _insert_macro(migrated_connection)
    active_week_id = _insert_weekly_plan(
        migrated_connection,
        active_macro_id,
    )

    macro_rows = migrated_connection.execute(
        "SELECT id, status FROM macro_plan ORDER BY id"
    ).fetchall()
    week_rows = migrated_connection.execute(
        "SELECT id, macro_plan_id, week_start FROM weekly_plan ORDER BY id"
    ).fetchall()

    assert macro_rows == [
        (archived_macro_id, "archived"),
        (active_macro_id, "active"),
    ]
    assert week_rows == [
        (archived_week_id, archived_macro_id, _WEEK_START),
        (active_week_id, active_macro_id, _WEEK_START),
    ]


@pytest.mark.parametrize("reason", ["", "   "])
def test_macro_revision_requires_reason(
    migrated_connection: sqlite3.Connection,
    reason: str,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)

    with pytest.raises(sqlite3.IntegrityError):
        migrated_connection.execute(
            "INSERT INTO macro_revision "
            "(macro_plan_id, revision_number, reason, plan_json, created_at) "
            "VALUES (?, 1, ?, '{}', ?)",
            (macro_plan_id, reason, _CREATED_AT),
        )


def test_macro_revision_number_unique_per_macro(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)
    insert_revision = (
        "INSERT INTO macro_revision "
        "(macro_plan_id, revision_number, reason, plan_json, created_at) "
        "VALUES (?, 1, 'test revision', '{}', ?)"
    )
    migrated_connection.execute(insert_revision, (macro_plan_id, _CREATED_AT))

    with pytest.raises(sqlite3.IntegrityError):
        migrated_connection.execute(insert_revision, (macro_plan_id, _CREATED_AT))


def test_macro_revision_requires_existing_macro(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated_connection.execute(
            "INSERT INTO macro_revision "
            "(macro_plan_id, revision_number, reason, plan_json, created_at) "
            "VALUES (999, 1, 'test revision', '{}', ?)",
            (_CREATED_AT,),
        )


def test_weekly_plan_unique_week_start_per_macro(
    migrated_connection: sqlite3.Connection,
) -> None:
    first_macro_id = _insert_macro(migrated_connection)
    second_macro_id = _insert_macro(
        migrated_connection,
        status="archived",
        archived_at=_CREATED_AT,
    )
    _insert_weekly_plan(migrated_connection, first_macro_id)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_weekly_plan(migrated_connection, first_macro_id)

    _insert_weekly_plan(migrated_connection, second_macro_id)


def test_weekly_plan_requires_existing_macro(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_weekly_plan(migrated_connection, 999)


def test_macro_with_weeks_cannot_be_deleted(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)
    _insert_weekly_plan(migrated_connection, macro_plan_id)

    with pytest.raises(sqlite3.IntegrityError):
        migrated_connection.execute(
            "DELETE FROM macro_plan WHERE id = ?",
            (macro_plan_id,),
        )


def test_weekly_plan_span_at_most_seven_days(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_weekly_plan(
            migrated_connection,
            macro_plan_id,
            week_end="2026-11-02",
        )


def test_weekly_plan_requires_valid_json(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_weekly_plan(
            migrated_connection,
            macro_plan_id,
            raw_json="not-json",
        )


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("26/10 abcdefghi", True),
        ("26/10 abcdefghij", False),
        ("", False),
        ("26/10 leve é", False),
        ("26/10\t10x400", False),
    ],
)
def test_workout_name_rules(
    migrated_connection: sqlite3.Connection,
    name: str,
    valid: bool,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)
    weekly_plan_id = _insert_weekly_plan(migrated_connection, macro_plan_id)

    if valid:
        _insert_workout(migrated_connection, weekly_plan_id, name=name)
    else:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_workout(migrated_connection, weekly_plan_id, name=name)


def test_workout_name_unique_within_week(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)
    weekly_plan_id = _insert_weekly_plan(migrated_connection, macro_plan_id)
    _insert_workout(migrated_connection, weekly_plan_id, garmin_workout_id="workout-1")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_workout(
            migrated_connection,
            weekly_plan_id,
            garmin_workout_id="workout-2",
        )


def test_workout_garmin_ids_unique(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)
    weekly_plan_id = _insert_weekly_plan(migrated_connection, macro_plan_id)
    _insert_workout(
        migrated_connection,
        weekly_plan_id,
        name="26/10 One",
        garmin_workout_id=123,
    )

    with pytest.raises(sqlite3.IntegrityError):
        _insert_workout(
            migrated_connection,
            weekly_plan_id,
            name="26/10 Two",
            garmin_workout_id="123",
        )


def test_weekly_plan_with_workouts_cannot_be_deleted(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_plan_id = _insert_macro(migrated_connection)
    weekly_plan_id = _insert_weekly_plan(migrated_connection, macro_plan_id)
    _insert_workout(migrated_connection, weekly_plan_id)

    with pytest.raises(sqlite3.IntegrityError):
        migrated_connection.execute(
            "DELETE FROM weekly_plan WHERE id = ?",
            (weekly_plan_id,),
        )
