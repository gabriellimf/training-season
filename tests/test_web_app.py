"""The app factory, the request guards, the security headers and the error bodies."""

from __future__ import annotations

import logging
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from llm_fakes import QueuedReplyAdapter
from training_season.database import connect
from training_season.llm import LLMConfigurationError
from training_season.timeformat import TrainingTimezoneError
from training_season.web_app import STATIC_DIR, create_app
from training_season.web_responses import (
    DATABASE_FAILED_TEXT,
    SECURITY_HEADERS,
    SERVER_FAILED_TEXT,
)
from training_season.web_settings import (
    WebConfigurationError,
    WebDependencies,
    allowed_hosts_from_environment,
)
from web_fakes import (
    PLACEHOLDER_PAGE,
    FailingClock,
    RecordingAdapterFactory,
    build_web_app,
    web_environ,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from training_season.llm import LLMAdapter


class FailingAdapterFactory:
    """Fails like a bad `LLM_PROVIDER` setting."""

    def __call__(self, environ: Mapping[str, str]) -> LLMAdapter:
        raise LLMConfigurationError("LLM_PROVIDER must be openai_compatible")


def _assert_security_headers(headers: Mapping[str, str]) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert headers[name] == value


def test_root_serves_the_page_with_the_security_headers(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.text == PLACEHOLDER_PAGE
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; frame-ancestors 'none'"
    )
    _assert_security_headers(response.headers)


def test_foreign_host_is_refused(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        response = client.get("/api/state", headers={"Host": "attacker.example"})

    assert response.status_code == 400
    _assert_security_headers(response.headers)


def test_api_docs_are_not_served(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        statuses = [client.get(path).status_code for path in ("/docs", "/openapi.json")]

    assert statuses == [404, 404]


def test_database_failure_gives_503_with_the_fixed_text(tmp_path: Path) -> None:
    app = build_web_app(tmp_path)
    (tmp_path / "web.db").write_bytes(b"not a sqlite file" * 100)

    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "message": DATABASE_FAILED_TEXT}
    _assert_security_headers(response.headers)


def test_unknown_error_gives_500_without_its_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FailingClock()
    app = build_web_app(tmp_path, clock=clock)

    with TestClient(app) as client, caplog.at_level(logging.ERROR):
        response = client.get("/api/state")

    assert response.status_code == 500
    assert response.json() == {"status": "error", "message": SERVER_FAILED_TEXT}
    _assert_security_headers(response.headers)
    assert "GET /api/state failed: RuntimeError" in caplog.text
    assert clock.message not in caplog.text


def test_root_serves_the_packaged_page_and_its_files(tmp_path: Path) -> None:
    dependencies = WebDependencies(
        environ=web_environ(tmp_path), adapter_factory=RecordingAdapterFactory()
    )
    scripts = {
        "chat.js",
        "api.js",
        "checkin.js",
        "constants.js",
        "flow.js",
        "mfa.js",
        "onboarding.js",
        "plan.js",
        "render.js",
        "sync.js",
        "view.js",
    }
    styles = {"tokens.css", "base.css", "layout.css", "components.css", "plan.css"}

    with TestClient(create_app(dependencies)) as client:
        root = client.get("/")
        assets = {name: client.get(f"/{name}") for name in sorted(scripts | styles)}

    served = {
        name: (response.status_code, response.headers["content-type"].split(";")[0])
        for name, response in assets.items()
    }

    assert {path.name for path in STATIC_DIR.iterdir()} == {
        "index.html"
    } | scripts | styles
    assert root.status_code == 200
    assert root.text == (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # A system mime.types file can map .js to application/javascript; both run.
    assert {name: served[name] for name in styles} == dict.fromkeys(
        styles, (200, "text/css")
    )
    assert {name: served[name][0] for name in scripts} == dict.fromkeys(scripts, 200)
    assert {served[name][1] for name in scripts} <= {
        "text/javascript",
        "application/javascript",
    }
    _assert_security_headers(root.headers)


def test_create_app_migrates_and_closes_the_checked_adapter(tmp_path: Path) -> None:
    factory = RecordingAdapterFactory(QueuedReplyAdapter([]))

    build_web_app(tmp_path, adapter_factory=factory)

    assert factory.adapter.closed
    with closing(connect(tmp_path / "web.db")) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'message'"
        ).fetchall()
    assert tables == [("message",)]


def test_create_app_stops_on_a_bad_setting(tmp_path: Path) -> None:
    environ = web_environ(tmp_path)
    del environ["TZ"]

    with pytest.raises(TrainingTimezoneError):
        build_web_app(tmp_path, environ=environ)
    assert not (tmp_path / "web.db").exists()


def test_create_app_stops_on_a_bad_model_setting(tmp_path: Path) -> None:
    with pytest.raises(LLMConfigurationError):
        build_web_app(tmp_path, adapter_factory=FailingAdapterFactory())


def test_allowed_hosts_default_to_loopback() -> None:
    assert allowed_hosts_from_environment({}) == ("127.0.0.1", "localhost")
    assert allowed_hosts_from_environment({"WEB_ALLOWED_HOSTS": " "}) == (
        "127.0.0.1",
        "localhost",
    )


def test_allowed_hosts_are_a_trimmed_comma_list() -> None:
    environ = {"WEB_ALLOWED_HOSTS": " localhost , 192.168.0.9 ,"}

    assert allowed_hosts_from_environment(environ) == ("localhost", "192.168.0.9")


@pytest.mark.parametrize("raw_hosts", ["*", "*.example.com", ",", "local host"])
def test_allowed_hosts_refuse_a_wildcard_or_an_empty_list(raw_hosts: str) -> None:
    with pytest.raises(WebConfigurationError, match="WEB_ALLOWED_HOSTS") as caught:
        allowed_hosts_from_environment({"WEB_ALLOWED_HOSTS": raw_hosts})

    assert repr(raw_hosts) in str(caught.value)
