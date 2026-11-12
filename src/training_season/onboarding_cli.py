"""Command-line entry point for the onboarding chat, week 1 and the macro history."""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from training_season.cli_entry import report_failure, run_cli
from training_season.coach_instruction import (
    CoachInstructionError,
    load_coach_instruction,
)
from training_season.coach_system import StoredProfileError
from training_season.database import (
    DatabaseConfigurationError,
    MigrationError,
    connect,
    database_path_from_environment,
    initialize_database,
)
from training_season.garmin_auth import (
    GarminConfigurationError,
    GarminError,
    GarminMfaRequiredError,
    default_garmin_factory,
    garmin_auth_config_from_environment,
)
from training_season.llm import (
    LLMConfigurationError,
    LLMError,
    llm_config_from_environment,
    new_llm_adapter,
)
from training_season.macro_store import read_macro_history, read_macro_plan_json
from training_season.onboarding_chat import (
    Coach,
    CoachSetup,
    OnboardingStateError,
    converse,
    discard_session,
    push_first_week,
    session_for,
)
from training_season.onboarding_store import OnboardingSessionError
from training_season.push_store import NoActiveMacroPlanError
from training_season.timeformat import (
    TrainingTimezoneError,
    training_zone_from_environment,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from typing import TextIO

    from training_season.garmin_auth import GarminFactory
    from training_season.llm import LLMAdapter
    from training_season.macro_store import MacroHistoryEntry

type AdapterFactory = Callable[[Mapping[str, str]], LLMAdapter]

_USAGE = "training-season-onboarding chat | reopen | week | discard | history [<id>]"
_INPUT_ERROR = 2
_MFA_REQUIRED = 3
_MFA_HINT = (
    "Conclua o login com `training-season-garmin` e depois rode "
    "`training-season-onboarding week`."
)
_NO_HISTORY = "Nenhum plano macro salvo."
_STATUS_TEXT = {"active": "ativo", "archived": "arquivado"}
_INPUT_ERRORS = (
    DatabaseConfigurationError,
    LLMConfigurationError,
    CoachInstructionError,
    TrainingTimezoneError,
    GarminConfigurationError,
    NoActiveMacroPlanError,
    OnboardingStateError,
)
_RUNTIME_ERRORS = (
    GarminError,
    LLMError,
    MigrationError,
    OnboardingSessionError,
    StoredProfileError,
    sqlite3.Error,
)


class _BadArgumentError(ValueError):
    """An argument is not usable; the message names it and the expected shape."""


@dataclass(frozen=True, slots=True)
class _CommandInputs:
    environ: Mapping[str, str]
    adapter_factory: AdapterFactory
    garmin_factory: GarminFactory
    clock: Callable[[], datetime]
    read_line: Callable[[str], str]
    out: TextIO


def default_adapter_factory(environ: Mapping[str, str]) -> LLMAdapter:
    """
    Build the configured provider adapter for the chat and plan jobs.

    Used as `adapter = default_adapter_factory(os.environ)`
    """
    return new_llm_adapter(llm_config_from_environment(environ))


def utc_clock() -> datetime:
    """
    Return the current aware UTC time, the default clock of the coach commands.

    Used as `main(clock=utc_clock)`
    """
    return datetime.now(UTC)


def _connected(environ: Mapping[str, str]) -> closing[sqlite3.Connection]:
    initialize_database(environ)
    return closing(connect(database_path_from_environment(environ)))


def _coach_setup(inputs: _CommandInputs) -> CoachSetup:
    return CoachSetup(
        instruction=load_coach_instruction(inputs.environ),
        zone=training_zone_from_environment(inputs.environ),
        auth_config=garmin_auth_config_from_environment(inputs.environ),
        garmin_factory=inputs.garmin_factory,
        clock=inputs.clock,
    )


@contextmanager
def _opened_coach(inputs: _CommandInputs) -> Iterator[Coach]:
    setup = _coach_setup(inputs)
    adapter = inputs.adapter_factory(inputs.environ)
    try:
        with _connected(inputs.environ) as connection:
            yield Coach(connection, adapter, setup, inputs.out)
    finally:
        adapter.close()


def _run_chat(arguments: tuple[str, ...], inputs: _CommandInputs) -> int:
    with _opened_coach(inputs) as coach:
        reopening = arguments[0] == "reopen"
        session_id = session_for(coach.connection, reopening=reopening)
        return converse(coach, session_id, inputs.read_line)


def _run_week(_arguments: tuple[str, ...], inputs: _CommandInputs) -> int:
    with _opened_coach(inputs) as coach:
        return push_first_week(coach)


def _run_discard(_arguments: tuple[str, ...], inputs: _CommandInputs) -> int:
    with _connected(inputs.environ) as connection:
        confirmation = discard_session(connection)
    inputs.out.write(f"{confirmation}\n")
    return 0


def _macro_id(text: str) -> int:
    if not (text.isascii() and text.isdecimal()) or int(text) == 0:
        raise _BadArgumentError(
            f"macro plan id is {text!r}, expected a positive integer"
        )
    return int(text)


def _entry_lines(entry: MacroHistoryEntry) -> tuple[str, ...]:
    status = _STATUS_TEXT.get(entry.status, entry.status)
    archived_at = entry.archived_at
    archived = "" if archived_at is None else f", arquivado em {archived_at}"
    head = (
        f"Plano macro {entry.macro_plan_id}: {status}, {entry.start_date} a "
        f"{entry.race_date}, criado em {entry.created_at}{archived}"
    )
    revisions = tuple(
        f"  Revisão {revision.revision_number} ({revision.created_at}): "
        f"{revision.reason}"
        for revision in entry.revisions
    )
    return (head, *revisions)


def _history_lines(connection: sqlite3.Connection) -> tuple[str, ...]:
    history = read_macro_history(connection)
    if not history:
        return (_NO_HISTORY,)
    return tuple(line for entry in history for line in _entry_lines(entry))


def _plan_json_lines(connection: sqlite3.Connection, text: str) -> tuple[str, ...]:
    plan_json = read_macro_plan_json(connection, _macro_id(text))
    if plan_json is None:
        raise _BadArgumentError(
            f"macro plan id {text!r} is unknown, expected an id listed by history"
        )
    return (plan_json,)


def _run_history(arguments: tuple[str, ...], inputs: _CommandInputs) -> int:
    with _connected(inputs.environ) as connection:
        if len(arguments) == 1:
            lines = _history_lines(connection)
        else:
            lines = _plan_json_lines(connection, arguments[1])
    inputs.out.write("".join(f"{line}\n" for line in lines))
    return 0


_COMMANDS: dict[str, Callable[[tuple[str, ...], _CommandInputs], int]] = {
    "chat": _run_chat,
    "reopen": _run_chat,
    "week": _run_week,
    "discard": _run_discard,
    "history": _run_history,
}


def _is_known_command(arguments: tuple[str, ...]) -> bool:
    if len(arguments) == 2:
        return arguments[0] == "history"
    return len(arguments) == 1 and arguments[0] in _COMMANDS


def _onboarding_cli(arguments: tuple[str, ...], inputs: _CommandInputs) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        return _COMMANDS[arguments[0]](arguments, inputs)
    except GarminMfaRequiredError as error:
        sys.stderr.write(f"{error}\n{_MFA_HINT}\n")
        return _MFA_REQUIRED
    except (_BadArgumentError, *_INPUT_ERRORS) as error:
        return report_failure(error, _INPUT_ERROR)
    except _RUNTIME_ERRORS as error:
        return report_failure(error, 1)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    adapter_factory: AdapterFactory = default_adapter_factory,
    garmin_factory: GarminFactory = default_garmin_factory,
    clock: Callable[[], datetime] = utc_clock,
    read_line: Callable[[str], str] = input,
    out: TextIO | None = None,
) -> int:
    """
    Run the chat, reopen the goal, push week 1, discard a session, or read history.

    Used as `main(["chat"])`, `main(["discard"])` or `main(["history", "1"])`
    """
    inputs = _CommandInputs(
        environ=os.environ if environ is None else environ,
        adapter_factory=adapter_factory,
        garmin_factory=garmin_factory,
        clock=clock,
        read_line=read_line,
        out=sys.stdout if out is None else out,
    )
    return run_cli(
        argv,
        _USAGE,
        _is_known_command,
        lambda arguments: _onboarding_cli(arguments, inputs),
    )


if __name__ == "__main__":
    raise SystemExit(main())
