"""Tests for migration loading, atomicity, and rollback."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from schema_rows import _CREATED_AT, _TABLE_NAMES, _WEEK_END, _WEEK_START
from training_season.database import (
    Migration,
    MigrationError,
    connect,
    load_migrations,
    migrate,
)


def _schema_snapshot(
    connection: sqlite3.Connection,
) -> list[tuple[str | None, str | None, str | None]]:
    return [
        (row[0], row[1], row[2])
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY name"
        )
    ]


def _row_snapshot(
    connection: sqlite3.Connection,
) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: connection.execute(
            f"SELECT * FROM {table} ORDER BY 1"  # noqa: S608
        ).fetchall()
        for table in _TABLE_NAMES
    }


def _database_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _insert_sample_rows(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO profile (id, data_json, created_at, updated_at) "
        "VALUES (1, '{}', ?, ?)",
        (_CREATED_AT, _CREATED_AT),
    )
    macro_id = connection.execute(
        "INSERT INTO macro_plan "
        "(status, start_date, race_date, plan_json, created_at, archived_at) "
        "VALUES ('active', ?, '2027-01-01', '{}', ?, NULL)",
        (_WEEK_START, _CREATED_AT),
    ).lastrowid
    assert macro_id is not None
    connection.execute(
        "INSERT INTO macro_revision "
        "(macro_plan_id, revision_number, reason, plan_json, created_at) "
        "VALUES (?, 1, 'test revision', '{}', ?)",
        (macro_id, _CREATED_AT),
    )
    weekly_plan_id = connection.execute(
        "INSERT INTO weekly_plan "
        "(macro_plan_id, week_start, week_end, raw_json, created_at, updated_at) "
        "VALUES (?, ?, ?, '{}', ?, ?)",
        (macro_id, _WEEK_START, _WEEK_END, _CREATED_AT, _CREATED_AT),
    ).lastrowid
    assert weekly_plan_id is not None
    connection.execute(
        "INSERT INTO workout "
        "(weekly_plan_id, schedule_date, name, garmin_workout_id, "
        "garmin_schedule_id, created_at) "
        "VALUES (?, ?, '26/10 Easy', 'workout-1', 'schedule-1', ?)",
        (weekly_plan_id, _WEEK_START, _CREATED_AT),
    )
    connection.execute(
        "INSERT INTO activity "
        "(garmin_activity_id, sport, activity_type, start_time, activity_date, "
        "raw_json, created_at) "
        "VALUES ('activity-1', 'running', 'road_run', ?, ?, '{}', ?)",
        (_CREATED_AT, _WEEK_START, _CREATED_AT),
    )
    connection.execute(
        "INSERT INTO wellness "
        "(wellness_date, raw_json, created_at, updated_at) VALUES (?, '{}', ?, ?)",
        (_WEEK_START, _CREATED_AT, _CREATED_AT),
    )
    connection.execute(
        "INSERT INTO checkin "
        "(week_start, fatigue, has_pain, created_at) VALUES (?, 5, 0, ?)",
        (_WEEK_START, _CREATED_AT),
    )
    connection.execute(
        "INSERT INTO message (role, content, weekly_plan_id, created_at) "
        "VALUES ('assistant', 'test plan', ?, ?)",
        (weekly_plan_id, _CREATED_AT),
    )
    connection.execute(
        "INSERT INTO coach_summary (summary, created_at) VALUES ('test summary', ?)",
        (_CREATED_AT,),
    )


def test_shipped_migrations_load_in_order() -> None:
    migrations = load_migrations()

    assert [migration.version for migration in migrations] == list(
        range(1, len(migrations) + 1)
    )
    assert migrations[0].name == "0001_initial.sql"


def test_load_migrations_rejects_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="no SQL migration files"):
        load_migrations(tmp_path)


def test_load_migrations_rejects_bad_file_name(tmp_path: Path) -> None:
    (tmp_path / "initial.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationError, match="invalid migration filename"):
        load_migrations(tmp_path)


def test_load_migrations_rejects_duplicate_version(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_second.sql").write_text("SELECT 2;", encoding="utf-8")

    with pytest.raises(MigrationError, match="duplicate migration version"):
        load_migrations(tmp_path)


def test_load_migrations_rejects_gap(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0003_third.sql").write_text("SELECT 3;", encoding="utf-8")

    with pytest.raises(MigrationError, match="sequential"):
        load_migrations(tmp_path)


def test_load_migrations_rejects_empty_file(tmp_path: Path) -> None:
    (tmp_path / "0001_initial.sql").write_text(" \n\t", encoding="utf-8")

    with pytest.raises(MigrationError, match="empty"):
        load_migrations(tmp_path)


def test_migrate_fresh_database_creates_all_tables(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        migration_result = migrate(connection)
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        indexes = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        user_version = connection.execute("PRAGMA user_version").fetchone()

    assert tables == sorted(_TABLE_NAMES)
    assert indexes == [
        "activity_activity_date",
        "checkin_week_start",
        "macro_plan_single_active",
        "message_onboarding_session",
        "onboarding_session_single_open",
        "workout_schedule_date",
    ]
    assert user_version == (migration_result.current_version,)
    assert migration_result.applied == (1, 2, 3)


def test_migrate_twice_changes_nothing(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        first_result = migrate(connection)
        first_schema = _schema_snapshot(connection)
        first_version = connection.execute("PRAGMA user_version").fetchone()

    first_hash = _database_sha256(database_path)

    with closing(connect(database_path)) as connection:
        second_result = migrate(connection)
        second_schema = _schema_snapshot(connection)
        second_version = connection.execute("PRAGMA user_version").fetchone()

    second_hash = _database_sha256(database_path)

    assert first_result.applied == (1, 2, 3)
    assert second_result.applied == ()
    assert second_version == first_version
    assert second_schema == first_schema
    assert second_hash == first_hash


def test_migrate_twice_keeps_rows(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        migrate(connection)
        _insert_sample_rows(connection)
        rows_before = _row_snapshot(connection)
        second_result = migrate(connection)
        rows_after = _row_snapshot(connection)

    assert second_result.applied == ()
    assert rows_after == rows_before


def test_onboarding_session_migration_keeps_existing_messages(
    database_path: Path,
) -> None:
    with closing(connect(database_path)) as connection:
        migrate(connection, load_migrations()[:2])
        _insert_sample_rows(connection)
        migration_result = migrate(connection)
        messages = connection.execute(
            "SELECT role, content, onboarding_session_id FROM message"
        ).fetchall()
        dangling = connection.execute("PRAGMA foreign_key_check").fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO message (role, content, onboarding_session_id, "
                "created_at) VALUES ('user', 'x', 99, ?)",
                (_CREATED_AT,),
            )

    assert migration_result.applied == (3,)
    assert messages == [("assistant", "test plan", None)]
    assert dangling == []


def test_migrate_failure_rolls_back_whole_run(database_path: Path) -> None:
    migrations = (
        Migration(1, "0001_valid.sql", "CREATE TABLE created_first (value TEXT);"),
        Migration(2, "0002_failing.sql", "CREATE TABLE created_first (value TEXT);"),
    )

    statements: list[str] = []
    with closing(connect(database_path)) as connection:
        connection.set_trace_callback(statements.append)
        with pytest.raises(MigrationError, match=r"0002_failing\.sql") as raised:
            migrate(connection, migrations)
        connection.set_trace_callback(None)
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        user_version = connection.execute("PRAGMA user_version").fetchone()

    assert tables == []
    assert user_version == (0,)
    assert statements == [
        "BEGIN IMMEDIATE",
        "PRAGMA user_version",
        "CREATE TABLE created_first (value TEXT);",
        "PRAGMA user_version = 1",
        "ROLLBACK",
    ]
    assert str(raised.value) == (
        "migration 0002_failing.sql failed: table created_first already exists"
    )
    assert type(raised.value.__cause__) is sqlite3.OperationalError
    assert raised.value.__context__ is raised.value.__cause__


_SQL_A = "CREATE TABLE a (value TEXT);"
_SQL_B = "CREATE TABLE b (value TEXT);"


@pytest.mark.parametrize(
    ("migrations", "message"),
    [
        pytest.param((), "no migrations provided", id="empty"),
        pytest.param(
            (
                Migration(1, "0001_a.sql", _SQL_A),
                Migration(1, "0001_b.sql", _SQL_B),
            ),
            "duplicate migration version",
            id="duplicate",
        ),
        pytest.param(
            (
                Migration(3, "0003_b.sql", _SQL_B),
                Migration(1, "0001_a.sql", _SQL_A),
            ),
            "migration versions must be sequential from 1, found [3, 1]",
            id="gap",
        ),
    ],
)
def test_migrate_rejects_bad_caller_supplied_set(
    database_path: Path, migrations: tuple[Migration, ...], message: str
) -> None:
    with (
        closing(connect(database_path)) as connection,
        pytest.raises(MigrationError) as raised,
    ):
        migrate(connection, migrations)

    assert str(raised.value) == message


def test_migrate_sorts_caller_supplied_set(database_path: Path) -> None:
    migrations = (
        Migration(2, "0002_b.sql", _SQL_B),
        Migration(1, "0001_a.sql", _SQL_A),
    )

    with closing(connect(database_path)) as connection:
        migration_result = migrate(connection, migrations)

    assert migration_result.applied == (1, 2)
    assert migration_result.current_version == 2


def test_migrate_failure_keeps_previous_version(database_path: Path) -> None:
    first_migration = Migration(
        1,
        "0001_initial.sql",
        "CREATE TABLE original (value TEXT);",
    )
    failing_migration = Migration(
        2,
        "0002_failing.sql",
        "CREATE TABLE original (value TEXT);",
    )

    with closing(connect(database_path)) as connection:
        migrate(connection, (first_migration,))
        schema_before = _schema_snapshot(connection)

        with pytest.raises(MigrationError, match=r"0002_failing\.sql"):
            migrate(connection, (first_migration, failing_migration))

        schema_after = _schema_snapshot(connection)
        user_version = connection.execute("PRAGMA user_version").fetchone()

    assert schema_after == schema_before
    assert user_version == (1,)


def test_migrate_allows_add_column_on_a_strict_table(database_path: Path) -> None:
    migration = Migration(
        1,
        "0001_add_column.sql",
        "CREATE TABLE note (id INTEGER PRIMARY KEY) STRICT;\n"
        "CREATE TABLE tag (id INTEGER PRIMARY KEY) STRICT;\n"
        "ALTER TABLE note ADD COLUMN tag_id INTEGER REFERENCES tag (id);",
    )

    with closing(connect(database_path)) as connection:
        migration_result = migrate(connection, (migration,))
        columns = [row[1] for row in connection.execute("PRAGMA table_info(note)")]

    assert migration_result.applied == (1,)
    assert columns == ["id", "tag_id"]


@pytest.mark.parametrize(
    "control_statement",
    [
        "COMMIT;",
        "BEGIN;",
        "ROLLBACK;",
        "SAVEPOINT s;",
        "PRAGMA foreign_keys = OFF;",
        "PRAGMA user_version = 7;",
    ],
)
def test_migrate_rejects_transaction_control(
    database_path: Path,
    control_statement: str,
) -> None:
    migration = Migration(
        1,
        "0001_transaction_control.sql",
        f"CREATE TABLE rolled_back (value TEXT);\n{control_statement}",
    )

    with closing(connect(database_path)) as connection:
        with pytest.raises(
            MigrationError,
            match=r"0001_transaction_control\.sql",
        ):
            migrate(connection, (migration,))

        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        user_version = connection.execute("PRAGMA user_version").fetchone()

    assert tables == []
    assert user_version == (0,)


def test_migrate_refuses_newer_database(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        connection.execute("PRAGMA user_version = 99")
        with pytest.raises(MigrationError, match="newer than this code"):
            migrate(connection)

        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        user_version = connection.execute("PRAGMA user_version").fetchone()

    assert tables == []
    assert user_version == (99,)
