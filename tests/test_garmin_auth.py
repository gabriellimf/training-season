"""Tests for Garmin authentication and MFA handling."""

from __future__ import annotations

import json
import logging
import sys
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from garminconnect.client import token_file_path
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
)

from garmin_auth_fakes import (
    _FAKE_CLIENT_ID,
    _FAKE_CODE,
    _FAKE_COOKIE,
    _FAKE_EMAIL,
    _FAKE_PASSWORD,
    _FAKE_REFRESH_TOKEN,
    _FAKE_TOKEN,
    FakeGarminServer,
    _begin_mfa,
    _client_state,
    _write_fake_token,
)
from training_season import garmin_auth
from training_season.garmin_auth import (
    GarminAuthConfig,
    GarminAuthState,
    GarminConfigurationError,
    GarminFactory,
    GarminLoginReason,
    GarminMfaCodeRejectedError,
    GarminMfaNotPendingError,
    GarminMfaRequiredError,
    GarminTokenExpiredError,
    GarminUnavailableError,
    auth_status,
    garmin_auth_config_from_environment,
    garmin_session,
    start_login,
    submit_mfa_code,
)

if TYPE_CHECKING:
    from pathlib import Path


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


def test_config_rejects_garmintokens_variable(tmp_path: Path) -> None:
    with pytest.raises(GarminConfigurationError, match="GARMINTOKENS"):
        garmin_auth_config_from_environment(
            {
                "GARMIN_TOKEN_DIR": str(tmp_path / "garmin"),
                "GARMINTOKENS": "",
            }
        )


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


def test_session_translates_library_errors_from_body(
    auth_config: GarminAuthConfig,
    factory: GarminFactory,
) -> None:
    with (
        pytest.raises(GarminTokenExpiredError),
        garmin_session(auth_config, factory=factory),
    ):
        raise GarminConnectAuthenticationError("expired")


@pytest.mark.parametrize("status_code", [401, 403])
def test_session_translates_rejected_status_from_body(
    auth_config: GarminAuthConfig,
    factory: GarminFactory,
    status_code: int,
) -> None:
    error = GarminConnectConnectionError(f"API Error {status_code}")
    error.response = type("Response", (), {"status_code": status_code})()

    with (
        pytest.raises(GarminTokenExpiredError),
        garmin_session(auth_config, factory=factory),
    ):
        raise error


def test_session_translates_not_found_from_body_to_unavailable(
    auth_config: GarminAuthConfig,
    factory: GarminFactory,
) -> None:
    error = GarminConnectNotFoundError("missing resource")
    error.response = type("Response", (), {"status_code": 404})()

    with (
        pytest.raises(GarminUnavailableError),
        garmin_session(auth_config, factory=factory),
    ):
        raise error


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
