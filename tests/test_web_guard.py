"""The request guard: only same-site JSON requests of a bounded size reach a route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from garmin_push_fakes import FakeGarminCalendar, push_factory_for
from training_season.web_request_guard import MAX_BODY_BYTES
from training_season.web_responses import INVALID_REQUEST_TEXT, SECURITY_HEADERS
from web_fakes import RecordingAdapterFactory, build_web_app, web_database

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx2 import Response

    from garmin_auth_fakes import FakeGarminServer

_INVALID_BODY = {"status": "invalid", "message": INVALID_REQUEST_TEXT}
_BODYLESS_ROUTES = (
    "/api/week-one",
    "/api/garmin/login",
    "/api/onboarding/discard",
    "/api/checkin/end",
)
_NOT_JSON = [
    {"Content-Type": "text/plain"},
    {"Content-Type": "application/x-www-form-urlencoded"},
    {},
]


def _message_count(tmp_path: Path) -> int:
    with web_database(tmp_path) as connection:
        return int(connection.execute("SELECT count(*) FROM message").fetchone()[0])


def _assert_refused(status_code: int, response: Response) -> None:
    assert response.status_code == status_code
    assert response.json() == _INVALID_BODY
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


@pytest.mark.parametrize("path", _BODYLESS_ROUTES)
@pytest.mark.parametrize("headers", _NOT_JSON, ids=["text", "form", "none"])
def test_bodyless_route_refuses_a_body_that_is_not_json_and_does_not_run(
    tmp_path: Path, fake_server: FakeGarminServer, path: str, headers: dict[str, str]
) -> None:
    adapters = RecordingAdapterFactory()
    factory, clients = push_factory_for(fake_server, FakeGarminCalendar())
    app = build_web_app(tmp_path, adapter_factory=adapters, garmin_factory=factory)

    with TestClient(app) as client:
        response = client.post(path, content=b"{}", headers=headers)

    _assert_refused(415, response)
    assert len(adapters.calls) == 1
    assert clients == []
    assert fake_server.login_calls == []
    assert _message_count(tmp_path) == 0


def test_json_body_without_a_content_type_does_not_reach_the_mfa_route(
    tmp_path: Path, fake_server: FakeGarminServer
) -> None:
    factory, clients = push_factory_for(fake_server, FakeGarminCalendar())
    app = build_web_app(tmp_path, garmin_factory=factory)

    with TestClient(app) as client:
        response = client.post("/api/garmin/mfa", content=b'{"code": "123456"}')

    _assert_refused(415, response)
    assert clients == []
    assert app.router.strict_content_type is True


def test_json_with_a_charset_reaches_the_route(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        response = client.post(
            "/api/checkin/end",
            content=b"{}",
            headers={"Content-Type": "Application/JSON; charset=utf-8"},
        )

    assert response.json()["status"] == "ended"


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
    ids=["foreign-origin", "null-origin", "cross-site", "same-site"],
)
def test_json_from_a_foreign_page_is_refused(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        response = client.post("/api/checkin/end", json={}, headers=headers)

    _assert_refused(403, response)


def test_json_from_the_page_itself_reaches_the_route(tmp_path: Path) -> None:
    headers = {"Origin": "http://testserver:8000", "Sec-Fetch-Site": "same-origin"}

    with TestClient(build_web_app(tmp_path)) as client:
        response = client.post("/api/checkin/end", json={}, headers=headers)

    assert response.status_code == 200


def test_a_read_needs_no_json_type_and_ignores_the_origin(tmp_path: Path) -> None:
    headers = {"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"}

    with TestClient(build_web_app(tmp_path)) as client:
        response = client.get("/api/state", headers=headers)

    assert response.status_code == 200


def test_a_body_above_the_limit_is_refused(tmp_path: Path) -> None:
    body = b'{"text": "' + b"a" * MAX_BODY_BYTES + b'"}'

    with TestClient(build_web_app(tmp_path)) as client:
        response = client.post(
            "/api/onboarding/messages",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    _assert_refused(413, response)


def _chunks() -> Iterator[bytes]:
    yield b'{"text": '
    yield b'"Oi"}'


def test_a_body_without_a_length_is_refused(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        response = client.post(
            "/api/onboarding/messages",
            content=_chunks(),
            headers={"Content-Type": "application/json"},
        )

    _assert_refused(411, response)


def test_an_unknown_api_path_gives_the_portuguese_404(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        response = client.get("/api/unknown")

    _assert_refused(404, response)


def test_a_wrong_api_method_gives_the_portuguese_405(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        response = client.post("/api/state", json={})

    _assert_refused(405, response)


def test_api_answers_are_not_stored_by_the_browser(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        read = client.get("/api/state")
        refused = client.post("/api/checkin/end", content=b"{}")
        page = client.get("/")

    assert read.status_code == 200
    assert read.headers["cache-control"] == "no-store"
    _assert_refused(415, refused)
    assert refused.headers["cache-control"] == "no-store"
    assert page.status_code == 200
    assert "cache-control" not in page.headers


def test_a_missing_page_file_keeps_the_static_404(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        response = client.get("/missing.js")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
