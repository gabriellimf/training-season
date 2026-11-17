"""Garmin login and MFA routes: outcome, Garmin state, and the code never echoed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from garmin_auth_fakes import _FAKE_CODE, _FAKE_EMAIL, _FAKE_PASSWORD, _begin_mfa
from training_season.garmin_auth import GARMIN_EMAIL_ENV, GARMIN_PASSWORD_ENV
from training_season.web_responses import BUSY_TEXT, INVALID_REQUEST_TEXT
from web_fakes import build_web_app, context_of, web_environ

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory

_WRONG_CODE = "731942"


def _app(tmp_path: Path, factory: GarminFactory) -> FastAPI:
    environ = {
        **web_environ(tmp_path),
        GARMIN_EMAIL_ENV: _FAKE_EMAIL,
        GARMIN_PASSWORD_ENV: _FAKE_PASSWORD,
    }
    return build_web_app(tmp_path, environ=environ, garmin_factory=factory)


def test_accepted_code_saves_the_token(
    tmp_path: Path,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    app = _app(tmp_path, factory)
    _begin_mfa(auth_config, fake_server, factory)

    with TestClient(app) as client:
        body = client.post("/api/garmin/mfa", json={"code": _FAKE_CODE}).json()

    assert body == {
        "status": "connected",
        "message": "Login no Garmin concluído. O token foi salvo.",
        "garmin": {"state": "token_cached", "mfa_requested_at": None},
    }


def test_rejected_code_keeps_the_login_and_is_never_logged_or_echoed(
    tmp_path: Path,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _app(tmp_path, factory)
    _begin_mfa(auth_config, fake_server, factory)
    caplog.set_level(logging.DEBUG)

    with TestClient(app) as client:
        response = client.post("/api/garmin/mfa", json={"code": _WRONG_CODE})

    body = response.json()
    assert (body["status"], body["garmin"]["state"]) == ("code_rejected", "mfa_pending")
    assert _WRONG_CODE not in response.text
    assert _WRONG_CODE not in caplog.text


def test_login_that_needs_a_code_shows_the_pending_state(
    tmp_path: Path, fake_server: FakeGarminServer, factory: GarminFactory
) -> None:
    app = _app(tmp_path, factory)
    fake_server.credential_outcomes.append("mfa")

    with TestClient(app) as client:
        body = client.post("/api/garmin/login", json={}).json()

    assert (body["status"], body["garmin"]["state"]) == ("mfa_required", "mfa_pending")


@pytest.mark.parametrize(
    ("content_type", "content", "status_code"),
    [
        ("text/plain", f'{{"code":"{_WRONG_CODE}"}}', 415),
        ("application/json", f'{{"code":"{_WRONG_CODE * 6}"}}', 422),
    ],
    ids=["text_plain", "too_long"],
)
def test_bad_code_body_is_invalid_and_not_echoed(
    tmp_path: Path,
    factory: GarminFactory,
    content_type: str,
    content: str,
    status_code: int,
) -> None:
    with TestClient(_app(tmp_path, factory)) as client:
        response = client.post(
            "/api/garmin/mfa", content=content, headers={"Content-Type": content_type}
        )

    assert response.status_code == status_code
    assert response.json() == {"status": "invalid", "message": INVALID_REQUEST_TEXT}


def test_code_while_the_coach_works_is_busy(
    tmp_path: Path, factory: GarminFactory
) -> None:
    app = _app(tmp_path, factory)

    with context_of(app).work.run("week_one"), TestClient(app) as client:
        response = client.post("/api/garmin/mfa", json={"code": _FAKE_CODE})

    assert response.status_code == 409
    assert response.json() == {"status": "busy", "message": BUSY_TEXT}
