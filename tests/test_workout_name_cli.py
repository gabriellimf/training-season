from __future__ import annotations

from typing import TYPE_CHECKING

from training_season.workout_cli import main

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest


def run_name(
    arguments: Sequence[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = main(["name", *arguments])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_name_command_prints_one_name_per_pair(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run_name(
        ["2026-10-26", "10x400", "2026-10-30", "Long 14k"], capsys
    )

    assert code == 0
    assert out == "26/10 10x400\n30/10 Long 14k\n"
    assert err == ""


def test_name_command_rejects_a_collision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run_name(
        ["2026-10-26", "Leve 6k", "2026-10-26", "leve 6k"], capsys
    )

    assert code == 2
    assert out == ""
    assert "positions 1 and 2" in err
    assert err.count("\n") == 1


def test_name_command_rejects_a_date_without_dashes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run_name(["20261026", "10x400"], capsys)

    assert code == 2
    assert out == ""
    assert "'20261026'" in err


def test_name_command_rejects_a_bad_date(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run_name(["26/10/2026", "10x400"], capsys)

    assert code == 2
    assert out == ""
    assert "'26/10/2026'" in err
    assert "YYYY-MM-DD" in err


def test_name_command_rejects_an_odd_argument_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run_name(["2026-10-26", "10x400", "2026-10-28"], capsys)

    assert code == 2
    assert out == ""
    assert "3 arguments" in err
    assert "<YYYY-MM-DD> <content>" in err


def test_name_command_without_arguments_is_rejected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = run_name([], capsys)

    assert code == 2
    assert "0 arguments" in err
