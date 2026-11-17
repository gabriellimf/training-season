"""Command-line entry point that serves the chat page with uvicorn."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from typing import TYPE_CHECKING

import uvicorn

from training_season.cli_entry import report_failure, run_cli
from training_season.coach_instruction import CoachInstructionError
from training_season.database import DatabaseConfigurationError, MigrationError
from training_season.garmin_auth import GarminConfigurationError
from training_season.llm import LLMConfigurationError
from training_season.timeformat import TrainingTimezoneError
from training_season.web_app import create_app
from training_season.web_settings import WebConfigurationError, WebDependencies

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from fastapi import FastAPI

type ServeApp = Callable[[FastAPI, str, int], None]

WEB_HOST_ENV = "WEB_HOST"
WEB_PORT_ENV = "WEB_PORT"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8000
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
NOT_LOOPBACK_WARNING = (
    "The chat page has no login yet: keep this address private to this machine "
    "or a network only you use."
)

_LOGGER = logging.getLogger(__name__)

_USAGE = "training-season-web"
_INPUT_ERROR = 2
_INPUT_ERRORS = (
    WebConfigurationError,
    DatabaseConfigurationError,
    LLMConfigurationError,
    CoachInstructionError,
    TrainingTimezoneError,
    GarminConfigurationError,
)
_RUNTIME_ERRORS = (MigrationError, sqlite3.Error)


def _uvicorn_serve(app: FastAPI, host: str, port: int) -> None:
    # One worker: the coach lock and the check-in conversation live in memory.
    uvicorn.run(app, host=host, port=port, workers=1, server_header=False)


def _warn_unless_loopback(host: str) -> None:
    if host not in LOOPBACK_HOSTS:
        _LOGGER.warning(
            "%s is %s, not loopback. %s", WEB_HOST_ENV, host, NOT_LOOPBACK_WARNING
        )


def web_host_from_environment(environ: Mapping[str, str]) -> str:
    """
    Return the address to listen on, loopback when unset.

    Used as `host = web_host_from_environment(os.environ)`
    """
    host = environ.get(WEB_HOST_ENV, "").strip()
    return host or DEFAULT_WEB_HOST


def web_port_from_environment(environ: Mapping[str, str]) -> int:
    """
    Return the TCP port to listen on, 8000 when unset.

    Used as `port = web_port_from_environment({"WEB_PORT": "8080"})`
    """
    raw_port = environ.get(WEB_PORT_ENV, "")
    if not raw_port.strip():
        return DEFAULT_WEB_PORT
    text = raw_port.strip()
    if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= 65535:
        raise WebConfigurationError(
            f"{WEB_PORT_ENV} must be an integer from 1 to 65535. Got: {raw_port!r}."
        )
    return int(text)


def _serve_web(environ: Mapping[str, str], serve: ServeApp) -> int:
    host = web_host_from_environment(environ)
    port = web_port_from_environment(environ)
    app = create_app(WebDependencies(environ))
    _warn_unless_loopback(host)
    serve(app, host, port)
    return 0


def _web_cli(environ: Mapping[str, str], serve: ServeApp) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        return _serve_web(environ, serve)
    except _INPUT_ERRORS as error:
        return report_failure(error, _INPUT_ERROR)
    except _RUNTIME_ERRORS as error:
        return report_failure(error, 1)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    serve: ServeApp = _uvicorn_serve,
) -> int:
    """
    Check the settings, migrate the database, then serve the chat page.

    Takes no arguments. Used as `main([])`
    """
    environment = os.environ if environ is None else environ
    return run_cli(argv, _USAGE, {()}, lambda _arguments: _web_cli(environment, serve))


if __name__ == "__main__":
    raise SystemExit(main())
