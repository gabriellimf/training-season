"""Configuration and shared state the chat page routes read, loaded once at start."""

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from training_season.coach_instruction import load_coach_instruction
from training_season.database import connect, database_path_from_environment
from training_season.garmin_auth import (
    GarminFactory,
    default_garmin_factory,
    garmin_auth_config_from_environment,
)
from training_season.llm import LLMAdapter
from training_season.onboarding_chat import CoachSetup
from training_season.onboarding_cli import (
    AdapterFactory,
    default_adapter_factory,
    utc_clock,
)
from training_season.timeformat import training_date, training_zone_from_environment
from training_season.web_work import CheckinSlot, CoachWork

WEB_ALLOWED_HOSTS_ENV = "WEB_ALLOWED_HOSTS"
DEFAULT_ALLOWED_HOSTS = "127.0.0.1,localhost"


class WebConfigurationError(ValueError):
    """A web setting in the environment is missing or invalid."""


@dataclass(frozen=True, slots=True)
class WebDependencies:
    """What the app reads from outside: the environment and the injected factories."""

    environ: Mapping[str, str]
    adapter_factory: AdapterFactory = default_adapter_factory
    garmin_factory: GarminFactory = default_garmin_factory
    clock: Callable[[], datetime] = utc_clock


@dataclass(frozen=True, slots=True)
class WebSettings:
    """The validated settings: the coach setup, the database file, the hosts."""

    setup: CoachSetup
    database_path: Path
    allowed_hosts: tuple[str, ...]
    environ: Mapping[str, str] = field(repr=False)
    adapter_factory: AdapterFactory


@dataclass(frozen=True, slots=True)
class WebContext:
    """The settings and the in-memory state of the one server process."""

    settings: WebSettings
    work: CoachWork
    checkin: CheckinSlot


def allowed_hosts_from_environment(environ: Mapping[str, str]) -> tuple[str, ...]:
    """
    Read the Host header values the app answers, comma separated, no wildcard.

    Used as `allowed_hosts_from_environment({"WEB_ALLOWED_HOSTS": "localhost"})`
    """
    raw_hosts = environ.get(WEB_ALLOWED_HOSTS_ENV, "")
    text = raw_hosts if raw_hosts.strip() else DEFAULT_ALLOWED_HOSTS
    hosts = tuple(host.strip() for host in text.split(",") if host.strip())
    if not hosts or any("*" in host or " " in host for host in hosts):
        raise WebConfigurationError(
            f"{WEB_ALLOWED_HOSTS_ENV} must be a comma-separated list of host names "
            f"without wildcards, such as {DEFAULT_ALLOWED_HOSTS}. Got: {raw_hosts!r}."
        )
    return hosts


def load_web_settings(dependencies: WebDependencies) -> WebSettings:
    """
    Validate every setting the routes need, before the server listens.

    Used as `settings = load_web_settings(WebDependencies(os.environ))`
    """
    environ = dependencies.environ
    setup = CoachSetup(
        instruction=load_coach_instruction(environ),
        zone=training_zone_from_environment(environ),
        auth_config=garmin_auth_config_from_environment(environ),
        garmin_factory=dependencies.garmin_factory,
        clock=dependencies.clock,
    )
    return WebSettings(
        setup=setup,
        database_path=database_path_from_environment(environ),
        allowed_hosts=allowed_hosts_from_environment(environ),
        environ=environ,
        adapter_factory=dependencies.adapter_factory,
    )


def training_today(setup: CoachSetup) -> date:
    """
    Return the local training date of the injected clock.

    Used as `today = training_today(context.settings.setup)`
    """
    return date.fromisoformat(training_date(setup.clock(), setup.zone))


def open_connection(settings: WebSettings) -> closing[sqlite3.Connection]:
    """
    Open one SQLite connection for one request, closed on exit.

    SQLite connections stay on the thread that opened them, so a route opens
    its own inside its body, never in a dependency.
    Used as `with open_connection(context.settings) as connection:`
    """
    return closing(connect(settings.database_path))


@contextmanager
def opened_adapter(settings: WebSettings) -> Iterator[LLMAdapter]:
    """
    Build the configured model adapter for one job and close it after.

    Used as `with opened_adapter(context.settings) as adapter:`
    """
    adapter = settings.adapter_factory(settings.environ)
    try:
        yield adapter
    finally:
        adapter.close()


def web_context(request: Request) -> WebContext:
    """
    Return the context `create_app` stored on the app, for `Depends`.

    Used as `context: Annotated[WebContext, Depends(web_context)]`
    """
    context = request.app.state.web_context
    if not isinstance(context, WebContext):
        raise TypeError(
            f"app.state.web_context is {type(context).__name__}, expected WebContext"
        )
    return context


WebContextParam = Annotated[WebContext, Depends(web_context)]
