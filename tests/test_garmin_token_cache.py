"""Tests for the Garmin token cache: directory checks, cache file and lock."""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

import pytest
from garminconnect.client import token_file_path

from garmin_auth_fakes import (
    _FAKE_CODE,
    _FAKE_EMAIL,
    _FAKE_PASSWORD,
    FakeGarminServer,
    _app_log_records,
)
from training_season.garmin_auth import (
    GarminAuthConfig,
    GarminConfigurationError,
    GarminError,
    GarminLockTimeoutError,
    GarminLoginReason,
    GarminMfaRequiredError,
    GarminTokenCacheError,
    garmin_auth_config_from_environment,
    garmin_session,
    submit_mfa_code,
)

if TYPE_CHECKING:
    from pathlib import Path

    from training_season.garmin_auth import GarminFactory


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
    assert fake_server.login_calls == ["credential", "token"]
    assert "token cache is unreadable" in caplog.text
    assert _app_log_records(caplog) == [
        (
            "training_season.garmin_auth",
            logging.WARNING,
            "Garmin token cache is unreadable; logging in again",
        )
    ]


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


def test_unreadable_token_relogin_is_signalled(
    auth_config: GarminAuthConfig,
    factory: GarminFactory,
) -> None:
    auth_config.token_dir.mkdir()
    token_file_path(str(auth_config.token_dir)).write_text("invalid", encoding="utf-8")

    with garmin_session(auth_config, factory=factory) as session:
        assert session.login_reason is GarminLoginReason.TOKEN_UNREADABLE
