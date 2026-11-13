"""
Table names, fixed timestamps and one valid row per table.

Shared by the schema and migration tests.
"""

import sqlite3

_CREATED_AT = "2026-10-26T00:00:00Z"
_WEEK_START = "2026-10-26"
_WEEK_END = "2026-11-01"
_RACE_DATE = "2027-01-01"
_TABLE_NAMES = (
    "profile",
    "macro_plan",
    "macro_revision",
    "weekly_plan",
    "workout",
    "activity",
    "wellness",
    "checkin",
    "message",
    "coach_summary",
    "onboarding_session",
    "cycle_run",
)


def _inserted_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise AssertionError("insert did not return a row id")
    return cursor.lastrowid


def _insert_macro(
    connection: sqlite3.Connection,
    *,
    status: str = "active",
    start_date: str = _WEEK_START,
    race_date: str = _RACE_DATE,
    archived_at: str | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO macro_plan "
        "(status, start_date, race_date, plan_json, created_at, archived_at) "
        "VALUES (?, ?, ?, '{}', ?, ?)",
        (status, start_date, race_date, _CREATED_AT, archived_at),
    )
    return _inserted_id(cursor)


def _insert_weekly_plan(
    connection: sqlite3.Connection,
    macro_plan_id: int,
    *,
    week_start: str = _WEEK_START,
    week_end: str = _WEEK_END,
    raw_json: str = "{}",
) -> int:
    cursor = connection.execute(
        "INSERT INTO weekly_plan "
        "(macro_plan_id, week_start, week_end, raw_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (macro_plan_id, week_start, week_end, raw_json, _CREATED_AT, _CREATED_AT),
    )
    return _inserted_id(cursor)


def _insert_workout(
    connection: sqlite3.Connection,
    weekly_plan_id: int,
    *,
    name: str = "26/10 Easy",
    garmin_workout_id: str | int = "workout-1",
) -> None:
    connection.execute(
        "INSERT INTO workout "
        "(weekly_plan_id, schedule_date, name, garmin_workout_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (weekly_plan_id, _WEEK_START, name, garmin_workout_id, _CREATED_AT),
    )


def _insert_activity(
    connection: sqlite3.Connection,
    *,
    garmin_activity_id: str = "activity-1",
    sport: str = "running",
    raw_json: str | None = "{}",
) -> None:
    connection.execute(
        "INSERT INTO activity "
        "(garmin_activity_id, sport, activity_type, start_time, activity_date, "
        "raw_json, created_at) VALUES (?, ?, 'road_run', ?, ?, ?, ?)",
        (
            garmin_activity_id,
            sport,
            _CREATED_AT,
            _WEEK_START,
            raw_json,
            _CREATED_AT,
        ),
    )


def _insert_wellness(
    connection: sqlite3.Connection,
    *,
    wellness_date: str = _WEEK_START,
    raw_json: str | None = "{}",
) -> None:
    connection.execute(
        "INSERT INTO wellness "
        "(wellness_date, raw_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (wellness_date, raw_json, _CREATED_AT, _CREATED_AT),
    )


def _insert_checkin(
    connection: sqlite3.Connection,
    *,
    fatigue: int = 5,
    has_pain: int = 0,
    pain_location: str | None = None,
    pain_level: int | None = None,
    free_text: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO checkin "
        "(week_start, fatigue, has_pain, pain_location, pain_level, free_text, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _WEEK_START,
            fatigue,
            has_pain,
            pain_location,
            pain_level,
            free_text,
            _CREATED_AT,
        ),
    )
