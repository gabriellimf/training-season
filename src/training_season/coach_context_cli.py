"""Command-line entry point for the coach context and the athlete summary rewrite."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from training_season.athlete_summary import (
    AthleteSummaryRejectedError,
    athlete_summary_system,
    update_athlete_summary,
)
from training_season.cli_entry import report_failure, run_cli
from training_season.coach_context import assemble_coach_context
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
from training_season.llm import (
    LLMConfigurationError,
    LLMError,
    llm_config_from_environment,
    new_llm_adapter,
)
from training_season.timeformat import (
    TrainingTimezoneError,
    parse_iso_date,
    training_zone_from_environment,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import httpx2

    from training_season.athlete_summary import StoredSummary
    from training_season.coach_context import CoachContext
    from training_season.llm import LLMConfig

_USAGE = "training-season-context show <YYYY-MM-DD> | summary <YYYY-MM-DD>"
_INPUT_ERROR = 2
_SUMMARY_REJECTED = 3


class _BadArgumentError(ValueError):
    """An argument is not usable; the message names it and the expected shape."""


@dataclass(frozen=True, slots=True)
class _CommandInputs:
    environ: Mapping[str, str]
    transport: httpx2.BaseTransport | None


_INPUT_ERRORS = (
    _BadArgumentError,
    DatabaseConfigurationError,
    LLMConfigurationError,
    CoachInstructionError,
    TrainingTimezoneError,
)


def _target_week(week_start_text: str) -> date:
    week_start = parse_iso_date(week_start_text)
    if week_start is None:
        raise _BadArgumentError(
            f"target week is {week_start_text!r}, expected YYYY-MM-DD"
        )
    return week_start


def _show_context(week_start_text: str, inputs: _CommandInputs) -> int:
    target = _target_week(week_start_text)
    initialize_database(inputs.environ)
    with closing(connect(database_path_from_environment(inputs.environ))) as connection:
        context_text = assemble_coach_context(connection, target).text
    sys.stdout.write(context_text)
    sys.stderr.write(f"Contexto: {len(context_text)} caracteres\n")
    return 0


def _stored_rewrite(
    connection: sqlite3.Connection,
    llm_config: LLMConfig,
    transport: httpx2.BaseTransport | None,
    *,
    system: str,
    context: CoachContext,
) -> StoredSummary:
    adapter = new_llm_adapter(llm_config, transport=transport)
    try:
        return update_athlete_summary(
            connection, adapter, system=system, context=context
        )
    finally:
        adapter.close()


def _rewrite_summary(week_start_text: str, inputs: _CommandInputs) -> int:
    target = _target_week(week_start_text)
    llm_config = llm_config_from_environment(inputs.environ)
    instruction = load_coach_instruction(inputs.environ)
    zone = training_zone_from_environment(inputs.environ)
    initialize_database(inputs.environ)
    with closing(connect(database_path_from_environment(inputs.environ))) as connection:
        context = assemble_coach_context(connection, target)
        system = athlete_summary_system(
            connection, instruction, now=datetime.now(UTC), zone=zone
        )
        stored = _stored_rewrite(
            connection, llm_config, inputs.transport, system=system, context=context
        )
    sys.stdout.write(f"Resumo salvo: id {stored.summary_id}\n{stored.text}\n")
    return 0


_COMMANDS: dict[str, Callable[[str, _CommandInputs], int]] = {
    "show": _show_context,
    "summary": _rewrite_summary,
}


def _is_known_command(arguments: tuple[str, ...]) -> bool:
    return len(arguments) == 2 and arguments[0] in _COMMANDS


def _context_cli(arguments: tuple[str, ...], inputs: _CommandInputs) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    command, week_start_text = arguments
    try:
        return _COMMANDS[command](week_start_text, inputs)
    except AthleteSummaryRejectedError as error:
        return report_failure(error, _SUMMARY_REJECTED)
    except _INPUT_ERRORS as error:
        return report_failure(error, _INPUT_ERROR)
    except (MigrationError, sqlite3.Error, LLMError) as error:
        return report_failure(error, 1)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx2.BaseTransport | None = None,
) -> int:
    """
    Print the coach context, or rewrite and store the athlete summary, for a week.

    Used as `main(["show", "2026-11-02"])` or `main(["summary", "2026-11-02"])`
    """
    inputs = _CommandInputs(os.environ if environ is None else environ, transport)
    return run_cli(
        argv,
        _USAGE,
        _is_known_command,
        lambda arguments: _context_cli(arguments, inputs),
    )


if __name__ == "__main__":
    raise SystemExit(main())
