"""The onboarding chat on a console: session choice, intro, turns and week 1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.bounded_text import drop_control_characters_but_newline
from training_season.coach_context_store import read_active_macro
from training_season.coach_system import coach_system_for_now
from training_season.onboarding_store import (
    current_open_session,
    discard_open_session,
    open_session,
    session_messages,
)
from training_season.onboarding_turn import OnboardingTurnResult, onboarding_turn
from training_season.validated_reply import Rejected
from training_season.week_one import push_week_one

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import date, datetime
    from typing import TextIO
    from zoneinfo import ZoneInfo

    from training_season.coach_instruction import CoachInstruction
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory
    from training_season.llm import LLMAdapter
    from training_season.push_result import PushResult

PROMPT = "Você: "
LEAVE_COMMAND = "/sair"
_SPEAKER = "Treinador: "
FIRST_INTRO = (
    "Olá! Vou montar seu plano de corrida até a sua prova. "
    "Conte um pouco sobre você para começarmos."
)
REOPEN_INTRO = (
    "Vamos revisar o seu objetivo. Conte o que mudou e por quê. "
    "O plano atual fica valendo até o novo ficar pronto."
)
_PAUSE_HINT = "Digite /sair para pausar."
ACTIVE_PLAN_TEXT = "Já existe um plano ativo. Use `reopen` para mudar o objetivo."
NO_ACTIVE_PLAN_TEXT = "Nenhum plano ativo. Use `chat` para o primeiro onboarding."
NO_OPEN_SESSION_TEXT = "Nenhuma sessão de onboarding aberta."
_REJECTED_WEEK = 3
_SESSION_TOO_LONG = 2


class OnboardingStateError(ValueError):
    """The command does not fit the stored plan state; the message says which."""


@dataclass(frozen=True, slots=True)
class CoachSetup:
    """The configuration every coach command reads before any write."""

    instruction: CoachInstruction
    zone: ZoneInfo
    auth_config: GarminAuthConfig
    garmin_factory: GarminFactory
    clock: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class Coach:
    """An open database connection and model adapter for one command."""

    connection: sqlite3.Connection
    adapter: LLMAdapter
    setup: CoachSetup
    out: TextIO


def session_for(connection: sqlite3.Connection, *, reopening: bool) -> int:
    """
    Resume the open session, else open one if the plan state fits the command.

    `chat` needs no active macro; `reopen` needs one.
    Used as `session_id = session_for(connection, reopening=False)`
    """
    open_id = current_open_session(connection)
    if open_id is not None:
        return open_id
    has_active_macro = read_active_macro(connection) is not None
    if has_active_macro != reopening:
        message = ACTIVE_PLAN_TEXT if has_active_macro else NO_ACTIVE_PLAN_TEXT
        raise OnboardingStateError(message)
    return open_session(connection)


def onboarding_intro(*, reopening: bool) -> str:
    """
    The opening text of a session; a review of the goal has its own.

    Used as `onboarding_intro(reopening=read_active_macro(connection) is not None)`
    """
    return REOPEN_INTRO if reopening else FIRST_INTRO


def say(out: TextIO, text: str) -> None:
    """Write one coach line on the console: `say(out, "Olá")`."""
    out.write(f"{_SPEAKER}{text}\n")


def rendered_for_now(
    setup: CoachSetup, connection: sqlite3.Connection
) -> tuple[str, date]:
    """
    Render the coach system text for the current clock, with the training date.

    Used as `instruction_text, today = rendered_for_now(setup, connection)`
    """
    return coach_system_for_now(
        setup.instruction, connection, now=setup.clock(), zone=setup.zone
    )


def first_week_outcome(
    connection: sqlite3.Connection, adapter: LLMAdapter, setup: CoachSetup
) -> PushResult | Rejected:
    """
    Generate and push today..Sunday under the active macro; nothing is printed.

    Used as `outcome = first_week_outcome(connection, adapter, setup)`
    """
    instruction_text, today = rendered_for_now(setup, connection)
    return push_week_one(
        connection,
        adapter,
        setup.auth_config,
        instruction_text=instruction_text,
        today=today,
        factory=setup.garmin_factory,
    )


def push_first_week(coach: Coach) -> int:
    """
    Generate and push today..Sunday, print the outcome, and return the exit code.

    Used as `return push_first_week(coach)`
    """
    outcome = first_week_outcome(coach.connection, coach.adapter, coach.setup)
    coach.out.write(f"{outcome.chat_message}\n")
    if isinstance(outcome, Rejected):
        return _REJECTED_WEEK
    return 0 if outcome.failed is None else 1


def send_onboarding_message(
    connection: sqlite3.Connection,
    adapter: LLMAdapter,
    setup: CoachSetup,
    session_id: int,
    text: str,
) -> OnboardingTurnResult:
    """
    Run one onboarding turn on the open session with the current coach system.

    Used as `result = send_onboarding_message(connection, adapter, setup, 3, "Oi")`
    """
    instruction_text, today = rendered_for_now(setup, connection)
    return onboarding_turn(
        connection,
        adapter,
        session_id=session_id,
        instruction_text=instruction_text,
        user_text=text,
        today=today,
    )


def cleaned_athlete_text(text: str) -> str:
    """
    Drop control characters except newlines, then outer spaces.

    Used as `text = cleaned_athlete_text(" Oi ")`
    """
    return drop_control_characters_but_newline(text).strip()


def read_stripped_line(read_line: Callable[[str], str], out: TextIO) -> str | None:
    """
    Read one athlete line without control characters or outer spaces; None on EOF.

    Used as `line = read_stripped_line(input, sys.stdout)`
    """
    try:
        return cleaned_athlete_text(read_line(PROMPT))
    except EOFError:
        out.write("\n")
        return None


def next_athlete_line(read_line: Callable[[str], str], out: TextIO) -> str | None:
    """
    Read the next non-blank athlete line; None on EOF or `/sair`.

    Used as `while (text := next_athlete_line(input, sys.stdout)) is not None:`
    """
    line = read_stripped_line(read_line, out)
    while line == "":
        line = read_stripped_line(read_line, out)
    return None if line == LEAVE_COMMAND else line


def converse(coach: Coach, session_id: int, read_line: Callable[[str], str]) -> int:
    """
    Run the chat until EOF, `/sair` or completion; completion pushes week 1.

    The session stays open on EOF or `/sair`, so the next run resumes it.
    A session too long to send ends the chat with exit code 2.
    Used as `return converse(coach, session_id, input)`
    """
    if not session_messages(coach.connection, session_id):
        reopening = read_active_macro(coach.connection) is not None
        say(coach.out, f"{onboarding_intro(reopening=reopening)} {_PAUSE_HINT}")
    while (text := next_athlete_line(read_line, coach.out)) is not None:
        result = send_onboarding_message(
            coach.connection, coach.adapter, coach.setup, session_id, text
        )
        say(coach.out, result.chat_message)
        if result.status == "completed":
            return push_first_week(coach)
        if result.status == "session_too_long":
            return _SESSION_TOO_LONG
    return 0


def discard_session(connection: sqlite3.Connection) -> str:
    """
    Discard the open session and return the Portuguese confirmation.

    Used as `out.write(discard_session(connection))`
    """
    discarded_id = discard_open_session(connection)
    if discarded_id is None:
        raise OnboardingStateError(NO_OPEN_SESSION_TEXT)
    return (
        f"Sessão de onboarding {discarded_id} descartada. As mensagens ficam salvas. "
        "Use `chat` ou `reopen` para começar de novo."
    )
