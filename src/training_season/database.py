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


def _require_database_parent(parent: Path) -> None:
    if not parent.exists():
        raise DatabaseConfigurationError(f"parent directory does not exist: {parent}")
    if not parent.is_dir():
        raise DatabaseConfigurationError(f"parent path is not a directory: {parent}")


def database_path_from_environment(environ: Mapping[str, str] | None = None) -> Path:
    """Read and validate the absolute SQLite file path from the environment."""
    environment = os.environ if environ is None else environ
    raw_path = environment.get(DATABASE_PATH_ENV)
    if raw_path is None or not raw_path.strip() or not Path(raw_path).is_absolute():
        raise DatabaseConfigurationError(
            f"{DATABASE_PATH_ENV} must be an absolute path to the SQLite file"
        )
    path = Path(raw_path)
    _require_database_parent(path.parent)
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


def _roll_back_after_error(
    connection: sqlite3.Connection, error: BaseException
) -> None:
    if not connection.in_transaction:
        return
    try:
        connection.execute("ROLLBACK")
    except BaseException as rollback_error:
        raise error from rollback_error


def _commit_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("transaction ended before commit")
    try:
        connection.execute("COMMIT")
    except BaseException as commit_error:
        _roll_back_after_error(connection, commit_error)
        raise


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
    except BaseException as body_error:
        _roll_back_after_error(connection, body_error)
        raise
    else:
        _commit_transaction(connection)


def _migration_sql_files(migration_directory: Traversable) -> list[Traversable]:
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
    return sql_files


def _migration_version(entry: Traversable) -> int:
    match = _MIGRATION_FILENAME.fullmatch(entry.name)
    if match is None:
        raise MigrationError(f"invalid migration filename: {entry.name}")
    return int(match.group(1))


def _read_migration(entry: Traversable, version: int) -> Migration:
    sql = entry.read_text(encoding="utf-8")
    if not sql.strip():
        raise MigrationError(f"migration file is empty: {entry.name}")
    return Migration(version=version, name=entry.name, sql=sql)


def _require_sequential_versions(
    versions: list[int], ordered_versions: list[int]
) -> None:
    if ordered_versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"migration versions must be sequential from 1, found {versions}"
        )


def load_migrations(directory: Traversable | None = None) -> tuple[Migration, ...]:
    """Load and validate the sequential SQL migration files."""
    migration_directory = (
        files("training_season").joinpath("migrations")
        if directory is None
        else directory
    )
    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for entry in _migration_sql_files(migration_directory):
        version = _migration_version(entry)
        if version in seen_versions:
            raise MigrationError(f"duplicate migration version {version}")
        seen_versions.add(version)
        migrations.append(_read_migration(entry, version))
    versions = [migration.version for migration in migrations]
    _require_sequential_versions(versions, versions)
    return tuple(migrations)


_READ_ONLY_PRAGMAS = frozenset({"quick_check"})


def _migration_authorizer(
    action: int,
    argument_one: str | None,
    _argument_two: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    # SQLite runs an internal `PRAGMA quick_check` for ALTER TABLE ADD COLUMN.
    if action == sqlite3.SQLITE_PRAGMA and argument_one in _READ_ONLY_PRAGMAS:
        return sqlite3.SQLITE_OK
    denied_actions = {
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_PRAGMA,
    }
    if action in denied_actions:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _checked_migration_set(migrations: Sequence[Migration]) -> tuple[Migration, ...]:
    migration_set = tuple(migrations)
    if not migration_set:
        raise MigrationError("no migrations provided")

    versions = [migration.version for migration in migration_set]
    if len(set(versions)) != len(versions):
        raise MigrationError("duplicate migration version")
    _require_sequential_versions(versions, sorted(versions))
    return tuple(sorted(migration_set, key=lambda migration: migration.version))


def _checked_schema_version(connection: sqlite3.Connection, latest_version: int) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        raise MigrationError("SQLite did not return the schema version")
    previous_version = int(row[0])
    if previous_version > latest_version:
        raise MigrationError(
            "database schema version "
            f"{previous_version} is newer than this code, latest {latest_version}"
        )
    return previous_version


def _apply_migration(connection: sqlite3.Connection, migration: Migration) -> None:
    connection.set_authorizer(_migration_authorizer)
    try:
        connection.executescript(migration.sql)
    except sqlite3.DatabaseError as error:
        raise MigrationError(f"migration {migration.name} failed: {error}") from error
    finally:
        connection.set_authorizer(None)

    if not connection.in_transaction:
        raise MigrationError(f"migration {migration.name} ended the runner transaction")
    connection.execute(f"PRAGMA user_version = {migration.version:d}")


def _run_pending_migrations(
    connection: sqlite3.Connection,
    migration_set: tuple[Migration, ...],
    previous_version: int,
) -> tuple[int, ...]:
    applied: list[int] = []
    for migration in migration_set:
        if migration.version <= previous_version:
            continue
        _apply_migration(connection, migration)
        applied.append(migration.version)
    return tuple(applied)


def migrate(
    connection: sqlite3.Connection, migrations: Sequence[Migration] | None = None
) -> MigrationResult:
    """Apply all pending migrations atomically under one write lock."""
    migration_set = _checked_migration_set(
        load_migrations() if migrations is None else migrations
    )
    latest_version = migration_set[-1].version
    with transaction(connection):
        previous_version = _checked_schema_version(connection, latest_version)
        applied = _run_pending_migrations(connection, migration_set, previous_version)
    current_version = applied[-1] if applied else previous_version
    return MigrationResult(
        previous_version=previous_version,
        current_version=current_version,
        applied=applied,
    )


def _wait_for_wal_retry(error: sqlite3.OperationalError, deadline: float) -> None:
    message = str(error).casefold()
    if "locked" not in message and "busy" not in message:
        raise error

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise error
    time.sleep(min(WAL_RETRY_INTERVAL_SECONDS, remaining))
    if time.monotonic() >= deadline:
        raise error


def _switch_journal_to_wal(
    connection: sqlite3.Connection, deadline: float
) -> str | None:
    while True:
        try:
            row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError as error:
            _wait_for_wal_retry(error, deadline)
            continue
        return None if row is None else str(row[0])


def _ensure_wal_mode(connection: sqlite3.Connection, *, retry_timeout: float) -> None:
    row = connection.execute("PRAGMA journal_mode").fetchone()
    if row is None:
        raise DatabaseConfigurationError("SQLite did not return a journal mode")
    if str(row[0]).casefold() == "wal":
        return
    deadline = time.monotonic() + retry_timeout
    new_journal_mode = _switch_journal_to_wal(connection, deadline)
    if new_journal_mode is None or new_journal_mode.casefold() != "wal":
        raise DatabaseConfigurationError("SQLite could not enable WAL mode")


def initialize_database(
    environ: Mapping[str, str] | None = None,
    *,
    wal_retry_timeout: float = BUSY_TIMEOUT_SECONDS,
) -> MigrationResult:
    """Configure SQLite and apply migrations from the environment path."""
    path = database_path_from_environment(environ)
    with closing(connect(path)) as connection:
        _ensure_wal_mode(connection, retry_timeout=wal_retry_timeout)
        migration_outcome = migrate(connection)
        applied: str | list[int] = (
            list(migration_outcome.applied) if migration_outcome.applied else "none"
        )
        _LOGGER.info(
            "database %s at schema version %d, applied %s",
            path,
            migration_outcome.current_version,
            applied,
        )
        return migration_outcome


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
