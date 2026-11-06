"""
Load and render the replaceable coaching instruction.

The value passed as ``system=`` must be exactly ``CoachInstruction.render(...)``.
Code-owned output contracts and device rules belong in a user turn. Never log the
template text or rendered text.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

from training_season.cli_entry import run_cli
from training_season.timeformat import training_date

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from zoneinfo import ZoneInfo

COACH_INSTRUCTION_PATH_ENV = "COACH_INSTRUCTION_PATH"
DEFAULT_COACH_INSTRUCTION_PATH = (
    Path(__file__).parent / "instructions" / "coach.pt-BR.md"
)
MAX_COACH_INSTRUCTION_BYTES = 65_536
MISSING_VALUE = "-"
PLACEHOLDERS = frozenset({"today", "athlete_name", "race_date", "weeks_to_race"})

_LOGGER = logging.getLogger(__name__)


class CoachInstructionError(Exception):
    """Base class for coaching instruction errors."""


class CoachInstructionConfigurationError(CoachInstructionError):
    """Raised when the instruction path configuration is invalid."""


class CoachInstructionFileError(CoachInstructionError):
    """Raised when the instruction file cannot be loaded."""


class CoachInstructionTemplateError(CoachInstructionError):
    """Raised when the instruction has invalid or unknown placeholders."""


@dataclass(frozen=True, slots=True)
class CoachInstruction:
    """Validated instruction text and its source metadata."""

    path: Path
    text: str = field(repr=False)
    placeholders: frozenset[str]
    from_environment: bool
    size_bytes: int = field(repr=False)

    def render(
        self,
        *,
        now: datetime,
        zone: ZoneInfo,
        athlete_name: str | None,
        race_date: date | None,
    ) -> str:
        """Render the instruction with current profile and state values."""
        today_text = training_date(now, zone)
        today = date.fromisoformat(today_text)
        athlete_value = _athlete_name_value(athlete_name)
        race_value = MISSING_VALUE if race_date is None else race_date.isoformat()
        weeks_value = _weeks_to_race_value(today, race_date)
        return Template(self.text).substitute(
            today=today_text,
            athlete_name=athlete_value,
            race_date=race_value,
            weeks_to_race=weeks_value,
        )


def _athlete_name_value(athlete_name: str | None) -> str:
    if athlete_name is None:
        return MISSING_VALUE
    return athlete_name.strip() or MISSING_VALUE


def _weeks_to_race_value(today: date, race_date: date | None) -> str:
    if race_date is None:
        return MISSING_VALUE
    return str(weeks_to_race(today, race_date))


def coach_instruction_path_from_environment(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the configured absolute path or the bundled instruction path."""
    environment = os.environ if environ is None else environ
    raw_path = environment.get(COACH_INSTRUCTION_PATH_ENV, "")
    configured_path = raw_path.strip()
    if not configured_path:
        return DEFAULT_COACH_INSTRUCTION_PATH

    path = Path(configured_path)
    if not path.is_absolute():
        raise CoachInstructionConfigurationError(
            f"{COACH_INSTRUCTION_PATH_ENV} must be an absolute path to the coaching "
            f"instruction file. Got: {raw_path!r}."
        )
    return path


def _invalid_placeholder_location(text: str, template: Template) -> tuple[int, int]:
    for match in template.pattern.finditer(text):
        if match.group("invalid") is not None:
            position = match.start()
            line = text.count("\n", 0, position) + 1
            column = position - text.rfind("\n", 0, position)
            return line, column
    raise RuntimeError("Invalid template state without an invalid placeholder")


def _validated_placeholders(text: str, path: Path) -> frozenset[str]:
    template = Template(text)
    if not template.is_valid():
        line, column = _invalid_placeholder_location(text, template)
        raise CoachInstructionTemplateError(
            "Invalid placeholder in coaching instruction file at "
            f"line {line}, column {column}: {path}. Write $$ for a literal dollar sign."
        )

    identifiers = frozenset(template.get_identifiers())
    unknown = identifiers - PLACEHOLDERS
    if unknown:
        unknown_names = ", ".join(f"${name}" for name in sorted(unknown))
        known_names = ", ".join(f"${name}" for name in sorted(PLACEHOLDERS))
        raise CoachInstructionTemplateError(
            f"Unknown placeholders in coaching instruction file {path}: "
            f"{unknown_names}. Known placeholders: {known_names}."
        )
    return identifiers


