"""The weekly cycle: pull, context, week, push, summary; it stops at a failed step."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING

from training_season.athlete_summary import (
    AthleteSummaryRejectedError,
    athlete_summary_system,
    update_athlete_summary,
)
from training_season.coach_context import assemble_coach_context
from training_season.coach_system import render_coach_system
from training_season.garmin_auth import (
    GarminConfigurationError,
    GarminMfaRequiredError,
)
from training_season.garmin_pull import pull_garmin
from training_season.garmin_push import push_garmin_week
from training_season.push_store import active_weekly_plan_id
from training_season.timeformat import training_date
from training_season.validated_reply import Rejected
from training_season.week_generation import generate_week
from training_season.weekly_cycle_text import (
    STEP_ERRORS,
    CycleStep,
    error_cause,
    failure_text,
    planned_week_lines,
    rejected_week_cause,
    week_comparison_lines,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from training_season.coach_context import CoachContext
    from training_season.coach_instruction import CoachInstruction
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory
    from training_season.llm import LLMAdapter
    from training_season.push_result import PushResult
    from training_season.push_week import PushWeek

_INPUT_EXIT = 2
_REJECTED_EXIT = 3
_LOGGER = logging.getLogger(__name__)


def next_week_range(today: date) -> tuple[date, date]:
    """
    Return the Monday after today and the Sunday after that Monday.

    Used as `monday, sunday = next_week_range(date(2026, 11, 11))`
    """
    monday = today + timedelta(days=7 - today.weekday())
    return monday, monday + timedelta(days=6)


@dataclass(frozen=True, slots=True)
class CycleSetup:
    """Configuration read before the first Garmin or model call."""

    instruction: CoachInstruction
    zone: ZoneInfo
    auth_config: GarminAuthConfig
    garmin_factory: GarminFactory
    history_start: str | None
    now: datetime


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    """The one chat message of a run, the week it belongs to, and the exit code."""

    chat_message: str
    weekly_plan_id: int | None
    exit_code: int
    garmin_login_needed: bool


@dataclass(frozen=True, slots=True)
class _Failure:
    step: CycleStep
    cause: str
    exit_code: int = 1
    garmin_login_needed: bool = False
    weekly_plan_id: int | None = None
    done_text: str | None = None


class _StepFailedError(Exception):
    def __init__(self, failure: _Failure) -> None:
        super().__init__(f"weekly cycle step {failure.step.value} failed")
        self.failure = failure


@dataclass(frozen=True, slots=True)
class _Cycle:
    connection: sqlite3.Connection
    adapter: LLMAdapter
    setup: CycleSetup
    first_day: date
    last_day: date


def _exit_code(error: Exception) -> int:
    if isinstance(error, GarminConfigurationError):
        return _INPUT_EXIT
    if isinstance(error, (GarminMfaRequiredError, AthleteSummaryRejectedError)):
        return _REJECTED_EXIT
    return 1


@dataclass(frozen=True, slots=True)
class _PushedWeek:
    result: PushResult
    week_id: int | None
    comparison: tuple[str, ...]


def _caught_failure(step: CycleStep, error: Exception) -> _Failure:
    return _Failure(
        step,
        error_cause(error),
        exit_code=_exit_code(error),
        garmin_login_needed=isinstance(error, GarminMfaRequiredError),
    )


@contextmanager
def _step(step: CycleStep) -> Iterator[None]:
    try:
        yield
    except STEP_ERRORS as error:
        _LOGGER.warning("weekly cycle step %d failed: %s", step, type(error).__name__)
        raise _StepFailedError(_caught_failure(step, error)) from error
    _LOGGER.info("weekly cycle step %d done", step)


def _pull(cycle: _Cycle) -> None:
    setup = cycle.setup
    with _step(CycleStep.PULL):
        pull_garmin(
            cycle.connection,
            setup.auth_config,
            setup.zone,
            factory=setup.garmin_factory,
            now=setup.now,
            history_start=setup.history_start,
        )


def _context(cycle: _Cycle) -> CoachContext:
    with _step(CycleStep.CONTEXT):
        return assemble_coach_context(cycle.connection, cycle.first_day)


def _week(cycle: _Cycle, context: CoachContext) -> PushWeek:
    setup = cycle.setup
    with _step(CycleStep.GENERATE):
        instruction_text = render_coach_system(
            setup.instruction, cycle.connection, now=setup.now, zone=setup.zone
        )
        outcome = generate_week(
            cycle.adapter,
            instruction_text=instruction_text,
            context_text=context.text,
            first_day=cycle.first_day,
            last_day=cycle.last_day,
        )
    if isinstance(outcome, Rejected):
        _LOGGER.warning("weekly cycle step %d failed: rejected", CycleStep.VALIDATE)
        cause = rejected_week_cause(outcome.errors)
        raise _StepFailedError(_Failure(CycleStep.VALIDATE, cause, _REJECTED_EXIT))
    return outcome.value


def _stored_week_id(cycle: _Cycle, week: PushWeek) -> int | None:
    try:
        return active_weekly_plan_id(cycle.connection, week.week_start)
    except sqlite3.Error:
        return None


def _pushed(cycle: _Cycle, week: PushWeek) -> _PushedWeek:
    setup = cycle.setup
    with _step(CycleStep.PUSH):
        comparison = week_comparison_lines(cycle.connection, week)
        result = push_garmin_week(
            cycle.connection, setup.auth_config, week, factory=setup.garmin_factory
        )
        weekly_plan_id = active_weekly_plan_id(cycle.connection, week.week_start)
        return _PushedWeek(result, weekly_plan_id, comparison)


def _push(cycle: _Cycle, week: PushWeek) -> _PushedWeek:
    try:
        pushed = _pushed(cycle, week)
    except _StepFailedError as failed:
        stored = replace(failed.failure, weekly_plan_id=_stored_week_id(cycle, week))
        raise _StepFailedError(stored) from failed.__cause__
    if pushed.result.failed is not None:
        failure = _Failure(
            CycleStep.PUSH,
            pushed.result.chat_message,
            weekly_plan_id=pushed.week_id,
            done_text=_week_block(week, pushed),
        )
        raise _StepFailedError(failure)
    return pushed


def _summarize(cycle: _Cycle, context: CoachContext) -> None:
    setup = cycle.setup
    with _step(CycleStep.SUMMARY):
        system = athlete_summary_system(
            cycle.connection, setup.instruction, now=setup.now, zone=setup.zone
        )
        update_athlete_summary(
            cycle.connection, cycle.adapter, system=system, context=context
        )


def _failed_outcome(failure: _Failure) -> CycleOutcome:
    text = failure_text(failure.step, failure.cause)
    if failure.done_text is not None:
        text = f"{failure.done_text}\n{text}"
    return CycleOutcome(
        text, failure.weekly_plan_id, failure.exit_code, failure.garmin_login_needed
    )


def _week_block(week: PushWeek, pushed: _PushedWeek) -> str:
    return "\n".join((*planned_week_lines(week), *pushed.comparison))


def _week_message(week: PushWeek, pushed: _PushedWeek) -> str:
    return f"{_week_block(week, pushed)}\n{pushed.result.chat_message}"


def _planned_and_pushed(cycle: _Cycle) -> tuple[CoachContext, PushWeek, _PushedWeek]:
    _pull(cycle)
    context = _context(cycle)
    week = _week(cycle, context)
    return context, week, _push(cycle, week)


def run_weekly_cycle(
    connection: sqlite3.Connection, adapter: LLMAdapter, setup: CycleSetup
) -> CycleOutcome:
    """
    Plan, push and summarize the Monday-to-Sunday week after today.

    A failed step ends the run; no later step runs. Writes no chat message.
    Used as `outcome = run_weekly_cycle(connection, adapter, setup)`
    """
    today = date.fromisoformat(training_date(setup.now, setup.zone))
    cycle = _Cycle(connection, adapter, setup, *next_week_range(today))
    try:
        context, week, pushed = _planned_and_pushed(cycle)
    except _StepFailedError as failed:
        return _failed_outcome(failed.failure)
    try:
        _summarize(cycle, context)
    except _StepFailedError as failed:
        after_push = replace(
            failed.failure,
            weekly_plan_id=pushed.week_id,
            done_text=_week_message(week, pushed),
        )
        return _failed_outcome(after_push)
    return CycleOutcome(_week_message(week, pushed), pushed.week_id, 0, False)
