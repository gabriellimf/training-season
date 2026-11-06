"""Tests for coaching instruction placeholders, rendering and the bundled example."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from coach_instruction_helpers import (
    _NOW,
    _TODAY,
    _clear_coach_instruction_path,
    _load_path,
    _load_text,
    _render,
    _write_instruction,
)
from training_season import coach_instruction
from training_season.coach_instruction import (
    DEFAULT_COACH_INSTRUCTION_PATH,
    MISSING_VALUE,
    PLACEHOLDERS,
    CoachInstructionTemplateError,
    load_coach_instruction,
)
from training_season.coach_instruction import (
    weeks_to_race as calculate_weeks_to_race,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def clear_coach_instruction_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_coach_instruction_path(monkeypatch)


@pytest.mark.parametrize(
    ("text", "line", "column"),
    [
        ("R$ 50", 1, 2),
        ("first\nsecond\n$5", 3, 1),
        ("trailing $", 1, 10),
    ],
)
def test_invalid_placeholder_fails_with_position(
    tmp_path: Path, text: str, line: int, column: int
) -> None:
    path = _write_instruction(tmp_path / "invalid.md", text)

    with pytest.raises(CoachInstructionTemplateError) as error:
        _load_path(path)

    assert f"line {line}, column {column}" in str(error.value)
    assert "$$" in str(error.value)
    assert str(path) in str(error.value)


def test_unknown_placeholders_are_all_listed(tmp_path: Path) -> None:
    with pytest.raises(CoachInstructionTemplateError) as error:
        _load_text(tmp_path, "$racedate $Today")

    message = str(error.value)
    assert "$Today, $racedate" in message
    assert "$athlete_name, $race_date, $today, $weeks_to_race" in message


def test_dollar_escape_renders_literal(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "R$$ 50 $$today")

    assert _render(instruction) == "R$ 50 $today"


def test_json_braces_pass_through(tmp_path: Path) -> None:
    text = '{"a": 1}'
    instruction = _load_text(tmp_path, text)

    assert _render(instruction) == text


def test_braced_placeholder(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "${today}x")

    assert _render(instruction) == "2026-03-01x"


def test_substituted_value_is_not_expanded(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "$athlete_name")

    assert _render(instruction, athlete_name="$today") == "$today"


def test_error_messages_exclude_file_text(tmp_path: Path) -> None:
    sentinel = "private-instruction-sentinel"

    with pytest.raises(CoachInstructionTemplateError) as error:
        _load_text(tmp_path, f"{sentinel} $")

    assert sentinel not in str(error.value)


def test_today_uses_training_timezone(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "$today")

    assert _render(instruction) == "2026-03-01"


def test_naive_now_raises_value_error(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "$today")

    with pytest.raises(ValueError, match="timezone-aware"):
        _render(instruction, now=_NOW.replace(tzinfo=None))


def test_athlete_name_present_and_stripped(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "$athlete_name")

    assert _render(instruction, athlete_name="  Test Athlete  ") == "Test Athlete"


@pytest.mark.parametrize("athlete_name", [None, "  "])
def test_athlete_name_missing_renders_marker(
    tmp_path: Path, athlete_name: str | None
) -> None:
    instruction = _load_text(tmp_path, "$athlete_name")

    assert _render(instruction, athlete_name=athlete_name) == MISSING_VALUE


def test_race_date_present(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "$race_date")
    race_date = _TODAY + timedelta(days=7)

    assert _render(instruction, race_date=race_date) == race_date.isoformat()


def test_race_date_missing_renders_marker(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "$race_date")

    assert _render(instruction) == MISSING_VALUE


@pytest.mark.parametrize(
    ("days", "expected"),
    [(0, 0), (6, 0), (7, 1), (20, 2), (-3, 0)],
)
def test_weeks_to_race(days: int, expected: int) -> None:
    race_date = _TODAY + timedelta(days=days)

    assert calculate_weeks_to_race(_TODAY, race_date) == expected


def test_weeks_to_race_render_uses_training_date(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "$weeks_to_race")
    race_date = _TODAY + timedelta(days=7)

    assert _render(instruction, race_date=race_date) == "1"


def test_weeks_to_race_missing_renders_marker(tmp_path: Path) -> None:
    instruction = _load_text(tmp_path, "$weeks_to_race")

    assert _render(instruction) == MISSING_VALUE


def test_fresh_install_renders_bundled_example() -> None:
    instruction = load_coach_instruction({})

    rendered = _render(instruction)

    assert MISSING_VALUE in rendered
    assert "$" not in rendered


def test_example_has_no_digits_or_at_sign() -> None:
    text = DEFAULT_COACH_INSTRUCTION_PATH.read_text(encoding="utf-8")

    assert not any(character in "0123456789" for character in text)
    assert "@" not in text


def test_example_uses_exactly_the_known_placeholders() -> None:
    instruction = load_coach_instruction({})

    assert instruction.placeholders == PLACEHOLDERS


def test_render_logs_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    instruction = _load_text(tmp_path, "$athlete_name")
    caplog.clear()
    caplog.set_level(logging.DEBUG)

    _render(instruction, athlete_name="private-profile-sentinel")

    records = [
        record for record in caplog.records if record.name == coach_instruction.__name__
    ]
    assert records == []
    assert "private-profile-sentinel" not in caplog.text
