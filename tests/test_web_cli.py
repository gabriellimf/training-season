"""The `training-season-web` command: settings, exit codes and the uvicorn call."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
import uvicorn
from fastapi import FastAPI

from training_season import web_cli
from training_season.web_app import create_app
from training_season.web_cli import (
    main,
    web_host_from_environment,
    web_port_from_environment,
)
from training_season.web_settings import WebConfigurationError
from web_fakes import placeholder_static_dir, web_environ

if TYPE_CHECKING:
    from pathlib import Path

    from training_season.web_settings import WebDependencies


@dataclass(frozen=True, slots=True)
class ServeCall:
    """One call of the serve function."""

    app: object
    host: str
    port: int
    options: dict[str, object] = field(default_factory=dict)


@dataclass
class RecordingServe:
    """Records the serve calls instead of listening on a port."""

    calls: list[ServeCall] = field(default_factory=list)

    def __call__(self, app: FastAPI, host: str, port: int) -> None:
        self.calls.append(ServeCall(app, host, port))


@dataclass
class RecordingUvicornRun:
    """Stands in for `uvicorn.run`; records its arguments."""

    calls: list[ServeCall] = field(default_factory=list)

    def __call__(
        self, app: object, *, host: str, port: int, workers: int, server_header: bool
    ) -> None:
        options: dict[str, object] = {
            "workers": workers,
            "server_header": server_header,
        }
        self.calls.append(ServeCall(app, host, port, options))


@dataclass(frozen=True, slots=True)
class PlaceholderPageApp:
    """Builds the real app, but serves the placeholder page folder."""

    static_dir: Path

    def __call__(self, dependencies: WebDependencies) -> FastAPI:
        return create_app(dependencies, static_dir=self.static_dir)


def _cli_environ(tmp_path: Path) -> dict[str, str]:
    # The real adapter factory runs; it builds a lazy client and calls no model.
    return {
        **web_environ(tmp_path),
        "LLM_PROVIDER": "anthropic",
        "ANTHROPIC_API_KEY": "sk-ant-test-key",
    }


@pytest.fixture(autouse=True)
def placeholder_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_factory = PlaceholderPageApp(placeholder_static_dir(tmp_path))
    monkeypatch.setattr(web_cli, "create_app", app_factory)


def test_help_prints_the_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert capsys.readouterr().err == "Usage: training-season-web\n"


def test_arguments_print_the_usage_and_exit_two() -> None:
    assert main(["serve"], serve=RecordingServe()) == 2


def test_serves_the_app_on_loopback_8000_by_default(tmp_path: Path) -> None:
    serve = RecordingServe()

    assert main([], environ=_cli_environ(tmp_path), serve=serve) == 0

    [call] = serve.calls
    assert (call.host, call.port) == ("127.0.0.1", 8000)
    assert isinstance(call.app, FastAPI)


def test_serves_on_the_configured_host_and_port(tmp_path: Path) -> None:
    environ = {**_cli_environ(tmp_path), "WEB_HOST": " 0.0.0.0 ", "WEB_PORT": "8080"}
    serve = RecordingServe()

    assert main([], environ=environ, serve=serve) == 0

    assert (serve.calls[0].host, serve.calls[0].port) == ("0.0.0.0", 8080)  # noqa: S104


def test_a_host_that_is_not_loopback_logs_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    environ = {**_cli_environ(tmp_path), "WEB_HOST": "0.0.0.0"}  # noqa: S104

    with caplog.at_level(logging.WARNING, logger=web_cli.__name__):
        assert main([], environ=environ, serve=RecordingServe()) == 0

    assert [record.getMessage() for record in caplog.records] == [
        f"WEB_HOST is 0.0.0.0, not loopback. {web_cli.NOT_LOOPBACK_WARNING}"
    ]


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_a_loopback_host_logs_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, host: str
) -> None:
    environ = {**_cli_environ(tmp_path), "WEB_HOST": host}

    with caplog.at_level(logging.WARNING, logger=web_cli.__name__):
        assert main([], environ=environ, serve=RecordingServe()) == 0

    assert caplog.records == []


def test_default_serve_runs_uvicorn_with_one_worker_and_no_server_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uvicorn_run = RecordingUvicornRun()
    monkeypatch.setattr(uvicorn, "run", uvicorn_run)

    assert main([], environ=_cli_environ(tmp_path)) == 0

    assert uvicorn_run.calls[0].options == {"workers": 1, "server_header": False}


@pytest.mark.parametrize(
    "raw_port",
    ["0", "65536", "80a", "-1", "\N{FULLWIDTH DIGIT EIGHT}\N{FULLWIDTH DIGIT ZERO}"],
)
def test_bad_port_exits_two_and_names_the_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], raw_port: str
) -> None:
    environ = {**web_environ(tmp_path), "WEB_PORT": raw_port}
    serve = RecordingServe()

    assert main([], environ=environ, serve=serve) == 2

    assert f"WEB_PORT must be an integer from 1 to 65535. Got: {raw_port!r}." in (
        capsys.readouterr().err
    )
    assert serve.calls == []


def test_bad_setting_exits_two_before_serving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = web_environ(tmp_path)
    del environ["DATABASE_PATH"]
    serve = RecordingServe()

    assert main([], environ=environ, serve=serve) == 2

    assert "DATABASE_PATH must be an absolute path" in capsys.readouterr().err
    assert serve.calls == []


def test_host_and_port_defaults_and_bounds() -> None:
    assert web_host_from_environment({"WEB_HOST": " "}) == "127.0.0.1"
    assert web_port_from_environment({}) == 8000
    assert web_port_from_environment({"WEB_PORT": "1"}) == 1
    assert web_port_from_environment({"WEB_PORT": "65535"}) == 65535
    with pytest.raises(WebConfigurationError):
        web_port_from_environment({"WEB_PORT": "65536"})
