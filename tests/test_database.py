"""Tests for database configuration, connections, and transactions."""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from training_season import database as database_module
from training_season.database import (
    BUSY_TIMEOUT_SECONDS,
    DatabaseConfigurationError,
    MigrationError,
    MigrationResult,
    connect,
    database_path_from_environment,
    initialize_database,
    main,
    migrate,
    transaction,
)
from training_season.timeformat import utc_now

_ABSOLUTE_PATH_TEXT = "DATABASE_PATH must be an absolute path to the SQLite file"


@dataclass
class SqlStatementRecorder:
    real_connect: Callable[..., sqlite3.Connection]
    statements: list[str] = field(default_factory=list)

    def connect(self, *args: object, **kwargs: object) -> sqlite3.Connection:
        connection = self.real_connect(*args, **kwargs)
        connection.set_trace_callback(self.statements.append)
        return connection


def _deny_rollback(
    action: int,
    argument_one: str | None,
    _argument_two: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    if action == sqlite3.SQLITE_TRANSACTION and argument_one == "ROLLBACK":
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _insert_then_fail(connection: sqlite3.Connection) -> None:
    with transaction(connection):
        connection.execute("INSERT INTO example (value) VALUES (?)", ("not saved",))
        raise RuntimeError("failure")


def _insert_deferred_invalid_message(connection: sqlite3.Connection) -> None:
    with transaction(connection):
        connection.execute("PRAGMA defer_foreign_keys = ON")
        connection.execute(
            "INSERT INTO message (role, content, weekly_plan_id, created_at) "
            "VALUES ('assistant', 'missing plan', 999, ?)",
            (utc_now(),),
        )


def test_database_path_reads_environment_variable(database_path: Path) -> None:
    assert (
        database_path_from_environment({"DATABASE_PATH": str(database_path)})
        == database_path
    )


def test_database_path_unset_raises() -> None:
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_PATH") as error:
        database_path_from_environment({})

    assert str(error.value) == _ABSOLUTE_PATH_TEXT


@pytest.mark.parametrize("value", ["", "   "])
def test_database_path_blank_raises(value: str) -> None:
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_PATH") as error:
        database_path_from_environment({"DATABASE_PATH": value})

    assert str(error.value) == _ABSOLUTE_PATH_TEXT


def test_database_path_relative_raises() -> None:
    with pytest.raises(DatabaseConfigurationError, match="absolute path") as error:
        database_path_from_environment({"DATABASE_PATH": "training-season.db"})

    assert str(error.value) == _ABSOLUTE_PATH_TEXT


def test_database_path_missing_parent_raises(tmp_path: Path) -> None:
    missing_parent = tmp_path / "missing"
    with pytest.raises(DatabaseConfigurationError) as error:
        database_path_from_environment(
            {"DATABASE_PATH": str(missing_parent / "training-season.db")}
        )

    assert str(error.value) == f"parent directory does not exist: {missing_parent}"


def test_database_path_parent_is_file_raises(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("data", encoding="utf-8")

    with pytest.raises(DatabaseConfigurationError) as error:
        database_path_from_environment(
            {"DATABASE_PATH": str(parent_file / "training-season.db")}
        )

    assert str(error.value) == f"parent path is not a directory: {parent_file}"


def test_database_path_is_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(DatabaseConfigurationError, match="not a directory") as error:
        database_path_from_environment({"DATABASE_PATH": str(tmp_path)})

    assert (
        str(error.value)
        == f"DATABASE_PATH must name a file, not a directory: {tmp_path}"
    )


def test_connect_enables_foreign_keys(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_connect_sets_busy_timeout(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        row = connection.execute("PRAGMA busy_timeout").fetchone()

    assert row == (int(BUSY_TIMEOUT_SECONDS * 1000),)


def test_connect_uses_autocommit_mode(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        assert connection.autocommit is True


def test_transaction_commits_on_success(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
        with transaction(connection):
            connection.execute("INSERT INTO example (value) VALUES (?)", ("saved",))

        assert connection.execute("SELECT value FROM example").fetchone() == ("saved",)


def test_transaction_rolls_back_on_error(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
        with pytest.raises(RuntimeError, match="failure"):
            _insert_then_fail(connection)

        assert connection.execute("SELECT count(*) FROM example").fetchone() == (0,)


def test_transaction_preserves_body_error_when_rollback_fails(
    database_path: Path,
) -> None:
    with closing(connect(database_path)) as connection:
        connection.set_authorizer(_deny_rollback)
        try:
            with (
                pytest.raises(ValueError, match="original body error") as error,
                transaction(connection),
            ):
                raise ValueError("original body error")
        finally:
            connection.set_authorizer(None)

    assert type(error.value) is ValueError
    assert str(error.value) == "original body error"
    assert isinstance(error.value.__cause__, sqlite3.Error)
    assert str(error.value.__cause__) == "not authorized"


def test_transaction_keeps_commit_error_when_rollback_fails(
    migrated_connection: sqlite3.Connection,
) -> None:
    migrated_connection.set_authorizer(_deny_rollback)
    try:
        with pytest.raises(sqlite3.IntegrityError) as raised:
            _insert_deferred_invalid_message(migrated_connection)
    finally:
        migrated_connection.set_authorizer(None)

    assert type(raised.value) is sqlite3.IntegrityError
    assert str(raised.value) == "FOREIGN KEY constraint failed"
    assert isinstance(raised.value.__cause__, sqlite3.DatabaseError)
    assert str(raised.value.__cause__) == "not authorized"
    assert migrated_connection.in_transaction
    migrated_connection.execute("ROLLBACK")


def test_transaction_rolls_back_after_commit_error(
    migrated_connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        _insert_deferred_invalid_message(migrated_connection)

    assert not migrated_connection.in_transaction
    with transaction(migrated_connection):
        migrated_connection.execute(
            "INSERT INTO coach_summary (summary, created_at) VALUES (?, ?)",
            ("transaction recovered", utc_now()),
        )


def test_transaction_rejects_nested_use(database_path: Path) -> None:
    with (
        closing(connect(database_path)) as connection,
        transaction(connection),
        pytest.raises(ValueError, match="already"),
        transaction(connection),
    ):
        pass


def test_transaction_rejects_legacy_connection() -> None:
    with (
        closing(sqlite3.connect(":memory:")) as connection,
        pytest.raises(ValueError, match="autocommit=True"),
        transaction(connection),
    ):
        pass


def test_initialize_database_creates_file_and_enables_wal(
    database_path: Path,
) -> None:
    result = initialize_database({"DATABASE_PATH": str(database_path)})

    assert database_path.is_file()
    assert result.current_version == 4
    with closing(connect(database_path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_initialize_database_statement_order_and_log(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = SqlStatementRecorder(real_connect=sqlite3.connect)
    monkeypatch.setattr(sqlite3, "connect", recorder.connect)
    environment = {"DATABASE_PATH": str(database_path)}

    with caplog.at_level(logging.INFO, logger="training_season.database"):
        initialize_database(environment)
        fresh = list(recorder.statements)
        recorder.statements.clear()
        initialize_database(environment)

    assert fresh[:6] == [
        "PRAGMA foreign_keys = ON",
        "PRAGMA foreign_keys",
        "PRAGMA journal_mode",
        "PRAGMA journal_mode = WAL",
        "BEGIN IMMEDIATE",
        "PRAGMA user_version",
    ]
    assert fresh[-2:] == ["PRAGMA user_version = 4", "COMMIT"]
    assert fresh.count("BEGIN IMMEDIATE") == 1
    assert fresh.count("COMMIT") == 1
    assert "ROLLBACK" not in fresh
    assert recorder.statements == [
        "PRAGMA foreign_keys = ON",
        "PRAGMA foreign_keys",
        "PRAGMA journal_mode",
        "BEGIN IMMEDIATE",
        "PRAGMA user_version",
        "COMMIT",
    ]
    assert [
        (record.name, record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "training_season.database"
    ] == [
        (
            "training_season.database",
            logging.INFO,
            f"database {database_path} at schema version 4, applied [1, 2, 3, 4]",
        ),
        (
            "training_season.database",
            logging.INFO,
            f"database {database_path} at schema version 4, applied none",
        ),
    ]


def test_wal_switch_retries_until_lock_released(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_connection = connect(database_path)
    lock_connection.execute("BEGIN IMMEDIATE")
    retry_waiting = threading.Event()
    retry_allowed = threading.Event()
    results: list[MigrationResult] = []
    errors: list[BaseException] = []
    original_sleep = time.sleep

    def wait_for_retry(seconds: float) -> None:
        retry_waiting.set()
        if not retry_allowed.wait(timeout=5):
            original_sleep(seconds)

    def initialize() -> None:
        try:
            results.append(initialize_database({"DATABASE_PATH": str(database_path)}))
        except (
            DatabaseConfigurationError,
            MigrationError,
            OSError,
            RuntimeError,
            sqlite3.Error,
        ) as error:
            errors.append(error)

    monkeypatch.setattr(time, "sleep", wait_for_retry)
    thread = threading.Thread(target=initialize)
    thread.start()
    try:
        assert retry_waiting.wait(timeout=5)
        assert thread.is_alive()
    finally:
        if lock_connection.in_transaction:
            lock_connection.execute("ROLLBACK")
        retry_allowed.set()
        thread.join(timeout=20)
        lock_connection.close()

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1
    with closing(connect(database_path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_wal_switch_times_out_at_injected_deadline(
    database_path: Path,
) -> None:
    with closing(connect(database_path)) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked") as raised:
            initialize_database(
                {"DATABASE_PATH": str(database_path)},
                wal_retry_timeout=0.05,
            )

        assert str(raised.value) == "database is locked"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        lock_connection.execute("ROLLBACK")
        assert lock_connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_already_wal_skips_switch(database_path: Path) -> None:
    with closing(connect(database_path)) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        switch_attempts: list[str | None] = []

        def deny_switch(
            action: int,
            argument_one: str | None,
            argument_two: str | None,
            _database_name: str | None,
            _trigger_name: str | None,
        ) -> int:
            if (
                action == sqlite3.SQLITE_PRAGMA
                and argument_one == "journal_mode"
                and argument_two is not None
            ):
                switch_attempts.append(argument_two)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_switch)
        try:
            database_module._ensure_wal_mode(
                connection,
                retry_timeout=0,
            )
        finally:
            connection.set_authorizer(None)

    assert switch_attempts == []


def test_main_returns_2_when_path_unset(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    caplog.set_level(logging.ERROR)

    assert main() == 2
    assert "DATABASE_PATH" in caplog.text
    assert not database_path.exists()


def test_main_returns_1_for_migration_error(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    with closing(connect(database_path)) as connection:
        connection.execute("PRAGMA user_version = 99")
    caplog.set_level(logging.ERROR)

    assert main() == 1
    error_records = [
        record for record in caplog.records if record.levelno == logging.ERROR
    ]

    assert len(error_records) == 1
    assert error_records[0].getMessage() == (
        "database initialization failed: database schema version 99 is newer than "
        "this code, latest 4"
    )
    assert error_records[0].exc_info is None
    assert "Traceback" not in caplog.text


def test_main_logs_sqlite_constraint_without_row_value(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_with_constraint() -> None:
        with closing(connect(database_path)) as connection:
            migrate(connection)
            connection.execute(
                "INSERT INTO checkin (week_start, fatigue, has_pain, created_at) "
                "VALUES ('2026-10-26', 11, 0, ?)",
                (utc_now(),),
            )

    monkeypatch.setattr(database_module, "initialize_database", fail_with_constraint)
    caplog.set_level(logging.ERROR)

    assert main() == 1
    error_records = [
        record for record in caplog.records if record.levelno == logging.ERROR
    ]

    assert len(error_records) == 1
    assert error_records[0].getMessage() == (
        "database initialization failed: CHECK constraint failed: "
        "fatigue BETWEEN 1 AND 10"
    )
    assert "11" not in error_records[0].getMessage()
    assert error_records[0].exc_info is None


def test_main_creates_database(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(database_path))

    assert main() == 0
    assert database_path.is_file()


def test_new_database_file_is_owner_only(database_path: Path) -> None:
    initialize_database({"DATABASE_PATH": str(database_path)})

    assert database_path.stat().st_mode & 0o777 == 0o600
    with closing(connect(database_path)) as connection:
        with transaction(connection):
            connection.execute(
                "INSERT INTO coach_summary (summary, created_at) VALUES (?, ?)",
                ("test summary", utc_now()),
            )
        wal_path = database_path.with_name(f"{database_path.name}-wal")
        assert wal_path.is_file()
        assert wal_path.stat().st_mode & 0o777 == 0o600


def test_connect_keeps_existing_file_mode(database_path: Path) -> None:
    database_path.touch()
    database_path.chmod(0o640)

    with closing(connect(database_path)):
        assert database_path.stat().st_mode & 0o777 == 0o640
