"""
Load and render the replaceable coaching instruction.

The value passed as ``system=`` must be exactly ``CoachInstruction.render(...)``.
Code-owned output contracts and device rules belong in a user turn. Never log the
template text or rendered text.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

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
        athlete_value = (
            MISSING_VALUE if athlete_name is None else athlete_name.strip()
        ) or MISSING_VALUE
        race_value = MISSING_VALUE if race_date is None else race_date.isoformat()
        weeks_value = (
            MISSING_VALUE if race_date is None else str(weeks_to_race(today, race_date))
        )
        return Template(self.text).substitute(
            today=today_text,
            athlete_name=athlete_value,
            race_date=race_value,
            weeks_to_race=weeks_value,
        )


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


def load_coach_instruction(
    environ: Mapping[str, str] | None = None,
) -> CoachInstruction:
    """Read, validate, and return the configured coaching instruction."""
    environment = os.environ if environ is None else environ
    configured_path = environment.get(COACH_INSTRUCTION_PATH_ENV, "").strip()
    path = coach_instruction_path_from_environment(environment)

    if not path.exists():
        raise CoachInstructionFileError(f"Coaching instruction file not found: {path}")
    if path.is_dir():
        raise CoachInstructionFileError(
            f"Coaching instruction path is a directory, not a file: {path}"
        )
    if not path.is_file():
        raise CoachInstructionFileError(
            f"Coaching instruction path is not a regular file: {path}"
        )

    try:
        with path.open("rb") as instruction_file:
            raw_text = instruction_file.read(MAX_COACH_INSTRUCTION_BYTES + 1)
    except OSError as error:
        detail = error.strerror or type(error).__name__
        raise CoachInstructionFileError(
            f"Cannot read coaching instruction file {path}: {detail}"
        ) from error

    if len(raw_text) > MAX_COACH_INSTRUCTION_BYTES:
        raise CoachInstructionFileError(
            f"Coaching instruction file is larger than "
            f"{MAX_COACH_INSTRUCTION_BYTES} bytes: {path}"
        )

    try:
        text = raw_text.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise CoachInstructionFileError(
            f"Coaching instruction file is not valid UTF-8 at byte "
            f"{error.start}: {path}"
        ) from None

    if not text.strip():
        raise CoachInstructionFileError(f"Coaching instruction file is empty: {path}")

    placeholders = _validated_placeholders(text, path)
    from_environment = bool(configured_path)
    source = COACH_INSTRUCTION_PATH_ENV if from_environment else "default"
    placeholder_summary = ",".join(sorted(placeholders)) or "none"
    _LOGGER.info(
        "Loaded coaching instruction source=%s path=%s bytes=%d placeholders=%s",
        source,
        path,
        len(raw_text),
        placeholder_summary,
    )
    return CoachInstruction(
        path=path,
        text=text,
        placeholders=placeholders,
        from_environment=from_environment,
        size_bytes=len(raw_text),
    )


def weeks_to_race(today: date, race_date: date) -> int:
    """Return full weeks until the race, with zero for race week or past dates."""
    return max(0, (race_date - today).days // 7)


def _print_usage() -> None:
    sys.stderr.write("Usage: training-season-instruction check\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Check the coaching instruction without making a network request."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--help"], ["-h"]):
        _print_usage()
        return 0
    if arguments != ["check"]:
        _print_usage()
        return 2

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


if __name__ == "__main__":
    raise SystemExit(main())
