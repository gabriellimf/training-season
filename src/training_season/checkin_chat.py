"""The check-in on a console: the fixed questions, then a chat with the coach."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

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
    from datetime import date, datetime
    from typing import TextIO
    from zoneinfo import ZoneInfo

    from training_season.coach_instruction import CoachInstruction
    from training_season.llm import LLMAdapter

type CheckinReplyStatus = Literal[
    "replied", "message_too_long", "conversation_too_long", "model_failed"
]

CHECKIN_INTRO = (
    "Vamos fazer o check-in: três perguntas rápidas sobre como você está, "
    "depois conversamos se quiser."
)
_LEAVE_HINT = "Digite /sair para terminar."
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
_ENDING_EXIT_CODES: dict[CheckinReplyStatus, int] = {
    "model_failed": 1,
    "conversation_too_long": 2,
}
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
class CheckinConversation:
    """One check-in chat: its stored row, the request texts and the turns so far."""

    checkin_id: int
    system: str = field(repr=False)
    kickoff: str = field(repr=False)
    history: tuple[SessionMessage, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class CheckinReply:
    """The outcome of one athlete message, its Portuguese text and the new state."""

    status: CheckinReplyStatus
    chat_message: str = field(repr=False)
    conversation: CheckinConversation

    @property
    def ends_conversation(self) -> bool:
        """True when the chat cannot go on: `if reply.ends_conversation: ...`."""
        return self.status in _ENDING_EXIT_CODES


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
    say(coach.out, f"{CHECKIN_INTRO} {_LEAVE_HINT}")
    now = coach.setup.clock()
    system, today = coach_system_for_now(
        coach.setup.instruction, coach.connection, now=now, zone=coach.setup.zone
    )
    checkin_id = record_checkin(
        coach.connection, read_line, coach.out, today=today, now=now
    )
    if checkin_id is None:
        return 0
    conversation = open_checkin_conversation(
        coach.connection, checkin_id, system, today
    )
    return _converse(coach, conversation, read_line)


def open_checkin_conversation(
    connection: sqlite3.Connection, checkin_id: int, system: str, today: date
) -> CheckinConversation:
    """
    Start the chat for a stored check-in, with the context of the week to plan.

    Used as `conversation = open_checkin_conversation(connection, 7, system, today)`
    """
    context_text = assemble_coach_context(connection, next_week_range(today)[0]).text
    return CheckinConversation(checkin_id, system, checkin_kickoff_text(context_text))


def _converse(
    coach: CheckinCoach,
    conversation: CheckinConversation,
    read_line: Callable[[str], str],
) -> int:
    while (text := next_athlete_line(read_line, coach.out)) is not None:
        reply = checkin_reply(coach.connection, coach.adapter, conversation, text)
        say(coach.out, reply.chat_message)
        exit_code = _ENDING_EXIT_CODES.get(reply.status)
        if exit_code is not None:
            return exit_code
        conversation = reply.conversation
    return 0


def _refused_length(
    conversation: CheckinConversation, text: str
) -> CheckinReply | None:
    if len(text) > MAX_ATHLETE_MESSAGE_LENGTH:
        return CheckinReply("message_too_long", MESSAGE_TOO_LONG_TEXT, conversation)
    conversation_length = sum(len(turn.content) for turn in conversation.history)
    if conversation_length + len(text) > MAX_SESSION_HISTORY_LENGTH:
        status: CheckinReplyStatus = "conversation_too_long"
        return CheckinReply(status, _CONVERSATION_TOO_LONG_TEXT, conversation)
    return None


def checkin_reply(
    connection: sqlite3.Connection,
    adapter: LLMAdapter,
    conversation: CheckinConversation,
    text: str,
) -> CheckinReply:
    """
    Store one athlete message in the free text and ask the coach for a reply.

    A refused length stores nothing. A failed model call keeps the stored message.
    Used as `reply = checkin_reply(connection, adapter, conversation, "Dormi mal")`
    """
    refused = _refused_length(conversation, text)
    if refused is not None:
        return refused
    append_checkin_message(connection, conversation.checkin_id, text)
    reply = _coach_reply(adapter, conversation, text)
    if reply is None:
        return CheckinReply("model_failed", _MODEL_FAILED_TEXT, conversation)
    turns = (SessionMessage("user", text), SessionMessage("assistant", reply))
    grown = replace(conversation, history=(*conversation.history, *turns))
    return CheckinReply("replied", reply, grown)


def _coach_reply(
    adapter: LLMAdapter, conversation: CheckinConversation, text: str
) -> str | None:
    turns = conversation_turns(conversation.kickoff, conversation.history, text)
    try:
        reply = adapter.complete(LLMJob.CHAT, system=conversation.system, turns=turns)
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
