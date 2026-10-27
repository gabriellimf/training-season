"""Tests for schema constraints and data invariants."""

import sqlite3

import pytest

from training_season.timeformat import utc_now

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


def test_all_tables_are_strict(migrated_connection: sqlite3.Connection) -> None:
    strict_tables = {
        row[1]: row[5]
        for row in migrated_connection.execute("PRAGMA table_list")
        if row[1] in _TABLE_NAMES
    }

    assert set(strict_tables) == set(_TABLE_NAMES)
    assert all(value == 1 for value in strict_tables.values())


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
