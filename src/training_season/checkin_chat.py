"""The check-in on a console: the fixed questions, then a chat with the coach."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from training_season.bounded_text import drop_control_characters_but_newline
from training_season.checkin_questions import record_checkin
from training_season.checkin_store import append_checkin_message
from training_season.coach_context import assemble_coach_context
from training_season.coach_system import coach_system_for_now
from training_season.llm import LLMError, LLMJob
from training_season.onboarding_chat import next_athlete_line, say
from training_season.onboarding_contract import conversation_turns
from training_season.onboarding_store import SessionMessage
from training_season.onboarding_turn import (
    MAX_ATHLETE_MESSAGE_LENGTH,
    MAX_SESSION_HISTORY_LENGTH,
    MESSAGE_TOO_LONG_TEXT,
)
from training_season.weekly_cycle import next_week_range

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import datetime
    from typing import TextIO
    from zoneinfo import ZoneInfo

    from training_season.coach_instruction import CoachInstruction
    from training_season.llm import LLMAdapter

CHECKIN_INTRO = (
    "Vamos fazer o check-in: três perguntas rápidas sobre como você está, "
    "depois conversamos se quiser. Digite /sair para terminar."
)
_KICKOFF_HEAD = (
    "Início do check-in: o atleta já respondeu às perguntas fixas, e as respostas "
    "estão salvas na seção Check-in do contexto. Converse com ele sobre como está."
)
_CONVERSATION_TOO_LONG_TEXT = (
    "Esta conversa de check-in ficou longa demais para continuar. "
    "Suas respostas e mensagens estão salvas."
)
_MODEL_FAILED_TEXT = (
    "Não consegui falar com o treinador agora. Suas respostas e mensagens estão salvas."
)
_MODEL_FAILED = 1
_CONVERSATION_TOO_LONG = 2
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CheckinSetup:
    """The configuration the check-in reads before any write."""

    instruction: CoachInstruction
    zone: ZoneInfo
    clock: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class CheckinCoach:
    """An open database connection and model adapter for one check-in."""

    connection: sqlite3.Connection
    adapter: LLMAdapter
    setup: CheckinSetup
    out: TextIO


@dataclass(frozen=True, slots=True)
class _Chat:
    coach: CheckinCoach
    checkin_id: int
    system: str = field(repr=False)
    kickoff: str = field(repr=False)


def checkin_kickoff_text(context_text: str) -> str:
    """
    The code-owned opening of every check-in request. It is never stored.

    Used as `checkin_kickoff_text(assemble_coach_context(connection, monday).text)`
    """
    return f"{_KICKOFF_HEAD}\n\nContexto do atleta:\n{context_text}"


def run_checkin(coach: CheckinCoach, read_line: Callable[[str], str]) -> int:
    """
    Ask the fixed questions, store them, then chat until EOF or `/sair`.

    Leaving before the last answer stores nothing and calls no model.
    Used as `return run_checkin(coach, input)`
    """
    say(coach.out, CHECKIN_INTRO)
    now = coach.setup.clock()
    system, today = coach_system_for_now(
        coach.setup.instruction, coach.connection, now=now, zone=coach.setup.zone
    )
    checkin_id = record_checkin(
        coach.connection, read_line, coach.out, today=today, now=now
    )
    if checkin_id is None:
        return 0
    context_text = assemble_coach_context(
        coach.connection, next_week_range(today)[0]
    ).text
    chat = _Chat(coach, checkin_id, system, checkin_kickoff_text(context_text))
    return _converse(chat, read_line)


def _converse(chat: _Chat, read_line: Callable[[str], str]) -> int:
    history: list[SessionMessage] = []
    while (text := next_athlete_line(read_line, chat.coach.out)) is not None:
        exit_code = _answer(chat, history, text)
        if exit_code is not None:
            return exit_code
    return 0


def _answer(chat: _Chat, history: list[SessionMessage], text: str) -> int | None:
    out = chat.coach.out
    if len(text) > MAX_ATHLETE_MESSAGE_LENGTH:
        say(out, MESSAGE_TOO_LONG_TEXT)
        return None
    conversation_length = sum(len(message.content) for message in history)
    if conversation_length + len(text) > MAX_SESSION_HISTORY_LENGTH:
        say(out, _CONVERSATION_TOO_LONG_TEXT)
        return _CONVERSATION_TOO_LONG
    append_checkin_message(chat.coach.connection, chat.checkin_id, text)
    reply = _coach_reply(chat, history, text)
    if reply is None:
        say(out, _MODEL_FAILED_TEXT)
        return _MODEL_FAILED
    history.extend((SessionMessage("user", text), SessionMessage("assistant", reply)))
    say(out, reply)
    return None


def _coach_reply(chat: _Chat, history: list[SessionMessage], text: str) -> str | None:
    turns = conversation_turns(chat.kickoff, history, text)
    try:
        reply = chat.coach.adapter.complete(
            LLMJob.CHAT, system=chat.system, turns=turns
        )
    except LLMError as error:
        _log_reply_failure(type(error).__name__)
        return None
    cleaned = drop_control_characters_but_newline(reply.text).strip()
    if not cleaned:
        _log_reply_failure("EmptyReply")
        return None
    return cleaned


def _log_reply_failure(error_type: str) -> None:
    _LOGGER.warning("Check-in reply failed error_type=%s", error_type)
