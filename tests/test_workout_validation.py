from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from training_season.workout_cli import main
from training_season.workout_validation import (
    WorkoutValidationError,
    bounded_line,
    workout_from_text,
)
from workout_fixtures import (
    JsonObject,
    easy_step,
    example_workout_text,
    workout_with_steps,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_bounded_line_drops_line_and_paragraph_separators() -> None:
    line = f"06/01{chr(0x2028)}fake{chr(0x2029)}line{chr(0x1B)}"

    assert bounded_line(line) == "06/01fakeline"


def test_plain_json_text_is_accepted() -> None:
    assert workout_from_text(example_workout_text()).name == "26/10 10x400"


def test_one_fenced_json_block_is_accepted() -> None:
    text = f"```json\n{example_workout_text()}\n```\n"

    assert workout_from_text(text).name == "26/10 10x400"


def test_text_around_the_fence_is_rejected() -> None:
    text = f"Aqui está:\n```json\n{example_workout_text()}\n```"

    with pytest.raises(WorkoutValidationError):
        workout_from_text(text)


def test_non_json_text_raises_the_validation_error() -> None:
    with pytest.raises(WorkoutValidationError) as caught:
        workout_from_text("hello")

    assert len(caught.value.errors) == 1
    assert caught.value.errors[0].startswith("$: ")


def test_errors_carry_the_json_path() -> None:
    step = easy_step()
    step["pace_mps"] = {"min": 3.0, "max": 2.0}
    text = json.dumps(workout_with_steps([step]))

    with pytest.raises(WorkoutValidationError) as caught:
        workout_from_text(text)

    assert caught.value.errors[0].startswith("steps.0.warmup.pace_mps: ")


def test_message_joins_the_errors() -> None:
    text = json.dumps({"name": "", "steps": []})

    with pytest.raises(WorkoutValidationError) as caught:
        workout_from_text(text)

    assert str(caught.value) == "; ".join(caught.value.errors)
    assert len(caught.value.errors) == 2


def test_step_count_includes_the_group_and_its_children() -> None:
    assert workout_from_text(example_workout_text()).step_count == 5


def rejected_lines(payload: object) -> tuple[str, ...]:
    with pytest.raises(WorkoutValidationError) as caught:
        workout_from_text(json.dumps(payload))
    return caught.value.errors


def test_a_huge_unknown_key_gives_a_line_of_at_most_200_characters() -> None:
    payload = {**workout_with_steps([easy_step()]), "k" * 100_000: 1}

    lines = rejected_lines(payload)

    assert len(lines) == 1
    assert len(lines[0]) <= 200
    assert lines[0].endswith("…")


def test_many_bad_steps_give_twenty_lines_and_a_count_line() -> None:
    steps: list[JsonObject] = [{"type": "swim"} for _ in range(20_000)]

    lines = rejected_lines(workout_with_steps(steps))

    assert len(lines) == 21
    assert lines[-1] == "and 19980 more errors"


def test_control_characters_are_stripped_from_the_lines() -> None:
    payload = {**workout_with_steps([easy_step()]), "a\nb\x1b[2J\u200bc": 1}

    (line,) = rejected_lines(payload)

    assert "\n" not in line
    assert "\x1b" not in line
    assert "\u200b" not in line


def test_infinity_in_the_pace_maximum_is_rejected() -> None:
    text = json.dumps(workout_with_steps([easy_step()])).replace("2.9", "Infinity")

    with pytest.raises(WorkoutValidationError):
        workout_from_text(text)


def test_validate_command_prints_name_and_step_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "workout.json"
    path.write_text(example_workout_text(), encoding="utf-8")

    assert main(["validate", str(path)]) == 0

    captured = capsys.readouterr()
    assert captured.out == "workout name='26/10 10x400' steps=5\n"
    assert captured.err == ""


def test_validate_command_prints_the_errors_and_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "workout.json"
    path.write_text("{}", encoding="utf-8")

    assert main(["validate", str(path)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "name: " in captured.err
    assert "steps: " in captured.err


def test_validate_command_reports_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.json"

    assert main(["validate", str(missing)]) == 2

    assert str(missing) in capsys.readouterr().err


def test_validate_command_rejects_a_file_above_one_megabyte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b" " * 1_048_577)

    assert main(["validate", str(path)]) == 2

    assert "1048577 bytes, above the limit of 1048576 bytes" in capsys.readouterr().err


def test_validate_command_requires_a_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["validate"]) == 2

    assert capsys.readouterr().err.startswith("Usage: training-season-workout")
