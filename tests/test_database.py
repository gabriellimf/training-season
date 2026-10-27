"""Tests for database configuration, connections, and transactions."""

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from training_season.database import (
    BUSY_TIMEOUT_SECONDS,
    DatabaseConfigurationError,
    connect,
    database_path_from_environment,
    initialize_database,
    main,
    transaction,
)
from training_season.timeformat import utc_now


def _insert_then_fail(connection: sqlite3.Connection) -> None:
    with transaction(connection):
        connection.execute("INSERT INTO example (value) VALUES (?)", ("not saved",))
        raise RuntimeError("failure")


def test_database_path_reads_environment_variable(database_path: Path) -> None:
    assert (
        database_path_from_environment({"DATABASE_PATH": str(database_path)})
        == database_path
    )


def test_database_path_unset_raises() -> None:
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_PATH"):
        database_path_from_environment({})


@pytest.mark.parametrize("value", ["", "   "])
def test_database_path_blank_raises(value: str) -> None:
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_PATH"):
        database_path_from_environment({"DATABASE_PATH": value})


def test_database_path_relative_raises() -> None:
    with pytest.raises(DatabaseConfigurationError, match="absolute path"):
        database_path_from_environment({"DATABASE_PATH": "training-season.db"})


def test_database_path_missing_parent_raises(tmp_path: Path) -> None:
    missing_parent = tmp_path / "missing"
    with pytest.raises(DatabaseConfigurationError) as error:
        database_path_from_environment(
            {"DATABASE_PATH": str(missing_parent / "training-season.db")}
        )

    assert str(missing_parent) in str(error.value)


def test_database_path_parent_is_file_raises(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("data", encoding="utf-8")

    with pytest.raises(DatabaseConfigurationError) as error:
        database_path_from_environment(
            {"DATABASE_PATH": str(parent_file / "training-season.db")}
        )

    assert str(parent_file) in str(error.value)


def test_database_path_is_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(DatabaseConfigurationError, match="not a directory"):
        database_path_from_environment({"DATABASE_PATH": str(tmp_path)})


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
    assert result.current_version == 1
    with closing(connect(database_path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


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
