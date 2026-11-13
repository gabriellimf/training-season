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
from training_season.cycle_run_store import CycleRunNotice
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
from training_season.push_store import NoActiveMacroPlanError, active_macro_plan_id
from training_season.timeformat import (
    TrainingTimezoneError,
    training_zone_from_environment,
)
from training_season.weekly_cycle import CycleSetup, run_weekly_cycle
from training_season.weekly_job import (
    WeeklyJob,
    close_runs_left_open,
    run_weekly_job,
    stop_signals_interrupt,
)
from training_season.weekly_job_text import pre_step_text

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime
    from typing import TextIO
    from zoneinfo import ZoneInfo

    from training_season.cycle_run_store import CycleRunOutcome, CycleTrigger
    from training_season.garmin_auth import GarminFactory
    from training_season.onboarding_cli import AdapterFactory
    from training_season.weekly_cycle import CycleOutcome

_USAGE = "training-season-weekly [--scheduled | --close-open-runs] [--help]"
_SCHEDULED = ("--scheduled",)
_CLOSE_OPEN_RUNS = ("--close-open-runs",)
_INPUT_ERROR = 2
_MFA_HINT = (
    "Conclua o login com `training-season-garmin` e depois rode "
    "`training-season-weekly` de novo."
)
_PRE_STEP_ERRORS = (
    LLMConfigurationError,
    CoachInstructionError,
    TrainingTimezoneError,
    GarminConfigurationError,
    NoActiveMacroPlanError,
    sqlite3.Error,
)
_CLOSE_ERRORS = (DatabaseConfigurationError, MigrationError, sqlite3.Error, OSError)


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


def _cycle_notice(outcome: CycleOutcome) -> CycleRunNotice:
    if outcome.garmin_login_needed:
        sys.stderr.write(f"{_MFA_HINT}\n")
    run_outcome: CycleRunOutcome = "done" if outcome.exit_code == 0 else "failed"
    return CycleRunNotice(
        run_outcome, outcome.exit_code, outcome.chat_message, outcome.weekly_plan_id
    )


def _pre_step_notice(error: Exception) -> CycleRunNotice:
    exit_code = 1 if isinstance(error, sqlite3.Error) else _INPUT_ERROR
    report_failure(error, exit_code)
    return CycleRunNotice("failed", exit_code, pre_step_text(error))


def _run_cycle(
    connection: sqlite3.Connection, inputs: _CommandInputs
) -> CycleRunNotice:
    try:
        setup = _cycle_setup(inputs)
        active_macro_plan_id(connection)
        adapter = inputs.adapter_factory(inputs.environ)
    except _PRE_STEP_ERRORS as error:
        return _pre_step_notice(error)
    with closing(adapter), stop_signals_interrupt():
        outcome = run_weekly_cycle(connection, adapter, setup)
    return _cycle_notice(outcome)


def _zone_or_none(environ: Mapping[str, str]) -> ZoneInfo | None:
    try:
        return training_zone_from_environment(environ)
    except TrainingTimezoneError:
        return None


def _run_job(inputs: _CommandInputs, trigger: CycleTrigger) -> int:
    path = database_path_from_environment(inputs.environ)
    initialize_database(inputs.environ)
    with closing(connect(path)) as connection:
        job = WeeklyJob(
            trigger=trigger,
            zone=_zone_or_none(inputs.environ),
            out=inputs.out,
            body=lambda: _run_cycle(connection, inputs),
        )
        return run_weekly_job(connection, path, job)


def _close_runs_command(inputs: _CommandInputs) -> int:
    try:
        path = database_path_from_environment(inputs.environ)
        initialize_database(inputs.environ)
        with closing(connect(path)) as connection:
            zone = _zone_or_none(inputs.environ)
            close_runs_left_open(connection, path, zone, inputs.out)
    except _CLOSE_ERRORS as error:
        return report_failure(error, 1)
    return 0


def _weekly_cli(arguments: tuple[str, ...], inputs: _CommandInputs) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if arguments == _CLOSE_OPEN_RUNS:
        return _close_runs_command(inputs)
    trigger: CycleTrigger = "scheduled" if arguments == _SCHEDULED else "manual"
    try:
        return _run_job(inputs, trigger)
    except DatabaseConfigurationError as error:
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

    `--scheduled` records the run as started by the scheduler. `--close-open-runs`
    runs no cycle: it only closes the runs a killed process left open.
    Used as `main([])`, `main(["--scheduled"])` or `main(["--close-open-runs"])`
    """
    inputs = _CommandInputs(
        environ=os.environ if environ is None else environ,
        adapter_factory=adapter_factory,
        garmin_factory=garmin_factory,
        clock=clock,
        out=sys.stdout if out is None else out,
    )
    return run_cli(
        argv,
        _USAGE,
        {(), _SCHEDULED, _CLOSE_OPEN_RUNS},
        lambda arguments: _weekly_cli(arguments, inputs),
    )


if __name__ == "__main__":
    raise SystemExit(main())
