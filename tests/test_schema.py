"""
Tests for STRICT tables and the table constraints.

Covers profile, activity, wellness, checkin, message and coach_summary.
"""

import sqlite3

import pytest

from schema_rows import (
    _CREATED_AT,
    _TABLE_NAMES,
    _WEEK_START,
    _insert_activity,
    _insert_checkin,
    _insert_wellness,
)
from training_season.timeformat import utc_now


def test_all_tables_are_strict(migrated_connection: sqlite3.Connection) -> None:
    strict_tables = {
        row[1]: row[5]
        for row in migrated_connection.execute("PRAGMA table_list")
        if row[1] in _TABLE_NAMES
    }

    assert set(strict_tables) == set(_TABLE_NAMES)
    assert all(is_strict == 1 for is_strict in strict_tables.values())


def test_profile_allows_only_one_row(
    migrated_connection: sqlite3.Connection,
) -> None:
    migrated_connection.execute(
        "INSERT INTO profile (id, data_json, created_at, updated_at) "
        "VALUES (1, '{}', ?, ?)",
        (_CREATED_AT, _CREATED_AT),
    )

    with pytest.raises(sqlite3.IntegrityError):
        migrated_connection.execute(
            "INSERT INTO profile (id, data_json, created_at, updated_at) "
            "VALUES (2, '{}', ?, ?)",
            (_CREATED_AT, _CREATED_AT),
        )


@pytest.mark.parametrize("data_json", ["[]", "nope"])
def test_profile_requires_json_object(
    migrated_connection: sqlite3.Connection,
    data_json: str,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated_connection.execute(
            "INSERT INTO profile (id, data_json, created_at, updated_at) "
            "VALUES (1, ?, ?, ?)",
            (data_json, _CREATED_AT, _CREATED_AT),
        )


def test_activity_garmin_id_prevents_duplicates(
    migrated_connection: sqlite3.Connection,
) -> None:
    _insert_activity(migrated_connection)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_activity(migrated_connection)

    migrated_connection.execute(
        "INSERT INTO activity "
        "(garmin_activity_id, sport, activity_type, start_time, activity_date, "
        "raw_json, created_at) "
        "VALUES ('activity-1', 'running', 'road_run', ?, ?, '{}', ?) "
        "ON CONFLICT (garmin_activity_id) DO NOTHING",
        (_CREATED_AT, _WEEK_START, _CREATED_AT),
    )

    assert migrated_connection.execute(
        "SELECT count(*) FROM activity WHERE garmin_activity_id = 'activity-1'"
    ).fetchone() == (1,)


def test_activity_sport_limited_to_running_and_swimming(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_activity(migrated_connection, sport="cycling")


def test_wellness_one_row_per_date(
    migrated_connection: sqlite3.Connection,
) -> None:
    migrated_connection.execute(
        "INSERT INTO wellness "
        "(wellness_date, resting_hr, raw_json, created_at, updated_at) "
        "VALUES (?, 52, '{}', ?, ?)",
        (_WEEK_START, _CREATED_AT, _CREATED_AT),
    )
    migrated_connection.execute(
        "INSERT INTO wellness "
        "(wellness_date, resting_hr, raw_json, created_at, updated_at) "
        'VALUES (?, 49, \'{"source":"updated"}\', ?, ?) '
        "ON CONFLICT (wellness_date) DO UPDATE SET "
        "resting_hr = excluded.resting_hr, raw_json = excluded.raw_json, "
        "updated_at = excluded.updated_at",
        (_WEEK_START, _CREATED_AT, _CREATED_AT),
    )

    assert migrated_connection.execute(
        "SELECT count(*), resting_hr, raw_json FROM wellness WHERE wellness_date = ?",
        (_WEEK_START,),
    ).fetchone() == (1, 49.0, '{"source":"updated"}')


@pytest.mark.parametrize("fatigue", [0, 11])
def test_checkin_fatigue_range(
    migrated_connection: sqlite3.Connection,
    fatigue: int,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_checkin(migrated_connection, fatigue=fatigue)


@pytest.mark.parametrize(
    ("has_pain", "pain_location", "pain_level"),
    [
        (0, "knee", 3),
        (1, None, None),
        (1, "knee", None),
        (1, None, 3),
        (1, "   ", 3),
        (1, "knee", 0),
        (1, "knee", 11),
    ],
)
def test_checkin_pain_fields_follow_has_pain(
    migrated_connection: sqlite3.Connection,
    has_pain: int,
    pain_location: str | None,
    pain_level: int | None,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_checkin(
            migrated_connection,
            has_pain=has_pain,
            pain_location=pain_location,
            pain_level=pain_level,
        )


def test_message_role_values(migrated_connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated_connection.execute(
            "INSERT INTO message (role, content, created_at) "
            "VALUES ('coach', 'test message', ?)",
            (_CREATED_AT,),
        )


def test_message_weekly_plan_must_exist(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated_connection.execute(
            "INSERT INTO message (role, content, weekly_plan_id, created_at) "
            "VALUES ('assistant', 'test message', 999, ?)",
            (_CREATED_AT,),
        )


def test_coach_summary_appends_rows(
    migrated_connection: sqlite3.Connection,
) -> None:
    migrated_connection.execute(
        "INSERT INTO coach_summary (summary, created_at) VALUES ('first', ?)",
        (_CREATED_AT,),
    )
    migrated_connection.execute(
        "INSERT INTO coach_summary (summary, created_at) VALUES ('second', ?)",
        (_CREATED_AT,),
    )

    assert migrated_connection.execute(
        "SELECT summary FROM coach_summary ORDER BY id"
    ).fetchall() == [("first",), ("second",)]


def test_utc_now_is_accepted_by_schema(
    migrated_connection: sqlite3.Connection,
) -> None:
    created_at = utc_now()
    migrated_connection.execute(
        "INSERT INTO coach_summary (summary, created_at) VALUES ('current', ?)",
        (created_at,),
    )

    assert migrated_connection.execute(
        "SELECT created_at FROM coach_summary"
    ).fetchone() == (created_at,)


@pytest.mark.parametrize("table", ["activity", "wellness"])
@pytest.mark.parametrize("raw_json", ["[]", "nope", None])
def test_raw_json_requires_json_object(
    migrated_connection: sqlite3.Connection,
    table: str,
    raw_json: str | None,
) -> None:
    if table == "activity":
        with pytest.raises(sqlite3.IntegrityError):
            _insert_activity(migrated_connection, raw_json=raw_json)
    else:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_wellness(migrated_connection, raw_json=raw_json)
