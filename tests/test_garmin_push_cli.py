"""Tests for the Garmin push command line entry point."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
import requests

from garmin_auth_fakes import _write_fake_token
from garmin_push_fakes import FakeGarminCalendar, push_factory_for, week_text
from training_season import garmin_push_cli
from training_season.garmin_auth import (
    GarminConfigurationError,
    GarminLoginReason,
    GarminMfaRequiredError,
    GarminRateLimitedError,
)
from training_season.garmin_push import show_garmin_week
from training_season.push_result import PushFailure, PushResult, PushStep
from training_season.push_store import NoActiveMacroPlanError

if TYPE_CHECKING:
    from pathlib import Path

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig

_USAGE = "Usage: training-season-push <week.json> | show <YYYY-MM-DD>\n"
_LANDED = PushResult(date(2027, 1, 4), (date(2027, 1, 4),), None, ())


def _configure_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "training.db"))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setattr(garmin_push_cli, "initialize_database", lambda: None)
    monkeypatch.setattr(
        garmin_push_cli,
        "connect",
        lambda _path: sqlite3.connect(":memory:", autocommit=True),
    )


def _week_file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "week.json"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _fail(*_args: Any, **_kwargs: Any) -> PushResult:
    raise AssertionError("the push must not start")


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (_LANDED, 0),
        (
            PushResult(
                date(2027, 1, 4),
                (),
                PushFailure(
                    date(2027, 1, 4), PushStep.UPLOAD, "o Garmin não respondeu"
                ),
                (),
            ),
            1,
        ),
    ],
)
def test_cli_prints_chat_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    result: PushResult,
    code: int,
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(
        garmin_push_cli, "push_garmin_week", lambda *_args, **_kwargs: result
    )

    assert garmin_push_cli.main([_week_file(tmp_path, week_text([]))]) == code
    assert capsys.readouterr().out == f"{result.chat_message}\n"


@pytest.mark.parametrize(
    "text",
    [week_text(["2027-01-11"]), "{", ""],
)
def test_cli_rejects_bad_input_before_any_garmin_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    text: str,
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(garmin_push_cli, "push_garmin_week", _fail)
    monkeypatch.setattr(garmin_push_cli, "initialize_database", _fail)

    assert garmin_push_cli.main([_week_file(tmp_path, text)]) == 2
    assert capsys.readouterr().err != ""


def test_cli_missing_file_returns_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(garmin_push_cli, "push_garmin_week", _fail)

    assert garmin_push_cli.main([str(tmp_path / "missing.json")]) == 2


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (GarminMfaRequiredError(GarminLoginReason.MANUAL, "requested"), 3),
        (GarminConfigurationError("invalid configuration"), 2),
        (NoActiveMacroPlanError("no active macro plan"), 2),
        (GarminRateLimitedError("rate limited"), 1),
        (sqlite3.Error("database failure"), 1),
    ],
)
def test_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: BaseException,
    expected_code: int,
) -> None:
    _configure_cli(monkeypatch, tmp_path)

    def raise_error(*_args: Any, **_kwargs: Any) -> PushResult:
        raise error

    monkeypatch.setattr(garmin_push_cli, "push_garmin_week", raise_error)

    assert garmin_push_cli.main([_week_file(tmp_path, week_text([]))]) == expected_code


@pytest.mark.parametrize(
    ("arguments", "code"),
    [(["--help"], 0), ([], 2), (["show"], 2), (["a", "b"], 2)],
)
def test_cli_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    code: int,
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(garmin_push_cli, "push_garmin_week", _fail)

    assert garmin_push_cli.main(arguments) == code
    assert capsys.readouterr().err == _USAGE


def test_cli_show_prints_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    shown: list[date] = []

    def show(_config: object, week_start: date) -> tuple[str, ...]:
        shown.append(week_start)
        return ("2027-01-04 04/01 Leve",)

    monkeypatch.setattr(garmin_push_cli, "show_garmin_week", show)

    assert garmin_push_cli.main(["show", "2027-01-04"]) == 0
    assert capsys.readouterr().out == "2027-01-04 04/01 Leve\n"
    assert shown == [date(2027, 1, 4)]


def test_cli_show_network_error_prints_a_fixed_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    _write_fake_token(auth_config)
    calendar = FakeGarminCalendar(
        errors={("get_scheduled_workouts_by_period", 0): requests.ConnectionError()}
    )
    factory, _ = push_factory_for(fake_server, calendar)

    def show(_config: object, week_start: date) -> tuple[str, ...]:
        return show_garmin_week(auth_config, week_start, factory=factory)

    monkeypatch.setattr(garmin_push_cli, "show_garmin_week", show)

    assert garmin_push_cli.main(["show", "2027-01-04"]) == 1
    assert capsys.readouterr().err.endswith(
        "Garmin is unreachable during the authenticated request\n"
    )


def test_cli_show_rejects_a_bad_date(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(garmin_push_cli, "show_garmin_week", _fail)

    assert garmin_push_cli.main(["show", "2027-1-4"]) == 2
    assert "expected YYYY-MM-DD" in capsys.readouterr().err
