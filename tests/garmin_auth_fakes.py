"""Fake Garmin client, server and fixtures shared by the Garmin auth and pull tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from garminconnect import Garmin
from garminconnect.client import Client, token_file_path
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

from training_season.garmin_auth import (
    GarminAuthConfig,
    GarminMfaRequiredError,
    garmin_session,
    start_login,
)

if TYPE_CHECKING:
    from pathlib import Path

    from training_season.garmin_auth import GarminFactory

_FAKE_EMAIL = "runner@example.test"
_FAKE_PASSWORD = "not-a-real-password"  # noqa: S105
_FAKE_CODE = "000000"
_FAKE_TOKEN = "fake-access-token"  # noqa: S105
_FAKE_REFRESH_TOKEN = "fake-refresh-token"  # noqa: S105
_FAKE_CLIENT_ID = "fake-client-id"
_FAKE_COOKIE = "fake-cookie-value"


def _client_state(cookie: str = _FAKE_COOKIE) -> dict[str, Any]:
    return {
        "domain": "garmin.com",
        "flow": "ios",
        "method": "email",
        "login_params": {},
        "post_headers": {},
        "service_url": None,
        "csrf_token": None,
        "impersonate": None,
        "cookies": [
            {
                "name": "fake-session",
                "value": cookie,
                "domain": ".garmin.com",
                "path": "/",
                "secure": True,
                "expires": None,
                "domain_specified": True,
            }
        ],
    }


@dataclass
class FakeGarminServer:
    credential_outcomes: list[str | BaseException] = field(default_factory=list)
    token_login_errors: list[BaseException] = field(default_factory=list)
    resume_error: BaseException | None = None
    profile_error_after_resume: BaseException | None = None
    resume_error_after_token: BaseException | None = None
    credential_logins: int = 0
    token_logins: int = 0
    resumes: int = 0
    login_calls: list[str] = field(default_factory=list)
    issued_state: dict[str, Any] = field(default_factory=_client_state)
    valid_code: str = _FAKE_CODE


class FakeGarmin(Garmin):
    def __init__(
        self,
        server: FakeGarminServer,
        email: str | None,
        password: str | None,
    ) -> None:
        super().__init__(email=email, password=password, return_on_mfa=True)
        self.server = server

    def login(self, /, tokenstore: str | None = None) -> tuple[str | None, Any]:
        if tokenstore is not None:
            self.server.token_logins += 1
            self.server.login_calls.append("token")
            self.client.load(tokenstore)
            if self.server.token_login_errors:
                raise self.server.token_login_errors.pop(0)
            return None, None

        self.server.credential_logins += 1
        self.server.login_calls.append("credential")
        outcome = (
            self.server.credential_outcomes.pop(0)
            if self.server.credential_outcomes
            else "success"
        )
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "mfa":
            state = json.loads(json.dumps(self.server.issued_state))
            return "needs_mfa", state
        if outcome == "uncacheable":
            self.client.di_token = None
            self.client.di_refresh_token = None
            self.client.di_client_id = None
            return None, None
        if outcome != "success":
            raise AssertionError("unknown fake credential outcome")

        self.client.di_token = _FAKE_TOKEN
        self.client.di_refresh_token = _FAKE_REFRESH_TOKEN
        self.client.di_client_id = _FAKE_CLIENT_ID
        return None, None

    def resume_login(
        self,
        client_state: dict[str, Any],
        mfa_code: str,
    ) -> tuple[Any, Any]:
        self.server.resumes += 1
        self.server.login_calls.append("resume")
        if self.server.resume_error is not None:
            raise self.server.resume_error
        if client_state != json.loads(json.dumps(self.server.issued_state)):
            raise GarminConnectAuthenticationError(
                "Invalid MFA client_state; start the login again"
            ) from KeyError("missing field")
        if mfa_code != self.server.valid_code:
            raise GarminConnectAuthenticationError("MFA verification failed")

        self.client.di_token = _FAKE_TOKEN
        self.client.di_refresh_token = _FAKE_REFRESH_TOKEN
        self.client.di_client_id = _FAKE_CLIENT_ID
        if self.server.resume_error_after_token is not None:
            raise self.server.resume_error_after_token
        if self.server.profile_error_after_resume is not None:
            raise GarminConnectAuthenticationError(
                "Failed to retrieve social profile"
            ) from self.server.profile_error_after_resume
        return None, None


@pytest.fixture
def auth_config(tmp_path: Path) -> GarminAuthConfig:
    return GarminAuthConfig(
        tmp_path / "garmin",
        email=_FAKE_EMAIL,
        password=_FAKE_PASSWORD,
    )


@pytest.fixture
def fake_server() -> FakeGarminServer:
    return FakeGarminServer()


@pytest.fixture
def factory(fake_server: FakeGarminServer) -> GarminFactory:
    def create_garmin(email: str | None, password: str | None) -> Garmin:
        return FakeGarmin(fake_server, email, password)

    return create_garmin


def _write_fake_token(config: GarminAuthConfig, token: str = _FAKE_TOKEN) -> Path:
    client = Client()
    client.di_token = token
    client.di_refresh_token = _FAKE_REFRESH_TOKEN
    client.di_client_id = _FAKE_CLIENT_ID
    client.dump(str(config.token_dir))
    return token_file_path(str(config.token_dir))


def _chain_rejected_token_error(
    status_code: int = 401,
) -> GarminConnectAuthenticationError:
    connection_error = GarminConnectConnectionError(f"API Error {status_code}")
    connection_error.response = type("Response", (), {"status_code": status_code})()
    error = GarminConnectAuthenticationError("Failed to retrieve social profile")
    error.__cause__ = connection_error
    return error


def _app_log_records(
    caplog: pytest.LogCaptureFixture,
) -> list[tuple[str, int, str]]:
    return [
        (record.name, record.levelno, record.getMessage())
        for record in caplog.records
        if record.name.startswith("training_season")
    ]


def _begin_mfa(
    config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    *,
    use_session: bool = False,
) -> None:
    fake_server.credential_outcomes.append("mfa")
    if use_session:
        with (
            pytest.raises(GarminMfaRequiredError),
            garmin_session(
                config,
                factory=factory,
            ),
        ):
            pass
    else:
        with pytest.raises(GarminMfaRequiredError):
            start_login(config, factory=factory)
