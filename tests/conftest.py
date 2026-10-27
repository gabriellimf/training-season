"""Shared test fixtures."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import httpx2
import pytest
from curl_cffi.curl import Curl
from requests.adapters import HTTPAdapter

from training_season.database import connect, migrate


class NetworkBlockedError(BaseException):
    """Raised when a test attempts an unmocked network request."""


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> NoReturn:
        raise NetworkBlockedError("network access is disabled in tests")

    monkeypatch.setattr(HTTPAdapter, "send", blocked)
    monkeypatch.setattr(Curl, "perform", blocked)
    monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", blocked)


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
