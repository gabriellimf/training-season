"""Tests for the replaceable coaching instruction."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx2
import pytest

from training_season import coach_instruction
from training_season.coach_instruction import (
    COACH_INSTRUCTION_PATH_ENV,
    DEFAULT_COACH_INSTRUCTION_PATH,
    MAX_COACH_INSTRUCTION_BYTES,
    MISSING_VALUE,
    PLACEHOLDERS,
    CoachInstruction,
    CoachInstructionConfigurationError,
    CoachInstructionFileError,
    CoachInstructionTemplateError,
    coach_instruction_path_from_environment,
    load_coach_instruction,
)
from training_season.coach_instruction import (
    weeks_to_race as calculate_weeks_to_race,
)
from training_season.llm import (
    ChatTurn,
    LLMAdapter,
    LLMJob,
    llm_config_from_environment,
    new_llm_adapter,
)

RequestHandler = Callable[[httpx2.Request], httpx2.Response]
_FAKE_KEY = "sk-or-test-not-a-real-key"
_NOW = datetime(2026, 3, 2, 2, 30, tzinfo=UTC)
_ZONE = ZoneInfo("America/Sao_Paulo")
_TODAY = _NOW.astimezone(_ZONE).date()


@pytest.fixture(autouse=True)
def clear_coach_instruction_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(COACH_INSTRUCTION_PATH_ENV, raising=False)


def _write_instruction(path: Path, content: str | bytes) -> Path:
    data = content.encode("utf-8") if isinstance(content, str) else content
    path.write_bytes(data)
    return path


def _load_path(path: Path) -> CoachInstruction:
    return load_coach_instruction({COACH_INSTRUCTION_PATH_ENV: str(path)})


def _load_text(tmp_path: Path, text: str) -> CoachInstruction:
    return _load_path(_write_instruction(tmp_path / "coach.md", text))


def _render(
    instruction: CoachInstruction,
    *,
    now: datetime = _NOW,
    zone: ZoneInfo = _ZONE,
    athlete_name: str | None = None,
    race_date: date | None = None,
) -> str:
    return instruction.render(
        now=now,
        zone=zone,
        athlete_name=athlete_name,
        race_date=race_date,
    )


def _completion_payload(model: str) -> dict[str, object]:
    return {
        "id": "gen-test",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "safe response"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0},
    }


def _handler(requests: list[httpx2.Request]) -> RequestHandler:
    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        request_body = json.loads(request.content)
        return httpx2.Response(
            200, json=_completion_payload(str(request_body["model"]))
        )

    return handle


@contextmanager
def _adapter(requests: list[httpx2.Request]) -> Iterator[LLMAdapter]:
    config = llm_config_from_environment({"OPENAI_COMPATIBLE_API_KEY": _FAKE_KEY})
    adapter = new_llm_adapter(
        config, transport=httpx2.MockTransport(_handler(requests))
    )
    try:
        yield adapter
    finally:
        adapter.close()


def test_default_path_is_bundled_package_file() -> None:
    path = coach_instruction_path_from_environment({})

    assert path == DEFAULT_COACH_INSTRUCTION_PATH
    assert path.parent.parent == Path(coach_instruction.__file__).parent
    assert path.is_file()


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_env_uses_default(value: str) -> None:
    assert (
        coach_instruction_path_from_environment({COACH_INSTRUCTION_PATH_ENV: value})
        == DEFAULT_COACH_INSTRUCTION_PATH
    )


def test_absolute_env_path_is_used_and_stripped(tmp_path: Path) -> None:
    path = tmp_path / "coach.md"

    assert (
        coach_instruction_path_from_environment(
            {COACH_INSTRUCTION_PATH_ENV: f" {path} "}
        )
        == path
    )


@pytest.mark.parametrize("value", ["coach.md", "./coach.md", "~/coach.md"])
def test_relative_env_path_is_rejected(value: str) -> None:
    with pytest.raises(CoachInstructionConfigurationError) as error:
        coach_instruction_path_from_environment({COACH_INSTRUCTION_PATH_ENV: value})

    assert COACH_INSTRUCTION_PATH_ENV in str(error.value)
    assert value in str(error.value)


def test_default_load_does_not_depend_on_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    instruction = load_coach_instruction({})

    assert instruction.path == DEFAULT_COACH_INSTRUCTION_PATH


def test_missing_file_fails(tmp_path: Path) -> None:
    path = tmp_path / "missing.md"

    with pytest.raises(CoachInstructionFileError) as error:
        _load_path(path)

    assert str(error.value) == f"Coaching instruction file not found: {path}"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def test_long_path_fails(tmp_path: Path) -> None:
    path = tmp_path / ("a" * 300)

    with pytest.raises(CoachInstructionFileError) as error:
        _load_path(path)

    assert str(error.value).startswith(
        f"Cannot read coaching instruction file {path}: "
    )
    assert isinstance(error.value.__cause__, OSError)


def test_check_long_path_exits_2_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / ("a" * 300)
    monkeypatch.setenv(COACH_INSTRUCTION_PATH_ENV, str(path))

    assert coach_instruction.main(["check"]) == 2

    stderr = capsys.readouterr().err
    assert str(path) in stderr
    assert "Traceback" not in stderr


def test_directory_fails(tmp_path: Path) -> None:
    path = tmp_path / "instruction"
    path.mkdir()

    with pytest.raises(CoachInstructionFileError) as error:
        _load_path(path)

    assert str(error.value) == (
        f"Coaching instruction path is a directory, not a file: {path}"
    )


def test_fifo_fails_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "instruction.fifo"
    os.mkfifo(path)

    with pytest.raises(CoachInstructionFileError) as error:
        _load_path(path)

    assert str(error.value) == (
        f"Coaching instruction path is not a regular file: {path}"
    )


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root can read files with no permission bits",
)
def test_unreadable_file_fails(tmp_path: Path) -> None:
    path = _write_instruction(tmp_path / "unreadable.md", "instruction")
    path.chmod(0o000)

    with pytest.raises(CoachInstructionFileError) as error:
        _load_path(path)

    assert str(error.value) == (
        f"Cannot read coaching instruction file {path}: Permission denied"
    )
    assert type(error.value.__cause__) is PermissionError
    assert error.value.__context__ is error.value.__cause__


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root can read files with no permission bits",
)
def test_inaccessible_parent_directory_fails(tmp_path: Path) -> None:
    parent = tmp_path / "restricted"
    parent.mkdir()
    path = _write_instruction(parent / "coach.md", "instruction")
    parent.chmod(0o000)

    try:
        with pytest.raises(CoachInstructionFileError) as error:
            _load_path(path)
        assert str(error.value) == (
            f"Cannot read coaching instruction file {path}: Permission denied"
        )
        assert type(error.value.__cause__) is PermissionError
        assert error.value.__context__ is error.value.__cause__
    finally:
        parent.chmod(0o700)


def test_file_over_size_cap_fails(tmp_path: Path) -> None:
    path = _write_instruction(
        tmp_path / "large.md", b"a" * (MAX_COACH_INSTRUCTION_BYTES + 1)
    )

    with pytest.raises(CoachInstructionFileError) as error:
        _load_path(path)

    assert str(error.value) == (
        f"Coaching instruction file is larger than {MAX_COACH_INSTRUCTION_BYTES} "
        f"bytes: {path}"
    )


def test_file_at_size_cap_loads(tmp_path: Path) -> None:
    path = _write_instruction(tmp_path / "limit.md", b"a" * MAX_COACH_INSTRUCTION_BYTES)

    instruction = _load_path(path)

    assert instruction.size_bytes == MAX_COACH_INSTRUCTION_BYTES


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b" \n\t",
        "\u00a0\u2003\n".encode("utf-8"),
        b"\xef\xbb\xbf",
    ],
)
def test_empty_file_fails(tmp_path: Path, content: bytes) -> None:
    path = _write_instruction(tmp_path / "empty.md", content)

    with pytest.raises(CoachInstructionFileError) as error:
        _load_path(path)

    assert str(error.value) == f"Coaching instruction file is empty: {path}"


def test_invalid_utf8_fails_with_offset(tmp_path: Path) -> None:
    path = _write_instruction(tmp_path / "invalid.md", b"ab\xffc")

    with pytest.raises(CoachInstructionFileError) as error:
        _load_path(path)

    assert str(error.value) == (
        f"Coaching instruction file is not valid UTF-8 at byte 2: {path}"
    )
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True


def test_bom_is_removed(tmp_path: Path) -> None:
    path = _write_instruction(tmp_path / "bom.md", b"\xef\xbb\xbfA valid instruction.")

    assert _load_path(path).text == "A valid instruction."


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


def test_load_logs_path_not_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "private-instruction-sentinel"
    path = _write_instruction(tmp_path / "coach.md", f"Instruction {sentinel}")

    with caplog.at_level(logging.INFO, logger=coach_instruction.__name__):
        load_coach_instruction({COACH_INSTRUCTION_PATH_ENV: str(path)})

    records = [
        record for record in caplog.records if record.name == coach_instruction.__name__
    ]
    assert len(records) == 1
    assert str(path) in records[0].getMessage()
    assert sentinel not in caplog.text
    assert [(r.name, r.levelno, r.getMessage()) for r in records] == [
        (
            coach_instruction.__name__,
            logging.INFO,
            (
                "Loaded coaching instruction source=COACH_INSTRUCTION_PATH "
                f"path={path} bytes=40 placeholders=none"
            ),
        )
    ]


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


def test_check_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert coach_instruction.main([]) == 2
    assert "Usage: training-season-instruction check" in capsys.readouterr().err
    assert coach_instruction.main(["--help"]) == 0
    assert "Usage: training-season-instruction check" in capsys.readouterr().err


def test_replacing_file_changes_system_text(tmp_path: Path) -> None:
    path = tmp_path / "coach.md"
    _write_instruction(path, "Instruction A: ${today}")
    instruction_a = _load_path(path)
    system_a = _render(instruction_a)

    _write_instruction(path, "Instruction B: ${today}")
    instruction_b = _load_path(path)
    system_b = _render(instruction_b)

    requests: list[httpx2.Request] = []
    with _adapter(requests) as adapter:
        adapter.complete(
            LLMJob.CHAT,
            system=system_a,
            turns=[ChatTurn("user", "safe user turn")],
        )
        adapter.complete(
            LLMJob.CHAT,
            system=system_b,
            turns=[ChatTurn("user", "safe user turn")],
        )

    systems = [json.loads(request.content)["messages"][0] for request in requests]
    assert systems[0] == {"role": "system", "content": system_a}
    assert systems[1] == {"role": "system", "content": system_b}
    assert system_a != system_b


def test_german_instruction_reaches_system_unchanged(tmp_path: Path) -> None:
    german_text = "Antworte auf Deutsch. ä ö ü ß."
    path = _write_instruction(tmp_path / "coach.md", german_text)
    instruction = _load_path(path)
    system_text = _render(instruction)
    requests: list[httpx2.Request] = []

    with _adapter(requests) as adapter:
        adapter.complete(
            LLMJob.CHAT,
            system=system_text,
            turns=[ChatTurn("user", "safe user turn")],
        )

    system = json.loads(requests[0].content)["messages"][0]
    assert system["role"] == "system"
    assert system["content"].encode("utf-8") == german_text.encode("utf-8")

    _write_instruction(path, "Antworte auf Deutsch. Heute: ${today}")
    instruction_with_date = _load_path(path)
    expected = _render(instruction_with_date)

    with _adapter(requests) as adapter:
        adapter.complete(
            LLMJob.CHAT,
            system=expected,
            turns=[ChatTurn("user", "safe user turn")],
        )

    second_system = json.loads(requests[1].content)["messages"][0]
    assert second_system == {"role": "system", "content": expected}
    assert expected == "Antworte auf Deutsch. Heute: 2026-03-01"
