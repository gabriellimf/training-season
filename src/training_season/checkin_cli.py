"""Command-line entry point for the weekly check-in with the coach."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.checkin_chat import CheckinCoach, CheckinSetup, run_checkin
from training_season.checkin_store import CheckinStoreError
from training_season.cli_entry import report_failure, run_cli
from training_season.coach_instruction import (
    CoachInstructionError,
    load_coach_instruction,
)
from training_season.coach_system import StoredProfileError
from training_season.database import DatabaseConfigurationError, MigrationError
from training_season.llm import LLMConfigurationError
from training_season.onboarding_cli import (
    connected_database,
    default_adapter_factory,
    utc_clock,
)
from training_season.timeformat import (
    TrainingTimezoneError,
    training_zone_from_environment,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from typing import TextIO

    from training_season.onboarding_cli import AdapterFactory

_USAGE = "training-season-checkin"
_INPUT_ERROR = 2
_INPUT_ERRORS = (
    DatabaseConfigurationError,
    LLMConfigurationError,
    CoachInstructionError,
    TrainingTimezoneError,
)
_RUNTIME_ERRORS = (
    CheckinStoreError,
    MigrationError,
    StoredProfileError,
    sqlite3.Error,
)


@dataclass(frozen=True, slots=True)
class _CommandInputs:
    environ: Mapping[str, str]
    adapter_factory: AdapterFactory
    clock: Callable[[], datetime]
    read_line: Callable[[str], str]
    out: TextIO


def _run_checkin(inputs: _CommandInputs) -> int:
    setup = CheckinSetup(
        instruction=load_coach_instruction(inputs.environ),
        zone=training_zone_from_environment(inputs.environ),
        clock=inputs.clock,
    )
    adapter = inputs.adapter_factory(inputs.environ)
    try:
        with connected_database(inputs.environ) as connection:
            coach = CheckinCoach(connection, adapter, setup, inputs.out)
            return run_checkin(coach, inputs.read_line)
    finally:
        adapter.close()


def _checkin_cli(inputs: _CommandInputs) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        return _run_checkin(inputs)
    except _INPUT_ERRORS as error:
        return report_failure(error, _INPUT_ERROR)
    except _RUNTIME_ERRORS as error:
        return report_failure(error, 1)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    adapter_factory: AdapterFactory = default_adapter_factory,
    clock: Callable[[], datetime] = utc_clock,
    read_line: Callable[[str], str] = input,
    out: TextIO | None = None,
) -> int:
    """
    Ask the three check-in questions, store them, then chat with the coach.

    Takes no arguments. Used as `main([])`
    """
    inputs = _CommandInputs(
        environ=os.environ if environ is None else environ,
        adapter_factory=adapter_factory,
        clock=clock,
        read_line=read_line,
        out=sys.stdout if out is None else out,
    )
    return run_cli(argv, _USAGE, {()}, lambda _arguments: _checkin_cli(inputs))


if __name__ == "__main__":
    raise SystemExit(main())
