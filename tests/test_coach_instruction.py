"""Tests for the coaching instruction path setting and file loading."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from coach_instruction_helpers import (
    _clear_coach_instruction_path,
    _load_path,
    _write_instruction,
)
from training_season import coach_instruction
from training_season.coach_instruction import (
    COACH_INSTRUCTION_PATH_ENV,
    DEFAULT_COACH_INSTRUCTION_PATH,
    MAX_COACH_INSTRUCTION_BYTES,
    CoachInstructionConfigurationError,
    CoachInstructionFileError,
    coach_instruction_path_from_environment,
    load_coach_instruction,
)


@pytest.fixture(autouse=True)
def clear_coach_instruction_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_coach_instruction_path(monkeypatch)


def test_default_path_is_bundled_package_file() -> None:
    path = coach_instruction_path_from_environment({})

    assert path == DEFAULT_COACH_INSTRUCTION_PATH
    assert path.parent.parent == Path(coach_instruction.__file__).parent
    assert path.is_file()


@pytest.mark.parametrize("configured_path", ["", "   "])
def test_blank_env_uses_default(configured_path: str) -> None:
    assert (
        coach_instruction_path_from_environment(
            {COACH_INSTRUCTION_PATH_ENV: configured_path}
        )
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


@pytest.mark.parametrize("configured_path", ["coach.md", "./coach.md", "~/coach.md"])
def test_relative_env_path_is_rejected(configured_path: str) -> None:
    with pytest.raises(CoachInstructionConfigurationError) as error:
        coach_instruction_path_from_environment(
            {COACH_INSTRUCTION_PATH_ENV: configured_path}
        )

    assert COACH_INSTRUCTION_PATH_ENV in str(error.value)
    assert configured_path in str(error.value)


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
