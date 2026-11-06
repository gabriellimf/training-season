"""Tests for the Garmin login: cached token, credential login and MFA outcomes."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from garminconnect.client import token_file_path
from garminconnect.exceptions import (
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_auth_fakes import (
    _FAKE_CODE,
    FakeGarminServer,
    _app_log_records,
    _begin_mfa,
    _chain_rejected_token_error,
    _write_fake_token,
)
from training_season.garmin_auth import (
    GarminLoginReason,
    GarminMfaCodeRejectedError,
    GarminMfaRequiredError,
    GarminRateLimitedError,
    GarminTokenExpiredError,
    GarminUnavailableError,
    garmin_session,
    submit_mfa_code,
)

if TYPE_CHECKING:
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory


def test_first_run_without_cache_logs_in_and_writes_token(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is GarminLoginReason.NO_TOKEN

    token_path = token_file_path(str(auth_config.token_dir))
    assert fake_server.credential_logins == 1
    assert fake_server.login_calls == ["credential", "token"]
    assert token_path.is_file()
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert auth_config.token_dir.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in auth_config.token_dir.iterdir()) == [
        "garmin.lock",
        "garmin_tokens.json",
    ]
    assert (auth_config.token_dir / "garmin.lock").stat().st_mode & 0o777 == 0o600


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
    assert fake_server.login_calls == ["credential", "token", "token"]


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
    assert fake_server.login_calls == ["token"]


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


@pytest.mark.parametrize(
    ("profile_error", "raised_directly"),
    [
        pytest.param(
            GarminConnectConnectionError("profile unavailable"),
            False,
            id="connection-error",
        ),
        pytest.param(ValueError("invalid profile response"), False, id="value-error"),
        pytest.param(
            GarminConnectConnectionError("profile unavailable"),
            True,
            id="direct-connection-error",
        ),
    ],
)
def test_mfa_accepted_but_profile_failed_keeps_token(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    profile_error: BaseException,
    *,
    raised_directly: bool,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)
    if raised_directly:
        fake_server.resume_error_after_token = profile_error
    else:
        fake_server.profile_error_after_resume = profile_error

    with pytest.raises(GarminUnavailableError, match="token is saved"):
        submit_mfa_code(auth_config, _FAKE_CODE, factory=factory)

    assert token_file_path(str(auth_config.token_dir)).is_file()
    assert not (auth_config.token_dir / "garmin_mfa_state.json").exists()


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
    assert fake_server.login_calls == ["token", "credential", "token"]
    assert "expired or revoked" in caplog.text
    assert _app_log_records(caplog) == [
        (
            "training_season.garmin_auth",
            logging.WARNING,
            "Garmin rejected the cached token (expired or revoked); logging in again",
        )
    ]


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
    assert fake_server.login_calls == ["token", "credential"]
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
        pytest.raises(GarminTokenExpiredError, match="not set") as raised,
        garmin_session(config_without_credentials, factory=factory),
    ):
        pass

    assert fake_server.credential_logins == 0
    assert str(raised.value) == (
        "Garmin rejected the cached token (expired or revoked), and "
        "GARMIN_EMAIL and GARMIN_PASSWORD are not set"
    )


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
