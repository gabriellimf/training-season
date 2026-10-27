"""
SQLite connections and versioned migrations for the training-season database.

Use ``connect()`` for every connection and ``transaction()`` for each write unit.
Never keep a write transaction open across a network call. Bind UTC timestamp and
training-date strings, not ``datetime`` or ``date`` objects.

A future migration that rebuilds a referenced table may need
``PRAGMA defer_foreign_keys``. The migration runner must then be changed to allow it.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from importlib.resources.abc import Traversable

DATABASE_PATH_ENV = "DATABASE_PATH"
BUSY_TIMEOUT_SECONDS = 10.0
WAL_RETRY_INTERVAL_SECONDS = 0.05

_MIGRATION_FILENAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
_LOGGER = logging.getLogger(__name__)


class DatabaseConfigurationError(Exception):
    """Raised when the database path or SQLite configuration is invalid."""


class MigrationError(Exception):
    """Raised when the migration set or a migration cannot be applied."""


@dataclass(frozen=True)
class Migration:
    """One versioned SQL migration."""

    version: int
    name: str
    sql: str


@dataclass(frozen=True)
class MigrationResult:
    """The schema version before and after a migration run."""

    previous_version: int
    current_version: int
    applied: tuple[int, ...]


def database_path_from_environment(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Read and validate the absolute SQLite file path from the environment."""
    environment = os.environ if environ is None else environ
    raw_path = environment.get(DATABASE_PATH_ENV)
    if raw_path is None or not raw_path.strip():
        raise DatabaseConfigurationError(
            f"{DATABASE_PATH_ENV} must be an absolute path to the SQLite file"
        )

    path = Path(raw_path)
    if not path.is_absolute():
        raise DatabaseConfigurationError(
            f"{DATABASE_PATH_ENV} must be an absolute path to the SQLite file"
        )

    parent = path.parent
    if not parent.exists():
        raise DatabaseConfigurationError(f"parent directory does not exist: {parent}")
    if not parent.is_dir():
        raise DatabaseConfigurationError(f"parent path is not a directory: {parent}")
    if path.exists() and path.is_dir():
        raise DatabaseConfigurationError(
            f"{DATABASE_PATH_ENV} must name a file, not a directory: {path}"
        )
    return path


