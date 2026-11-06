"""Tests for the Garmin pull command line entry point."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from training_season import garmin_pull
from training_season.garmin_auth import (
    GarminConfigurationError,
    GarminLoginReason,
    GarminMfaRequiredError,
    GarminRateLimitedError,
)
from training_season.garmin_pull import GarminPullResult


def _configure_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "training.db"))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.delenv("GARMIN_HISTORY_START", raising=False)
    monkeypatch.setattr(garmin_pull, "initialize_database", lambda: None)
    monkeypatch.setattr(
        garmin_pull,
        "connect",
        lambda _path: sqlite3.connect(":memory:", autocommit=True),
    )


def test_cli_prints_counts_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    result = GarminPullResult(
        login_reason=None,
        activity_start="2026-10-20",
        activity_end="2026-10-28",
        activities_added=2,
        activities_skipped=1,
        activities_invalid=0,
        wellness_start="2026-10-20",
        wellness_end="2026-10-27",
        wellness_days_written=3,
    )
    monkeypatch.setattr(garmin_pull, "pull_garmin", lambda *_args, **_kwargs: result)

    assert garmin_pull.main([]) == 0
    assert capsys.readouterr().out == (
        "activities 2026-10-20..2026-10-28: added 2, skipped 1, invalid 0; "
        "wellness 2026-10-20..2026-10-27: 3 days\n"
    )


@pytest.mark.parametrize(
    ("error", "environment", "expected_code"),
    [
        (
            GarminMfaRequiredError(GarminLoginReason.MANUAL, "requested"),
            {},
            3,
        ),
        (GarminConfigurationError("invalid configuration"), {}, 2),
        (None, {"TZ": "not/a-timezone"}, 2),
        (None, {"GARMIN_HISTORY_START": "not-a-date"}, 2),
        (GarminRateLimitedError("rate limited"), {}, 1),
        (sqlite3.Error("database failure"), {}, 1),
    ],
)
def test_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: BaseException | None,
    environment: dict[str, str],
    expected_code: int,
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    def fail(*_args: Any, **_kwargs: Any) -> GarminPullResult:
        if error is None:
            raise AssertionError("pull should not run for invalid configuration")
        raise error

    monkeypatch.setattr(garmin_pull, "pull_garmin", fail)

    assert garmin_pull.main([]) == expected_code
