"""Tests for migration loading, atomicity, and concurrency."""

import hashlib
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from training_season.database import (
    Migration,
    MigrationError,
    MigrationResult,
    connect,
    load_migrations,
    migrate,
)

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

_CREATED_AT = "2026-10-26T00:00:00Z"
_WEEK_START = "2026-10-26"
_WEEK_END = "2026-11-01"


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
        result = migrate(connection)
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
    ]
    assert user_version == (result.current_version,)
    assert result.applied == (1,)


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

    assert first_result.applied == (1,)
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


def test_migrate_failure_rolls_back_whole_run(database_path: Path) -> None:
    migrations = (
        Migration(1, "0001_valid.sql", "CREATE TABLE created_first (value TEXT);"),
        Migration(2, "0002_failing.sql", "CREATE TABLE created_first (value TEXT);"),
    )

    with closing(connect(database_path)) as connection:
        with pytest.raises(MigrationError, match=r"0002_failing\.sql"):
            migrate(connection, migrations)
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        user_version = connection.execute("PRAGMA user_version").fetchone()

    assert tables == []
    assert user_version == (0,)


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


@pytest.mark.parametrize(
    "control_statement",
    [
        "COMMIT;",
        "BEGIN;",
        "ROLLBACK;",
        "SAVEPOINT s;",
        "PRAGMA foreign_keys = OFF;",
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


def test_concurrent_migrations_apply_once(database_path: Path) -> None:
    barrier = threading.Barrier(4)
    results: list[MigrationResult] = []
    errors: list[BaseException] = []

    def run_migration() -> None:
        try:
            with closing(connect(database_path)) as connection:
                barrier.wait(timeout=10)
                result = migrate(connection)
            results.append(result)
        except (
            OSError,
            RuntimeError,
            sqlite3.Error,
            MigrationError,
        ) as error:
            errors.append(error)

    threads = [threading.Thread(target=run_migration) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 4
    assert sum(result.applied == (1,) for result in results) == 1

    with closing(connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


def test_migrate_waits_for_write_lock(database_path: Path) -> None:
    lock_connection = connect(database_path)
    thread_started = threading.Event()
    results: list[MigrationResult] = []
    errors: list[BaseException] = []

    def run_migration() -> None:
        try:
            with closing(connect(database_path)) as connection:
                thread_started.set()
                results.append(migrate(connection))
        except (
            OSError,
            RuntimeError,
            sqlite3.Error,
            MigrationError,
        ) as error:
            errors.append(error)

    try:
        lock_connection.execute("BEGIN IMMEDIATE")
        thread = threading.Thread(target=run_migration)
        thread.start()
        assert thread_started.wait(timeout=10)
        time.sleep(0.1)
        assert thread.is_alive()
        lock_connection.execute("ROLLBACK")
        thread.join(timeout=20)

        assert not thread.is_alive()
        assert errors == []
        assert len(results) == 1
        assert results[0].applied == (1,)
    finally:
        if lock_connection.in_transaction:
            lock_connection.execute("ROLLBACK")
        lock_connection.close()


def test_migrate_lock_timeout_raises(database_path: Path) -> None:
    with closing(connect(database_path)) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")
        try:
            with closing(connect(database_path, timeout=0.1)) as connection:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    migrate(connection)
                assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        finally:
            if lock_connection.in_transaction:
                lock_connection.execute("ROLLBACK")


def test_concurrent_processes_migrate_once(database_path: Path) -> None:
    environment = os.environ.copy()
    environment["DATABASE_PATH"] = str(database_path)
    command = [sys.executable, "-m", "training_season.database"]
    processes = [
        subprocess.Popen(  # noqa: S603
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]

    outputs: list[tuple[str, str]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, f"stdout: {stdout}\nstderr: {stderr}"
        outputs.append((stdout, stderr))

    applied_logs = [
        line
        for _stdout, stderr in outputs
        for line in stderr.splitlines()
        if "applied [1]" in line
    ]
    assert len(applied_logs) == 1
    assert all("schema version 1" in stderr for _stdout, stderr in outputs)

    with closing(connect(database_path)) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        user_version = connection.execute("PRAGMA user_version").fetchone()

    assert [row[0] for row in tables] == sorted(_TABLE_NAMES)
    assert user_version == (1,)
