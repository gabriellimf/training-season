"""Tests for the coaching instruction check command."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from coach_instruction_helpers import _clear_coach_instruction_path, _write_instruction
from training_season import coach_instruction
from training_season.coach_instruction import COACH_INSTRUCTION_PATH_ENV

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def clear_coach_instruction_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_coach_instruction_path(monkeypatch)


def test_check_long_path_exits_2_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / ("a" * 300)
    monkeypatch.setenv(COACH_INSTRUCTION_PATH_ENV, str(path))

    assert coach_instruction.main(["check"]) == 2

    stderr = capsys.readouterr().err
    assert str(path) in stderr
    assert "Traceback" not in stderr


def test_check_success_prints_summary_without_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "private-instruction-sentinel"
    path = _write_instruction(tmp_path / "coach.md", f"{sentinel} $today")
    monkeypatch.setenv(COACH_INSTRUCTION_PATH_ENV, str(path))

    assert coach_instruction.main(["check"]) == 0

    captured = capsys.readouterr()
    assert str(path) in captured.out
    assert "placeholders=today" in captured.out
    assert sentinel not in captured.out
    assert sentinel not in captured.err


def test_check_failure_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "missing.md"
    monkeypatch.setenv(COACH_INSTRUCTION_PATH_ENV, str(path))

    assert coach_instruction.main(["check"]) == 2

    assert str(path) in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "code"),
    [(["--help"], 0), (["-h"], 0), ([], 2), (["run"], 2), (["check", "x"], 2)],
)
def test_check_usage(
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    code: int,
) -> None:
    assert coach_instruction.main(arguments) == code

    captured = capsys.readouterr()
    assert captured.err == "Usage: training-season-instruction check\n"
    assert captured.out == ""
