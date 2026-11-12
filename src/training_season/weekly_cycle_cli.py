"""Command-line entry point for the weekly cycle."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.cli_entry import report_failure, run_cli
from training_season.coach_instruction import (
    CoachInstructionError,
    load_coach_instruction,
)
from training_season.database import (
    DatabaseConfigurationError,
    MigrationError,
    connect,
    database_path_from_environment,
    initialize_database,
)
from training_season.garmin_auth import (
    GarminConfigurationError,
    default_garmin_factory,
    garmin_auth_config_from_environment,
)
from training_season.garmin_pull import history_start_from_environment
from training_season.llm import LLMConfigurationError
from training_season.onboarding_cli import default_adapter_factory, utc_clock
from training_season.onboarding_store import insert_cycle_message
from training_season.push_store import NoActiveMacroPlanError, active_macro_plan_id
from training_season.timeformat import (
    TrainingTimezoneError,
    training_zone_from_environment,
)
from training_season.weekly_cycle import CycleSetup, run_weekly_cycle

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from typing import TextIO

    from training_season.garmin_auth import GarminFactory
    from training_season.onboarding_cli import AdapterFactory
    from training_season.weekly_cycle import CycleOutcome

_USAGE = "training-season-weekly [--help]"
_INPUT_ERROR = 2
_MFA_HINT = (
    "Conclua o login com `training-season-garmin` e depois rode "
    "`training-season-weekly` de novo."
)
_INPUT_ERRORS = (
    DatabaseConfigurationError,
    LLMConfigurationError,
    CoachInstructionError,
    TrainingTimezoneError,
    GarminConfigurationError,
    NoActiveMacroPlanError,
)


@dataclass(frozen=True, slots=True)
class _CommandInputs:
    environ: Mapping[str, str]
    adapter_factory: AdapterFactory
    garmin_factory: GarminFactory
    clock: Callable[[], datetime]
    out: TextIO


def _cycle_setup(inputs: _CommandInputs) -> CycleSetup:
    return CycleSetup(
        instruction=load_coach_instruction(inputs.environ),
        zone=training_zone_from_environment(inputs.environ),
        auth_config=garmin_auth_config_from_environment(inputs.environ),
        garmin_factory=inputs.garmin_factory,
        history_start=history_start_from_environment(inputs.environ),
        now=inputs.clock(),
    )


def _published(
    connection: sqlite3.Connection, outcome: CycleOutcome, out: TextIO
) -> int:
    out.write(f"{outcome.chat_message}\n")
    if outcome.garmin_login_needed:
        sys.stderr.write(f"{_MFA_HINT}\n")
    try:
        insert_cycle_message(connection, outcome.chat_message, outcome.weekly_plan_id)
    except sqlite3.Error as error:
        sys.stderr.write(f"weekly cycle chat message was not stored: {error}\n")
        return 1
    return outcome.exit_code


def _run_cycle(inputs: _CommandInputs) -> int:
    setup = _cycle_setup(inputs)
    adapter = inputs.adapter_factory(inputs.environ)
    try:
        initialize_database(inputs.environ)
        path = database_path_from_environment(inputs.environ)
        with closing(connect(path)) as connection:
            active_macro_plan_id(connection)
            outcome = run_weekly_cycle(connection, adapter, setup)
            return _published(connection, outcome, inputs.out)
    finally:
        adapter.close()


def _weekly_cli(_arguments: tuple[str, ...], inputs: _CommandInputs) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        return _run_cycle(inputs)
    except _INPUT_ERRORS as error:
        return report_failure(error, _INPUT_ERROR)
    except (MigrationError, sqlite3.Error) as error:
        return report_failure(error, 1)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    adapter_factory: AdapterFactory = default_adapter_factory,
    garmin_factory: GarminFactory = default_garmin_factory,
    clock: Callable[[], datetime] = utc_clock,
    out: TextIO | None = None,
) -> int:
    """
    Run the weekly cycle for the Monday-to-Sunday week after today.

    Used as `main([])`
    """
    inputs = _CommandInputs(
        environ=os.environ if environ is None else environ,
        adapter_factory=adapter_factory,
        garmin_factory=garmin_factory,
        clock=clock,
        out=sys.stdout if out is None else out,
    )
    return run_cli(argv, _USAGE, {()}, lambda arguments: _weekly_cli(arguments, inputs))


if __name__ == "__main__":
    raise SystemExit(main())