def connect(path: Path, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Open a connection with private file permissions and foreign keys enabled."""
    path.touch(mode=0o600, exist_ok=True)
    connection = sqlite3.connect(path, timeout=timeout, autocommit=True)
    ready = False
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        row = connection.execute("PRAGMA foreign_keys").fetchone()
        if row is None or row[0] != 1:
            raise DatabaseConfigurationError("SQLite foreign key enforcement is off")
        ready = True
        return connection
    finally:
        if not ready:
            connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction and roll back its changes when its body fails."""
    if connection.autocommit is not True:
        raise ValueError("connection must use autocommit=True")
    if connection.in_transaction:
        raise ValueError("connection already has an open transaction")

    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    else:
        if not connection.in_transaction:
            raise RuntimeError("transaction ended before commit")
        try:
            connection.execute("COMMIT")
        except BaseException as commit_error:
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except BaseException as rollback_error:
                    raise commit_error from rollback_error
            raise


def load_migrations(
    directory: Traversable | None = None,
) -> tuple[Migration, ...]:
    """Load and validate the sequential SQL migration files."""
    migration_directory = (
        files("training_season").joinpath("migrations")
        if directory is None
        else directory
    )
    try:
        sql_files = sorted(
            (
                entry
                for entry in migration_directory.iterdir()
                if entry.is_file() and entry.name.endswith(".sql")
            ),
            key=lambda entry: entry.name,
        )
    except OSError as error:
        raise MigrationError(
            f"cannot read migration directory {migration_directory}"
        ) from error

    if not sql_files:
        raise MigrationError(f"no SQL migration files found in {migration_directory}")

    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for entry in sql_files:
        match = _MIGRATION_FILENAME.fullmatch(entry.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {entry.name}")
        version = int(match.group(1))
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version {version}")
        seen_versions.add(version)

        sql = entry.read_text(encoding="utf-8")
        if not sql.strip():
            raise MigrationError(f"migration file is empty: {entry.name}")
        migrations.append(Migration(version=version, name=entry.name, sql=sql))

    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"migration versions must be sequential from 1, found {versions}"
        )
    return tuple(migrations)


def _migration_authorizer(
    action: int,
    _argument_one: str | None,
    _argument_two: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    denied_actions = {
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_PRAGMA,
    }
    if action in denied_actions:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def migrate(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration] | None = None,
) -> MigrationResult:
    """Apply all pending migrations atomically under one write lock."""
    migration_set = tuple(load_migrations() if migrations is None else migrations)
    if not migration_set:
        raise MigrationError("no migrations provided")

    versions = [migration.version for migration in migration_set]
    if len(set(versions)) != len(versions):
        raise MigrationError("duplicate migration version")
    if sorted(versions) != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"migration versions must be sequential from 1, found {versions}"
        )
    migration_set = tuple(
        sorted(migration_set, key=lambda migration: migration.version)
    )
    latest_version = migration_set[-1].version
    applied: list[int] = []

    with transaction(connection):
        row = connection.execute("PRAGMA user_version").fetchone()
        if row is None:
            raise MigrationError("SQLite did not return the schema version")
        previous_version = int(row[0])
        if previous_version > latest_version:
            raise MigrationError(
                "database schema version "
                f"{previous_version} is newer than this code, latest {latest_version}"
            )

        for migration in migration_set:
            if migration.version <= previous_version:
                continue

            connection.set_authorizer(_migration_authorizer)
            try:
                connection.executescript(migration.sql)
            except sqlite3.DatabaseError as error:
                raise MigrationError(
                    f"migration {migration.name} failed: {error}"
                ) from error
            finally:
                connection.set_authorizer(None)

            if not connection.in_transaction:
                raise MigrationError(
                    f"migration {migration.name} ended the runner transaction"
                )
            connection.execute(f"PRAGMA user_version = {migration.version:d}")
            applied.append(migration.version)

        current_version = previous_version
        if applied:
            current_version = applied[-1]

    return MigrationResult(
        previous_version=previous_version,
        current_version=current_version,
        applied=tuple(applied),
    )


def _ensure_wal_mode(
    connection: sqlite3.Connection,
    *,
    retry_timeout: float,
) -> None:
    row = connection.execute("PRAGMA journal_mode").fetchone()
    if row is None:
        raise DatabaseConfigurationError("SQLite did not return a journal mode")
    if str(row[0]).casefold() == "wal":
        return

    deadline = time.monotonic() + retry_timeout
    while True:
        try:
            row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError as error:
            message = str(error).casefold()
            if "locked" not in message and "busy" not in message:
                raise

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(WAL_RETRY_INTERVAL_SECONDS, remaining))
            if time.monotonic() >= deadline:
                raise
            continue

        if row is None or str(row[0]).casefold() != "wal":
            raise DatabaseConfigurationError("SQLite could not enable WAL mode")
        return


def initialize_database(
    environ: Mapping[str, str] | None = None,
    *,
    wal_retry_timeout: float = BUSY_TIMEOUT_SECONDS,
) -> MigrationResult:
    """Configure SQLite and apply migrations from the environment path."""
    path = database_path_from_environment(environ)
    with closing(connect(path)) as connection:
        _ensure_wal_mode(connection, retry_timeout=wal_retry_timeout)
        result = migrate(connection)
        applied: str | list[int] = list(result.applied) if result.applied else "none"
        _LOGGER.info(
            "database %s at schema version %d, applied %s",
            path,
            result.current_version,
            applied,
        )
        return result


def main() -> int:
    """Run database initialization as a command-line entry point."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        initialize_database()
    except DatabaseConfigurationError as error:
        _LOGGER.error("%s", error)  # noqa: TRY400
        return 2
    except (MigrationError, sqlite3.Error) as error:
        _LOGGER.error("database initialization failed: %s", error)  # noqa: TRY400
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