def _unreadable_instruction_error(
    path: Path, error: OSError
) -> CoachInstructionFileError:
    detail = error.strerror or type(error).__name__
    return CoachInstructionFileError(
        f"Cannot read coaching instruction file {path}: {detail}"
    )


def _instruction_file_mode(path: Path) -> int:
    try:
        return path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        raise CoachInstructionFileError(
            f"Coaching instruction file not found: {path}"
        ) from None
    except OSError as error:
        raise _unreadable_instruction_error(path, error) from error


def _require_instruction_file(path: Path) -> None:
    mode = _instruction_file_mode(path)
    if stat.S_ISDIR(mode):
        raise CoachInstructionFileError(
            f"Coaching instruction path is a directory, not a file: {path}"
        )
    if not stat.S_ISREG(mode):
        raise CoachInstructionFileError(
            f"Coaching instruction path is not a regular file: {path}"
        )


def _read_instruction_bytes(path: Path) -> bytes:
    _require_instruction_file(path)
    try:
        with path.open("rb") as instruction_file:
            raw_text = instruction_file.read(MAX_COACH_INSTRUCTION_BYTES + 1)
    except OSError as error:
        raise _unreadable_instruction_error(path, error) from error

    if len(raw_text) > MAX_COACH_INSTRUCTION_BYTES:
        raise CoachInstructionFileError(
            f"Coaching instruction file is larger than "
            f"{MAX_COACH_INSTRUCTION_BYTES} bytes: {path}"
        )
    return raw_text


def _decoded_instruction_text(raw_text: bytes, path: Path) -> str:
    try:
        text = raw_text.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise CoachInstructionFileError(
            f"Coaching instruction file is not valid UTF-8 at byte "
            f"{error.start}: {path}"
        ) from None

    if not text.strip():
        raise CoachInstructionFileError(f"Coaching instruction file is empty: {path}")
    return text


def _log_loaded_instruction(instruction: CoachInstruction) -> None:
    source = COACH_INSTRUCTION_PATH_ENV if instruction.from_environment else "default"
    placeholder_summary = ",".join(sorted(instruction.placeholders)) or "none"
    _LOGGER.info(
        "Loaded coaching instruction source=%s path=%s bytes=%d placeholders=%s",
        source,
        instruction.path,
        instruction.size_bytes,
        placeholder_summary,
    )


def load_coach_instruction(
    environ: Mapping[str, str] | None = None,
) -> CoachInstruction:
    """Read, validate, and return the configured coaching instruction."""
    environment = os.environ if environ is None else environ
    from_environment = bool(environment.get(COACH_INSTRUCTION_PATH_ENV, "").strip())
    path = coach_instruction_path_from_environment(environment)
    raw_text = _read_instruction_bytes(path)
    text = _decoded_instruction_text(raw_text, path)
    placeholders = _validated_placeholders(text, path)
    instruction = CoachInstruction(
        path=path,
        text=text,
        placeholders=placeholders,
        from_environment=from_environment,
        size_bytes=len(raw_text),
    )
    _log_loaded_instruction(instruction)
    return instruction


def weeks_to_race(today: date, race_date: date) -> int:
    """Return full weeks until the race, with zero for race week or past dates."""
    return max(0, (race_date - today).days // 7)


def _print_instruction_summary(_arguments: tuple[str, ...]) -> int:
    try:
        instruction = load_coach_instruction()
    except CoachInstructionError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    source = COACH_INSTRUCTION_PATH_ENV if instruction.from_environment else "default"
    placeholders = ",".join(sorted(instruction.placeholders)) or "none"
    sys.stdout.write(
        f"instruction source={source} path={instruction.path} "
        f"bytes={instruction.size_bytes} placeholders={placeholders}\n"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Check the coaching instruction without making a network request."""
    return run_cli(
        argv,
        "training-season-instruction check",
        {("check",)},
        _print_instruction_summary,
    )


if __name__ == "__main__":
    raise SystemExit(main())
