"""The onboarding chat on a console: session choice, intro, turns and week 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from training_season.bounded_text import drop_control_characters_but_newline
from training_season.coach_context_store import read_active_macro
from training_season.coach_system import render_coach_system
from training_season.onboarding_store import (
    current_open_session,
    discard_open_session,
    open_session,
    session_messages,
)
from training_season.onboarding_turn import OnboardingTurnResult, onboarding_turn
from training_season.timeformat import training_date
from training_season.validated_reply import Rejected
from training_season.week_one import push_week_one

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import datetime
    from typing import TextIO
    from zoneinfo import ZoneInfo

    from training_season.coach_instruction import CoachInstruction
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory
    from training_season.llm import LLMAdapter

PROMPT = "Você: "
_LEAVE_COMMAND = "/sair"
_SPEAKER = "Treinador: "
_FIRST_INTRO = (
    "Olá! Vou montar seu plano de corrida até a sua prova. "
    "Conte um pouco sobre você para começarmos. Digite /sair para pausar."
)
_REOPEN_INTRO = (
    "Vamos revisar o seu objetivo. Conte o que mudou e por quê. "
    "O plano atual fica valendo até o novo ficar pronto. Digite /sair para pausar."
)
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


def say(out: TextIO, text: str) -> None:
    """Write one coach line on the console: `say(out, "Olá")`."""
    out.write(f"{_SPEAKER}{text}\n")


def _rendered_for_now(coach: Coach) -> tuple[str, date]:
    now = coach.setup.clock()
    rendered = render_coach_system(
        coach.setup.instruction, coach.connection, now=now, zone=coach.setup.zone
    )
    return rendered, date.fromisoformat(training_date(now, coach.setup.zone))


def push_first_week(coach: Coach) -> int:
    """
    Generate and push today..Sunday, print the outcome, and return the exit code.

    Used as `return push_first_week(coach)`
    """
    instruction_text, today = _rendered_for_now(coach)
    outcome = push_week_one(
        coach.connection,
        coach.adapter,
        coach.setup.auth_config,
        instruction_text=instruction_text,
        today=today,
        factory=coach.setup.garmin_factory,
    )
    coach.out.write(f"{outcome.chat_message}\n")
    if isinstance(outcome, Rejected):
        return _REJECTED_WEEK
    return 0 if outcome.failed is None else 1


def _send(coach: Coach, session_id: int, text: str) -> OnboardingTurnResult:
    instruction_text, today = _rendered_for_now(coach)
    return onboarding_turn(
        coach.connection,
        coach.adapter,
        session_id=session_id,
        instruction_text=instruction_text,
        user_text=text,
        today=today,
    )


def _read_stripped(read_line: Callable[[str], str], out: TextIO) -> str | None:
    try:
        return drop_control_characters_but_newline(read_line(PROMPT)).strip()
    except EOFError:
        out.write("\n")
        return None


def _next_line(read_line: Callable[[str], str], out: TextIO) -> str | None:
    line = _read_stripped(read_line, out)
    while line == "":
        line = _read_stripped(read_line, out)
    return None if line == _LEAVE_COMMAND else line


def converse(coach: Coach, session_id: int, read_line: Callable[[str], str]) -> int:
    """
    Run the chat until EOF, `/sair` or completion; completion pushes week 1.

    The session stays open on EOF or `/sair`, so the next run resumes it.
    A session too long to send ends the chat with exit code 2.
    Used as `return converse(coach, session_id, input)`
    """
    if not session_messages(coach.connection, session_id):
        reopening = read_active_macro(coach.connection) is not None
        say(coach.out, _REOPEN_INTRO if reopening else _FIRST_INTRO)
    while (text := _next_line(read_line, coach.out)) is not None:
        result = _send(coach, session_id, text)
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
