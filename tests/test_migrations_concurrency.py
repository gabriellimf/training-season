"""Tests for migrations run by concurrent threads and processes."""

import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from schema_rows import _TABLE_NAMES
from training_season.database import (
    MigrationError,
    MigrationResult,
    connect,
    migrate,
)


def test_concurrent_migrations_apply_once(database_path: Path) -> None:
    barrier = threading.Barrier(4)
    migration_results: list[MigrationResult] = []
    errors: list[BaseException] = []

    def run_migration() -> None:
        try:
            with closing(connect(database_path)) as connection:
                barrier.wait(timeout=10)
                migration_result = migrate(connection)
            migration_results.append(migration_result)
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
    assert len(migration_results) == 4
    assert (
        sum(
            migration_result.applied == (1, 2, 3)
            for migration_result in migration_results
        )
        == 1
    )

    with closing(connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)


def test_migrate_waits_for_write_lock(database_path: Path) -> None:
    lock_connection = connect(database_path)
    thread_started = threading.Event()
    migration_results: list[MigrationResult] = []
    errors: list[BaseException] = []

    def run_migration() -> None:
        try:
            with closing(connect(database_path)) as connection:
                thread_started.set()
                migration_results.append(migrate(connection))
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
        assert len(migration_results) == 1
        assert migration_results[0].applied == (1, 2, 3)
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
        if "applied [1, 2, 3]" in line
    ]
    assert len(applied_logs) == 1
    assert all("schema version 3" in stderr for _stdout, stderr in outputs)

    with closing(connect(database_path)) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        user_version = connection.execute("PRAGMA user_version").fetchone()

    assert [row[0] for row in tables] == sorted(_TABLE_NAMES)
    assert user_version == (3,)
