"""Tests for the timestamp and date column CHECKs on every table."""

import sqlite3

import pytest

from schema_rows import (
    _CREATED_AT,
    _RACE_DATE,
    _WEEK_END,
    _WEEK_START,
    _insert_macro,
    _insert_weekly_plan,
)

_TIMESTAMP_COLUMNS = (
    ("profile", "created_at"),
    ("profile", "updated_at"),
    ("macro_plan", "created_at"),
    ("macro_plan", "archived_at"),
    ("macro_revision", "created_at"),
    ("weekly_plan", "created_at"),
    ("weekly_plan", "updated_at"),
    ("workout", "created_at"),
    ("activity", "start_time"),
    ("activity", "created_at"),
    ("wellness", "created_at"),
    ("wellness", "updated_at"),
    ("checkin", "created_at"),
    ("message", "created_at"),
    ("coach_summary", "created_at"),
)
_INVALID_TIMESTAMPS = (
    "2026-10-26T00:00:00+00:00",
    "2026-10-26T00:00:00-03:00",
    "2026-10-26T00:00:00.123Z",
    "2026-10-26 00:00:00Z",
    "2026-10-26T00:00:00",
    "2026-10-26T25:00:00Z",
)
_DATE_COLUMNS = (
    ("macro_plan", "start_date"),
    ("macro_plan", "race_date"),
    ("weekly_plan", "week_start"),
    ("weekly_plan", "week_end"),
    ("workout", "schedule_date"),
    ("activity", "activity_date"),
    ("wellness", "wellness_date"),
    ("checkin", "week_start"),
)
_INVALID_DATES = (
    "2026-02-30",
    "2026-2-3",
    "2026-10-26T00:00",
    "2026-10-26 ",
)


def _insert_with_invalid_timestamp(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    timestamp: str,
) -> None:
    if table == "profile":
        created_at = timestamp if column == "created_at" else _CREATED_AT
        updated_at = timestamp if column == "updated_at" else _CREATED_AT
        connection.execute(
            "INSERT INTO profile (id, data_json, created_at, updated_at) "
            "VALUES (1, '{}', ?, ?)",
            (created_at, updated_at),
        )
    elif table == "macro_plan":
        archived = column == "archived_at"
        created_at = timestamp if column == "created_at" else _CREATED_AT
        archived_at = timestamp if archived else None
        status = "archived" if archived else "active"
        connection.execute(
            "INSERT INTO macro_plan "
            "(status, start_date, race_date, plan_json, created_at, archived_at) "
            "VALUES (?, ?, ?, '{}', ?, ?)",
            (status, _WEEK_START, _RACE_DATE, created_at, archived_at),
        )
    elif table == "macro_revision":
        macro_plan_id = _insert_macro(connection)
        created_at = timestamp if column == "created_at" else _CREATED_AT
        connection.execute(
            "INSERT INTO macro_revision "
            "(macro_plan_id, revision_number, reason, plan_json, created_at) "
            "VALUES (?, 1, 'test revision', '{}', ?)",
            (macro_plan_id, created_at),
        )
    elif table == "weekly_plan":
        macro_plan_id = _insert_macro(connection)
        created_at = timestamp if column == "created_at" else _CREATED_AT
        updated_at = timestamp if column == "updated_at" else _CREATED_AT
        connection.execute(
            "INSERT INTO weekly_plan "
            "(macro_plan_id, week_start, week_end, raw_json, created_at, updated_at) "
            "VALUES (?, ?, ?, '{}', ?, ?)",
            (macro_plan_id, _WEEK_START, _WEEK_END, created_at, updated_at),
        )
    elif table == "workout":
        macro_plan_id = _insert_macro(connection)
        weekly_plan_id = _insert_weekly_plan(connection, macro_plan_id)
        created_at = timestamp if column == "created_at" else _CREATED_AT
        connection.execute(
            "INSERT INTO workout "
            "(weekly_plan_id, schedule_date, name, garmin_workout_id, created_at) "
            "VALUES (?, ?, '26/10 Easy', 'workout-1', ?)",
            (weekly_plan_id, _WEEK_START, created_at),
        )
    elif table == "activity":
        start_time = timestamp if column == "start_time" else _CREATED_AT
        created_at = timestamp if column == "created_at" else _CREATED_AT
        connection.execute(
            "INSERT INTO activity "
            "(garmin_activity_id, sport, activity_type, start_time, activity_date, "
            "raw_json, created_at) "
            "VALUES ('activity-1', 'running', 'road_run', ?, ?, '{}', ?)",
            (start_time, _WEEK_START, created_at),
        )
    elif table == "wellness":
        created_at = timestamp if column == "created_at" else _CREATED_AT
        updated_at = timestamp if column == "updated_at" else _CREATED_AT
        connection.execute(
            "INSERT INTO wellness "
            "(wellness_date, raw_json, created_at, updated_at) "
            "VALUES (?, '{}', ?, ?)",
            (_WEEK_START, created_at, updated_at),
        )
    elif table == "checkin":
        created_at = timestamp if column == "created_at" else _CREATED_AT
        connection.execute(
            "INSERT INTO checkin (week_start, fatigue, has_pain, created_at) "
            "VALUES (?, 5, 0, ?)",
            (_WEEK_START, created_at),
        )
    elif table == "message":
        created_at = timestamp if column == "created_at" else _CREATED_AT
        connection.execute(
            "INSERT INTO message (role, content, created_at) "
            "VALUES ('user', 'test message', ?)",
            (created_at,),
        )
    elif table == "coach_summary":
        created_at = timestamp if column == "created_at" else _CREATED_AT
        connection.execute(
            "INSERT INTO coach_summary (summary, created_at) VALUES ('test', ?)",
            (created_at,),
        )
    else:
        raise AssertionError(f"unrecognized table {table}")


