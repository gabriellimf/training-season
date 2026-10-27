"""Tests for Garmin authentication and MFA handling."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field, replace
from io import StringIO
from typing import TYPE_CHECKING, Any

import pytest
from garminconnect import Garmin
from garminconnect.client import Client, token_file_path
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from training_season import garmin_auth
from training_season.garmin_auth import (
    GarminAuthConfig,
    GarminAuthState,
    GarminConfigurationError,
    GarminError,
    GarminFactory,
    GarminLockTimeoutError,
    GarminLoginReason,
    GarminLoginRefusedError,
    GarminMfaCodeRejectedError,
    GarminMfaNotPendingError,
    GarminMfaRequiredError,
    GarminMfaStateInvalidError,
    GarminRateLimitedError,
    GarminTokenCacheError,
    GarminTokenExpiredError,
    GarminUnavailableError,
    auth_status,
    garmin_auth_config_from_environment,
    garmin_session,
    start_login,
    submit_mfa_code,
)
from training_season.timeformat import utc_now

if TYPE_CHECKING:
    from pathlib import Path

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
    credential_logins: int = 0
    token_logins: int = 0
    resumes: int = 0
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
            self.client.load(tokenstore)
            if self.server.token_login_errors:
                raise self.server.token_login_errors.pop(0)
            return None, None

        self.server.credential_logins += 1
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
        if self.server.profile_error_after_resume is not None:
            raise self.server.profile_error_after_resume
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


def test_config_reads_token_dir_and_credentials(tmp_path: Path) -> None:
    token_dir = tmp_path / "garmin"
    config = garmin_auth_config_from_environment(
        {
            "GARMIN_TOKEN_DIR": str(token_dir),
            "GARMIN_EMAIL": _FAKE_EMAIL,
            "GARMIN_PASSWORD": _FAKE_PASSWORD,
        }
    )

    assert config == GarminAuthConfig(token_dir, _FAKE_EMAIL, _FAKE_PASSWORD)


def test_config_token_dir_unset_raises() -> None:
    with pytest.raises(GarminConfigurationError, match="GARMIN_TOKEN_DIR"):
        garmin_auth_config_from_environment({})


@pytest.mark.parametrize("value", ["", "   "])
def test_config_token_dir_blank_raises(value: str) -> None:
    with pytest.raises(GarminConfigurationError, match="GARMIN_TOKEN_DIR"):
        garmin_auth_config_from_environment({"GARMIN_TOKEN_DIR": value})


def test_config_token_dir_relative_raises() -> None:
    with pytest.raises(GarminConfigurationError, match="absolute path"):
        garmin_auth_config_from_environment(
            {"GARMIN_TOKEN_DIR": "training-season-auth"}
        )


def test_config_token_dir_missing_parent_raises(tmp_path: Path) -> None:
    missing_parent = tmp_path / "missing"
    with pytest.raises(GarminConfigurationError, match="parent directory"):
        garmin_auth_config_from_environment(
            {"GARMIN_TOKEN_DIR": str(missing_parent / "garmin")}
        )


def test_config_token_dir_is_file_raises(tmp_path: Path) -> None:
    token_path = tmp_path / "garmin"
    token_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(GarminConfigurationError, match="must name a directory"):
        garmin_auth_config_from_environment({"GARMIN_TOKEN_DIR": str(token_path)})


def test_config_rejects_garmintokens_variable(tmp_path: Path) -> None:
    with pytest.raises(GarminConfigurationError, match="GARMINTOKENS"):
        garmin_auth_config_from_environment(
            {
                "GARMIN_TOKEN_DIR": str(tmp_path / "garmin"),
                "GARMINTOKENS": "",
            }
        )


def test_config_repr_hides_credentials(auth_config: GarminAuthConfig) -> None:
    config_repr = repr(auth_config)

    assert _FAKE_EMAIL not in config_repr
    assert _FAKE_PASSWORD not in config_repr


def test_first_run_without_cache_logs_in_and_writes_token(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is GarminLoginReason.NO_TOKEN

    token_path = token_file_path(str(auth_config.token_dir))
    assert fake_server.credential_logins == 1
    assert token_path.is_file()
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert auth_config.token_dir.stat().st_mode & 0o777 == 0o700


def test_second_run_reuses_token_without_credential_login(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    with garmin_session(auth_config, factory=factory) as first_session:
        assert first_session.login_reason is GarminLoginReason.NO_TOKEN
    with garmin_session(auth_config, factory=factory) as second_session:
        assert second_session.login_reason is None

    assert fake_server.credential_logins == 1
    assert fake_server.token_logins == 2


def test_second_run_works_without_credentials_in_environment(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _write_fake_token(auth_config)
    config_without_credentials = replace(auth_config, email=None, password=None)

    with garmin_session(config_without_credentials, factory=factory) as session:
        assert session.login_reason is None

    assert fake_server.credential_logins == 0


def test_mfa_required_saves_state_and_raises(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory, use_session=True)

    state_path = auth_config.token_dir / "garmin_mfa_state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert data["version"] == 1
    assert data["reason"] == GarminLoginReason.NO_TOKEN.value
    assert isinstance(data["created_at"], str)
    assert data["client_state"] == fake_server.issued_state


def test_mfa_resume_in_new_instance_uses_only_disk_state(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory, use_session=True)
    state_path = auth_config.token_dir / "garmin_mfa_state.json"

    submit_mfa_code(auth_config, _FAKE_CODE, factory=factory)

    assert fake_server.resumes == 1
    assert not state_path.exists()
    assert token_file_path(str(auth_config.token_dir)).is_file()
    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is None
    assert fake_server.credential_logins == 1


def test_mfa_pending_blocks_new_credential_login(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)
    login_count = fake_server.credential_logins

    with (
        pytest.raises(GarminMfaRequiredError),
        garmin_session(
            auth_config,
            factory=factory,
        ),
    ):
        pass

    assert fake_server.credential_logins == login_count


def test_start_login_discards_pending_state_and_requests_new_code(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)
    fake_server.issued_state = _client_state("replacement-cookie")
    fake_server.credential_outcomes.append("mfa")

    with pytest.raises(GarminMfaRequiredError) as error:
        start_login(auth_config, factory=factory)

    data = json.loads(
        (auth_config.token_dir / "garmin_mfa_state.json").read_text(encoding="utf-8")
    )
    assert data["reason"] == GarminLoginReason.MANUAL.value
    assert data["client_state"] == fake_server.issued_state
    assert error.value.reason is GarminLoginReason.MANUAL
    assert fake_server.credential_logins == 2


def test_mfa_wrong_code_keeps_pending_state(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)

    with pytest.raises(GarminMfaCodeRejectedError):
        submit_mfa_code(auth_config, "999999", factory=factory)

    assert (auth_config.token_dir / "garmin_mfa_state.json").is_file()
    assert fake_server.resumes == 1


def test_mfa_rate_limited_keeps_pending_state(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)
    fake_server.resume_error = GarminConnectTooManyRequestsError("rate limited")

    with pytest.raises(GarminRateLimitedError):
        submit_mfa_code(auth_config, _FAKE_CODE, factory=factory)

    assert (auth_config.token_dir / "garmin_mfa_state.json").is_file()
    assert fake_server.resumes == 1


def test_mfa_blank_code_rejected_without_garmin_call(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    with pytest.raises(GarminMfaCodeRejectedError, match="cannot be empty"):
        submit_mfa_code(auth_config, "   ", factory=factory)

    assert fake_server.resumes == 0
    assert fake_server.credential_logins == 0


def test_mfa_submit_without_pending_state_raises(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    with pytest.raises(GarminMfaNotPendingError, match="No Garmin MFA"):
        submit_mfa_code(auth_config, _FAKE_CODE, factory=factory)

    assert fake_server.resumes == 0


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param("not JSON", id="not-json"),
        pytest.param(
            (
                '{"version": 2, "created_at": "' + utc_now() + '", "reason": "manual", '
                '"client_state": {"domain": "garmin.com"}}'
            ),
            id="wrong-version",
        ),
        pytest.param(
            (
                '{"version": 1, "created_at": "'
                + utc_now()
                + '", "reason": "manual", "client_state": {}}'
            ),
            id="empty-client-state",
        ),
    ],
)
def test_mfa_corrupt_state_file_is_deleted_and_reported(
    auth_config: GarminAuthConfig,
    contents: str,
) -> None:
    auth_config.token_dir.mkdir(mode=0o700)
    state_path = auth_config.token_dir / "garmin_mfa_state.json"
    state_path.write_text(contents, encoding="utf-8")

    with pytest.raises(GarminMfaStateInvalidError):
        submit_mfa_code(auth_config, _FAKE_CODE)

    assert not state_path.exists()


def test_mfa_accepted_but_profile_failed_keeps_token(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)
    fake_server.profile_error_after_resume = GarminConnectConnectionError(
        "profile unavailable"
    )

    with pytest.raises(GarminUnavailableError, match="token is saved"):
        submit_mfa_code(auth_config, _FAKE_CODE, factory=factory)

    assert token_file_path(str(auth_config.token_dir)).is_file()
    assert not (auth_config.token_dir / "garmin_mfa_state.json").exists()


def test_cli_login_then_mfa_in_separate_invocations(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(auth_config.token_dir))
    monkeypatch.setenv("GARMIN_EMAIL", _FAKE_EMAIL)
    monkeypatch.setenv("GARMIN_PASSWORD", _FAKE_PASSWORD)
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    real_start_login = garmin_auth.start_login
    real_submit_mfa_code = garmin_auth.submit_mfa_code

    def start_with_fake(config: GarminAuthConfig) -> None:
        real_start_login(config, factory=factory)

    def submit_with_fake(config: GarminAuthConfig, code: str) -> None:
        real_submit_mfa_code(config, code, factory=factory)

    monkeypatch.setattr(garmin_auth, "start_login", start_with_fake)
    monkeypatch.setattr(garmin_auth, "submit_mfa_code", submit_with_fake)
    fake_server.credential_outcomes.append("mfa")

    assert garmin_auth.main(["login"]) == 3
    monkeypatch.setattr(sys, "stdin", StringIO(f"{_FAKE_CODE}\n"))
    assert garmin_auth.main(["mfa"]) == 0
    assert token_file_path(str(auth_config.token_dir)).is_file()
    assert fake_server.resumes == 1


@pytest.mark.parametrize("command", ["mfa", "import-token"])
def test_cli_reports_stdin_read_error(
    command: str,
    auth_config: GarminAuthConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class BrokenStdin:
        def read(self) -> str:
            raise OSError("fake input error")

        def readline(self) -> str:
            raise OSError("fake input error")

    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(auth_config.token_dir))
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    monkeypatch.setattr(sys, "stdin", BrokenStdin())

    assert garmin_auth.main([command]) == 1
    assert capsys.readouterr().err == "Could not read command input from stdin\n"


def test_rejected_token_falls_back_to_credential_login(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_fake_token(auth_config)
    fake_server.token_login_errors.append(_chain_rejected_token_error())
    caplog.set_level(logging.WARNING)

    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is GarminLoginReason.TOKEN_REJECTED

    assert fake_server.credential_logins == 1
    assert "expired or revoked" in caplog.text


def test_rejected_token_with_mfa_raises_mfa_required_naming_expiry(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _write_fake_token(auth_config)
    fake_server.token_login_errors.append(_chain_rejected_token_error())
    fake_server.credential_outcomes.append("mfa")

    with (
        pytest.raises(GarminMfaRequiredError, match="expired or revoked") as error,
        garmin_session(auth_config, factory=factory),
    ):
        pass

    data = json.loads(
        (auth_config.token_dir / "garmin_mfa_state.json").read_text(encoding="utf-8")
    )
    assert error.value.reason is GarminLoginReason.TOKEN_REJECTED
    assert data["reason"] == GarminLoginReason.TOKEN_REJECTED.value


def test_rejected_token_without_credentials_raises_token_expired(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _write_fake_token(auth_config)
    fake_server.token_login_errors.append(_chain_rejected_token_error())
    config_without_credentials = replace(auth_config, email=None, password=None)

    with (
        pytest.raises(GarminTokenExpiredError, match="not set"),
        garmin_session(config_without_credentials, factory=factory),
    ):
        pass

    assert fake_server.credential_logins == 0


def test_unreadable_token_file_triggers_credential_login(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    auth_config.token_dir.mkdir()
    token_file_path(str(auth_config.token_dir)).write_text("{", encoding="utf-8")
    caplog.set_level(logging.WARNING)

    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is GarminLoginReason.TOKEN_UNREADABLE

    assert fake_server.credential_logins == 1
    assert "token cache is unreadable" in caplog.text


def test_outage_during_token_login_raises_unavailable_without_credential_login(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _write_fake_token(auth_config)
    fake_server.token_login_errors.append(
        GarminConnectConnectionError("network unavailable")
    )

    with (
        pytest.raises(GarminUnavailableError, match="unreachable"),
        garmin_session(auth_config, factory=factory),
    ):
        pass

    assert fake_server.credential_logins == 0


def test_uncacheable_session_raises_token_cache_error(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    fake_server.credential_outcomes.append("uncacheable")

    with (
        pytest.raises(GarminTokenCacheError, match="cannot be cached"),
        garmin_session(auth_config, factory=factory),
    ):
        pass


@pytest.mark.parametrize(
    ("failure_point", "library_error", "expected_error", "message_part"),
    [
        (
            "credential",
            GarminConnectAuthenticationError("rejected"),
            GarminLoginRefusedError,
            "refused",
        ),
        (
            "credential",
            GarminConnectTooManyRequestsError("limited"),
            GarminRateLimitedError,
            "rate-limited",
        ),
        (
            "credential",
            GarminConnectConnectionError("offline"),
            GarminUnavailableError,
            "unreachable",
        ),
        (
            "token",
            GarminConnectAuthenticationError("profile failed"),
            GarminUnavailableError,
            "profile could not be loaded",
        ),
        (
            "token",
            GarminConnectTooManyRequestsError("limited"),
            GarminRateLimitedError,
            "rate-limited",
        ),
        (
            "token",
            GarminConnectConnectionError("offline"),
            GarminUnavailableError,
            "unreachable",
        ),
        (
            "resume",
            GarminConnectAuthenticationError("bad code"),
            GarminMfaCodeRejectedError,
            "rejected the MFA code",
        ),
        (
            "resume",
            GarminConnectTooManyRequestsError("limited"),
            GarminRateLimitedError,
            "rate-limited",
        ),
        (
            "resume",
            GarminConnectConnectionError("offline"),
            GarminUnavailableError,
            "unreachable",
        ),
        (
            "body",
            GarminConnectAuthenticationError("expired"),
            GarminTokenExpiredError,
            "rejected the authenticated session",
        ),
        (
            "body",
            GarminConnectTooManyRequestsError("limited"),
            GarminRateLimitedError,
            "rate-limited",
        ),
        (
            "body",
            GarminConnectConnectionError("offline"),
            GarminUnavailableError,
            "unreachable",
        ),
    ],
)
def test_library_errors_map_to_named_causes(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    failure_point: str,
    library_error: BaseException,
    expected_error: type[GarminError],
    message_part: str,
) -> None:
    def trigger_failure() -> None:
        if failure_point == "credential":
            fake_server.credential_outcomes.append(library_error)
            start_login(auth_config, factory=factory)
        elif failure_point == "token":
            _write_fake_token(auth_config)
            fake_server.token_login_errors.append(library_error)
            with garmin_session(auth_config, factory=factory):
                pass
        elif failure_point == "resume":
            _begin_mfa(auth_config, fake_server, factory)
            fake_server.resume_error = library_error
            submit_mfa_code(auth_config, _FAKE_CODE, factory=factory)
        else:
            with garmin_session(auth_config, factory=factory):
                raise library_error

    with pytest.raises(expected_error) as error:
        trigger_failure()

    assert message_part in str(error.value)


def test_errors_logs_and_cli_output_never_contain_secrets(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_state = json.dumps(fake_server.issued_state)
    secret_error = (
        f"{_FAKE_EMAIL} {_FAKE_PASSWORD} {_FAKE_CODE} "
        f"{_FAKE_TOKEN} {_FAKE_COOKIE} {secret_state}"
    )
    fake_server.credential_outcomes.append(
        GarminConnectAuthenticationError(secret_error)
    )
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(auth_config.token_dir))
    monkeypatch.setenv("GARMIN_EMAIL", _FAKE_EMAIL)
    monkeypatch.setenv("GARMIN_PASSWORD", _FAKE_PASSWORD)
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    real_start_login = garmin_auth.start_login

    def start_with_fake(config: GarminAuthConfig) -> None:
        real_start_login(config, factory=factory)

    monkeypatch.setattr(garmin_auth, "start_login", start_with_fake)
    caplog.set_level(logging.DEBUG)
    assert garmin_auth.main(["login"]) == 1
    first_output = capsys.readouterr()

    fake_server.credential_outcomes.append("mfa")
    assert garmin_auth.main(["login"]) == 3
    capsys.readouterr()
    fake_server.resume_error = GarminConnectAuthenticationError(
        f"failed {_FAKE_CODE} {_FAKE_TOKEN} {_FAKE_COOKIE}"
    )
    real_submit_mfa_code = garmin_auth.submit_mfa_code

    def submit_with_fake(config: GarminAuthConfig, code: str) -> None:
        real_submit_mfa_code(config, code, factory=factory)

    monkeypatch.setattr(garmin_auth, "submit_mfa_code", submit_with_fake)
    monkeypatch.setattr(sys, "stdin", StringIO(f"{_FAKE_CODE}\n"))
    assert garmin_auth.main(["mfa"]) == 1
    second_output = capsys.readouterr()
    all_output = (
        first_output.out
        + first_output.err
        + second_output.out
        + second_output.err
        + caplog.text
    )

    for secret in (
        _FAKE_EMAIL,
        _FAKE_PASSWORD,
        _FAKE_CODE,
        _FAKE_TOKEN,
        _FAKE_COOKIE,
        secret_state,
    ):
        assert secret not in all_output


def test_token_dir_symlink_raises_token_cache_error(
    tmp_path: Path,
    factory: GarminFactory,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    token_dir = tmp_path / "linked"
    token_dir.symlink_to(target, target_is_directory=True)
    config = GarminAuthConfig(token_dir, _FAKE_EMAIL, _FAKE_PASSWORD)

    with (
        pytest.raises(GarminTokenCacheError),
        garmin_session(
            config,
            factory=factory,
        ),
    ):
        pass


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write through directory modes")
def test_unwritable_token_dir_raises_token_cache_error(
    tmp_path: Path,
    factory: GarminFactory,
) -> None:
    parent = tmp_path / "read-only"
    parent.mkdir()
    parent.chmod(0o500)
    config = GarminAuthConfig(parent / "garmin", _FAKE_EMAIL, _FAKE_PASSWORD)

    try:
        with (
            pytest.raises(GarminTokenCacheError),
            garmin_session(
                config,
                factory=factory,
            ),
        ):
            pass
    finally:
        parent.chmod(0o700)


def test_lock_timeout_raises_lock_timeout_error(
    auth_config: GarminAuthConfig,
    factory: GarminFactory,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    thread_errors: list[BaseException] = []

    def hold_session() -> None:
        try:
            with garmin_session(auth_config, factory=factory):
                entered.set()
                if not release.wait(timeout=5):
                    thread_errors.append(TimeoutError("test lock release timed out"))
        except (GarminError, TimeoutError) as error:
            thread_errors.append(error)

    thread = threading.Thread(target=hold_session)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(GarminLockTimeoutError):
            submit_mfa_code(
                auth_config,
                _FAKE_CODE,
                lock_timeout=0.1,
                factory=factory,
            )
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert thread_errors == []


def test_concurrent_logins_start_one_credential_login(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    barrier = threading.Barrier(2)
    errors: list[GarminMfaRequiredError] = []

    def run_login() -> None:
        barrier.wait(timeout=5)
        try:
            with garmin_session(
                auth_config,
                lock_timeout=5,
                factory=factory,
            ):
                pass
        except GarminMfaRequiredError as error:
            errors.append(error)

    fake_server.credential_outcomes.append("mfa")
    threads = [threading.Thread(target=run_login) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert fake_server.credential_logins == 1
    assert len(errors) == 2


def test_session_translates_library_errors_from_body(
    auth_config: GarminAuthConfig,
    factory: GarminFactory,
) -> None:
    with (
        pytest.raises(GarminTokenExpiredError),
        garmin_session(auth_config, factory=factory),
    ):
        raise GarminConnectAuthenticationError("expired")


def test_import_token_writes_cache_from_stdin(
    auth_config: GarminAuthConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(auth_config.token_dir))
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(
            json.dumps(
                {
                    "di_token": _FAKE_TOKEN,
                    "di_refresh_token": _FAKE_REFRESH_TOKEN,
                    "di_client_id": _FAKE_CLIENT_ID,
                }
            )
        ),
    )

    assert garmin_auth.main(["import-token"]) == 0
    assert auth_status(auth_config).state is GarminAuthState.TOKEN_CACHED
    assert token_file_path(str(auth_config.token_dir)).stat().st_mode & 0o777 == 0o600


def test_import_token_rejects_invalid_json_without_echoing_input(
    auth_config: GarminAuthConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid_json = f'{{"di_token": "{_FAKE_TOKEN}",'
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(auth_config.token_dir))
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    monkeypatch.setattr(sys, "stdin", StringIO(invalid_json))
    caplog.set_level(logging.DEBUG)

    assert garmin_auth.main(["import-token"]) == 1
    output = capsys.readouterr()

    assert invalid_json not in output.out + output.err + caplog.text
    assert _FAKE_TOKEN not in output.out + output.err + caplog.text


def test_auth_status_reports_each_state(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    assert auth_status(auth_config).state is GarminAuthState.NO_TOKEN

    _write_fake_token(auth_config)
    assert auth_status(auth_config).state is GarminAuthState.TOKEN_CACHED

    (token_file_path(str(auth_config.token_dir))).unlink()
    _begin_mfa(auth_config, fake_server, factory)
    status = auth_status(auth_config)
    assert status.state is GarminAuthState.MFA_PENDING
    assert status.mfa_requested_at is not None


def test_cached_token_session_has_no_login_reason(
    auth_config: GarminAuthConfig,
    factory: GarminFactory,
) -> None:
    _write_fake_token(auth_config)

    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is None


def test_first_run_session_login_reason_is_no_token(
    auth_config: GarminAuthConfig,
    factory: GarminFactory,
) -> None:
    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is GarminLoginReason.NO_TOKEN


def test_rejected_token_relogin_is_signalled(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _write_fake_token(auth_config)
    fake_server.token_login_errors.append(_chain_rejected_token_error())

    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is GarminLoginReason.TOKEN_REJECTED


def test_unreadable_token_relogin_is_signalled(
    auth_config: GarminAuthConfig,
    factory: GarminFactory,
) -> None:
    auth_config.token_dir.mkdir()
    token_file_path(str(auth_config.token_dir)).write_text("invalid", encoding="utf-8")

    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is GarminLoginReason.TOKEN_UNREADABLE
