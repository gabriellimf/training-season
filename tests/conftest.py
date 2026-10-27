"""Shared database fixtures."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from training_season.database import connect, migrate


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "training-season.db"


@pytest.fixture
def migrated_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(database_path)
    try:
        migrate(connection)
        yield connection
    finally:
        connection.close()
