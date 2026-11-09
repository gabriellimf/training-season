"""Command-line entry point for the workout schema and the workout request."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from training_season.cli_entry import run_cli
from training_season.coach_instruction import (
    CoachInstructionError,
    load_coach_instruction,
)
from training_season.llm import (
    ChatTurn,
    LLMConfigurationError,
    LLMError,
    LLMJob,
    llm_config_from_environment,
    new_llm_adapter,
)
from training_season.timeformat import (
    TrainingTimezoneError,
    training_zone_from_environment,
)
from training_season.workout_name_cli import run_name_command
from training_season.workout_request import (
    WorkoutAccepted,
    WorkoutRejected,
    request_workout,
    workout_system_text,
)
from training_season.workout_validation import (
    WorkoutValidationError,
    workout_from_text,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    import httpx2

_USAGE = (
    "training-season-workout validate <path> | request <prompt-path> | "
    "name <YYYY-MM-DD> <content> [<YYYY-MM-DD> <content> ...]"
)
_COMMANDS = frozenset({"validate", "request"})
_MAX_FILE_BYTES = 1_048_576


def _is_known_command(arguments: tuple[str, ...]) -> bool:
    if arguments[:1] == ("name",):
        return True
    return len(arguments) == 2 and arguments[0] in _COMMANDS


def _read_text(path: str) -> str | None:
    try:
        size = Path(path).stat().st_size
        if size > _MAX_FILE_BYTES:
            sys.stderr.write(
                f"file {path} is {size} bytes, above the limit of "
                f"{_MAX_FILE_BYTES} bytes\n"
            )
            return None
        with Path(path).open("rb") as handle:
            return handle.read(_MAX_FILE_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        sys.stderr.write(f"cannot read file {path}: {error}\n")
        return None


def _validate_file(path: str) -> int:
    text = _read_text(path)
    if text is None:
        return 2
    try:
        workout = workout_from_text(text)
    except WorkoutValidationError as error:
        sys.stderr.write("".join(f"{line}\n" for line in error.errors))
        return 2
    sys.stdout.write(f"workout name={workout.name!r} steps={workout.step_count}\n")
    return 0


def _request_system_text(environ: Mapping[str, str]) -> str:
    instruction = load_coach_instruction(environ)
    rendered = instruction.render(
        now=datetime.now(UTC),
        zone=training_zone_from_environment(environ),
        athlete_name=None,
        race_date=None,
    )
    return workout_system_text(rendered)


def _print_outcome(outcome: WorkoutAccepted | WorkoutRejected) -> int:
    if isinstance(outcome, WorkoutRejected):
        sys.stdout.write(f"{outcome.chat_message}\n")
        return 3
    workout = outcome.workout
    sys.stdout.write(
        f"workout name={workout.name!r} steps={workout.step_count} "
        f"attempts={outcome.attempts}\n"
    )
    return 0


def _request_workout_from_prompt(
    prompt: str,
    environ: Mapping[str, str],
    transport: httpx2.BaseTransport | None,
) -> int:
    system = _request_system_text(environ)
    adapter = new_llm_adapter(llm_config_from_environment(environ), transport=transport)
    try:
        outcome = request_workout(
            adapter, LLMJob.PLAN, system=system, turns=[ChatTurn("user", prompt)]
        )
    finally:
        adapter.close()
    return _print_outcome(outcome)


def _request_file(
    path: str,
    environ: Mapping[str, str],
    transport: httpx2.BaseTransport | None,
) -> int:
    prompt = _read_text(path)
    if prompt is None:
        return 2
    if not prompt.strip():
        sys.stderr.write(f"prompt file {path} is empty or only whitespace\n")
        return 2
    try:
        return _request_workout_from_prompt(prompt, environ, transport)
    except (
        LLMConfigurationError,
        CoachInstructionError,
        TrainingTimezoneError,
    ) as error:
        return _report_failure(error, 2)
    except LLMError as error:
        return _report_failure(error, 1)


def _report_failure(error: Exception, exit_code: int) -> int:
    sys.stderr.write(f"{error}\n")
    return exit_code


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx2.BaseTransport | None = None,
) -> int:
    """
    Validate a workout file, request a workout, or build workout names.

    Used as `main(["validate", "data/workouts/valid.json"])`
    """
    environment = os.environ if environ is None else environ

    def run(arguments: tuple[str, ...]) -> int:
        if arguments[0] == "validate":
            return _validate_file(arguments[1])
        if arguments[0] == "name":
            return run_name_command(arguments[1:])
        return _request_file(arguments[1], environment, transport)

    return run_cli(argv, _USAGE, _is_known_command, run)


if __name__ == "__main__":
    raise SystemExit(main())
