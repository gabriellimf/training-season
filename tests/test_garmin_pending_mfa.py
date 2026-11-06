"""Tests for the pending MFA state: the state file and the code submit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from garminconnect.client import token_file_path

from garmin_auth_fakes import _FAKE_CODE, FakeGarminServer, _begin_mfa
from training_season.garmin_auth import (
    GarminLoginReason,
    GarminMfaStateInvalidError,
    garmin_session,
    submit_mfa_code,
)
from training_season.timeformat import utc_now

if TYPE_CHECKING:
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory


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
    assert state_path.read_text(encoding="utf-8") == json.dumps(
        {
            "version": 1,
            "created_at": data["created_at"],
            "reason": "no_token",
            "client_state": fake_server.issued_state,
        },
        separators=(",", ":"),
        ensure_ascii=True,
    )
    assert sorted(p.name for p in auth_config.token_dir.iterdir()) == [
        "garmin.lock",
        "garmin_mfa_state.json",
    ]
    assert (auth_config.token_dir / "garmin.lock").stat().st_mode & 0o777 == 0o600


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
    assert fake_server.login_calls == ["credential", "resume", "token"]


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
