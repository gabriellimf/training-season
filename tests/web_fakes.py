"""Environment, clock and app builders for the chat page tests, all under tmp_path."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from llm_fakes import QueuedReplyAdapter
from training_season.database import connect
from training_season.garmin_auth import default_garmin_factory
from training_season.web_app import create_app
from training_season.web_settings import WebContext, WebDependencies

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from pathlib import Path

    from fastapi import FastAPI

    from training_season.garmin_auth import GarminFactory
    from training_season.llm import LLMAdapter
    from training_season.onboarding_cli import AdapterFactory

TEST_HOST = "testserver"
PLACEHOLDER_PAGE = "<!doctype html><title>Treino</title>"
NOW = datetime(2026, 11, 16, 20, 0, tzinfo=UTC)


def web_environ(tmp_path: Path) -> dict[str, str]:
    return {
        "DATABASE_PATH": str(tmp_path / "web.db"),
        "TZ": "America/Sao_Paulo",
        "GARMIN_TOKEN_DIR": str(tmp_path / "garmin"),
        "WEB_ALLOWED_HOSTS": TEST_HOST,
    }


def placeholder_static_dir(tmp_path: Path) -> Path:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(PLACEHOLDER_PAGE, encoding="utf-8")
    return static_dir


@dataclass
class FixedClock:
    """Returns the same aware moment on every call."""

    moment: datetime = NOW

    def __call__(self) -> datetime:
        return self.moment


@dataclass
class FailingClock:
    """Raises an error whose text must never reach a response or a log."""

    message: str = "private detail of the failure"

    def __call__(self) -> datetime:
        raise RuntimeError(self.message)


@dataclass
class RecordingAdapterFactory:
    """Hands out one fake adapter and records each environment it was built from."""

    adapter: QueuedReplyAdapter = field(default_factory=lambda: QueuedReplyAdapter([]))
    calls: list[Mapping[str, str]] = field(default_factory=list)

    def __call__(self, environ: Mapping[str, str]) -> LLMAdapter:
        self.calls.append(environ)
        return self.adapter


def adapter_factory_for(adapter: LLMAdapter) -> AdapterFactory:
    return lambda _environ: adapter


def build_web_app(
    tmp_path: Path,
    *,
    environ: Mapping[str, str] | None = None,
    clock: FixedClock | FailingClock | None = None,
    adapter_factory: AdapterFactory | None = None,
    garmin_factory: GarminFactory = default_garmin_factory,
) -> FastAPI:
    dependencies = WebDependencies(
        environ=web_environ(tmp_path) if environ is None else environ,
        adapter_factory=adapter_factory or RecordingAdapterFactory(),
        garmin_factory=garmin_factory,
        clock=clock or FixedClock(),
    )
    return create_app(dependencies, static_dir=placeholder_static_dir(tmp_path))


def web_database(tmp_path: Path) -> closing[sqlite3.Connection]:
    return closing(connect(tmp_path / "web.db"))


def context_of(app: FastAPI) -> WebContext:
    context = app.state.web_context
    assert isinstance(context, WebContext)
    return context