def _insert_with_invalid_date(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    date_value: str,
) -> None:
    if table == "macro_plan":
        start_date = date_value if column == "start_date" else _WEEK_START
        race_date = date_value if column == "race_date" else _RACE_DATE
        connection.execute(
            "INSERT INTO macro_plan "
            "(status, start_date, race_date, plan_json, created_at, archived_at) "
            "VALUES ('active', ?, ?, '{}', ?, NULL)",
            (start_date, race_date, _CREATED_AT),
        )
    elif table == "weekly_plan":
        macro_plan_id = _insert_macro(connection)
        week_start = date_value if column == "week_start" else _WEEK_START
        week_end = date_value if column == "week_end" else _WEEK_END
        connection.execute(
            "INSERT INTO weekly_plan "
            "(macro_plan_id, week_start, week_end, raw_json, created_at, updated_at) "
            "VALUES (?, ?, ?, '{}', ?, ?)",
            (macro_plan_id, week_start, week_end, _CREATED_AT, _CREATED_AT),
        )
    elif table == "workout":
        macro_plan_id = _insert_macro(connection)
        weekly_plan_id = _insert_weekly_plan(connection, macro_plan_id)
        connection.execute(
            "INSERT INTO workout "
            "(weekly_plan_id, schedule_date, name, garmin_workout_id, created_at) "
            "VALUES (?, ?, '26/10 Easy', 'workout-1', ?)",
            (weekly_plan_id, date_value, _CREATED_AT),
        )
    elif table == "activity":
        connection.execute(
            "INSERT INTO activity "
            "(garmin_activity_id, sport, activity_type, start_time, activity_date, "
            "raw_json, created_at) "
            "VALUES ('activity-1', 'running', 'road_run', ?, ?, '{}', ?)",
            (_CREATED_AT, date_value, _CREATED_AT),
        )
    elif table == "wellness":
        connection.execute(
            "INSERT INTO wellness "
            "(wellness_date, raw_json, created_at, updated_at) "
            "VALUES (?, '{}', ?, ?)",
            (date_value, _CREATED_AT, _CREATED_AT),
        )
    elif table == "checkin":
        connection.execute(
            "INSERT INTO checkin (week_start, fatigue, has_pain, created_at) "
            "VALUES (?, 5, 0, ?)",
            (date_value, _CREATED_AT),
        )
    else:
        raise AssertionError(f"unrecognized date table {table}")


@pytest.mark.parametrize(("table", "column"), _TIMESTAMP_COLUMNS)
@pytest.mark.parametrize("timestamp", _INVALID_TIMESTAMPS)
def test_timestamp_columns_reject_bad_format(
    migrated_connection: sqlite3.Connection,
    table: str,
    column: str,
    timestamp: str,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_with_invalid_timestamp(
            migrated_connection,
            table,
            column,
            timestamp,
        )


@pytest.mark.parametrize(("table", "column"), _DATE_COLUMNS)
@pytest.mark.parametrize("date_value", _INVALID_DATES)
def test_date_columns_reject_bad_dates(
    migrated_connection: sqlite3.Connection,
    table: str,
    column: str,
    date_value: str,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_with_invalid_date(
            migrated_connection,
            table,
            column,
            date_value,
        )
