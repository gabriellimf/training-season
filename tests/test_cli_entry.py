from __future__ import annotations

import pytest

from training_season.cli_entry import run_cli

USAGE_LINE = "Usage: prog x\n"


class RecordingRun:
    def __init__(self) -> None:
        self.received: list[tuple[str, ...]] = []

    def __call__(self, arguments: tuple[str, ...]) -> int:
        self.received.append(arguments)
        return 7


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (["--help"], 0),
        (["-h"], 0),
        ([], 2),
        (["y"], 2),
        (["x", "x"], 2),
    ],
)
def test_usage_line_for_help_and_bad_arguments(
    arguments: list[str], code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    run = RecordingRun()

    assert run_cli(arguments, "prog x", {("x",)}, run) == code

    captured = capsys.readouterr()
    assert captured.err == USAGE_LINE
    assert captured.out == ""
    assert run.received == []


def test_accepted_arguments_run_the_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = RecordingRun()

    assert run_cli(["x"], "prog x", {("x",)}, run) == 7

    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert run.received == [("x",)]


def test_no_argv_reads_sys_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["prog", "--help"])
    run = RecordingRun()

    assert run_cli(None, "prog x", {("x",)}, run) == 0

    assert capsys.readouterr().err == USAGE_LINE
    assert run.received == []
