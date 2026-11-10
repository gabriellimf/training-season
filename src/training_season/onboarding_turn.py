"""One onboarding turn: ask the coach, store the exchange, complete on a result."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from training_season.coach_context import assemble_coach_context
from training_season.coach_context_store import read_active_macro
from training_season.llm import LLMJob
from training_season.macro_generation import generate_macro
from training_season.macro_store import store_onboarding_outcome
from training_season.onboarding_contract import (
    InterviewReply,
    conversation_turns,
    interview_reply_from_text,
    interview_retry_request,
    kickoff_text,
    onboarding_system_text,
    split_result,
)
from training_season.onboarding_store import (
    SessionExchange,
    SessionMessage,
    append_exchange,
    session_messages,
)
from training_season.validated_reply import Accepted, Rejected, request_with_one_retry

if TYPE_CHECKING:
    import sqlite3
    from datetime import date

    from training_season.athlete_profile import OnboardingResult
    from training_season.llm import LLMAdapter

TurnStatus = Literal[
    "asking",
    "completed",
    "result_rejected",
    "macro_rejected",
    "message_too_long",
    "session_too_long",
]

MAX_ATHLETE_MESSAGE_LENGTH = 4_000
MAX_SESSION_HISTORY_LENGTH = 100_000

_LOGGER = logging.getLogger(__name__)
_MESSAGE_TOO_LONG_TEXT = (
    f"Sua mensagem passa de {MAX_ATHLETE_MESSAGE_LENGTH} caracteres. "
    "Envie um texto mais curto."
)
_SESSION_TOO_LONG_TEXT = (
    "Esta sessão de onboarding ficou longa demais para continuar. Rode "
    "`training-season-onboarding discard` para descartá-la e comece de novo."
)
_CLOSING_TEXT = "Onboarding concluído. Plano salvo."
_RESULT_FAILED_TEXT = (
    "Não consegui registrar os dados do onboarding. Responda para tentar de novo."
)
_RESULT_REJECTION_HEADLINE = "O resultado do onboarding falhou na validação."


@dataclass(frozen=True, slots=True)
class OnboardingTurnResult:
    """What the turn did. `chat_message` is the Portuguese text for the athlete."""

    status: TurnStatus
    chat_message: str = field(repr=False)
    macro_plan_id: int | None


@dataclass(frozen=True, slots=True)
class _Turn:
    connection: sqlite3.Connection
    adapter: LLMAdapter
    session_id: int
    instruction_text: str
    user_text: str = field(repr=False)
    today: date


def onboarding_turn(
    connection: sqlite3.Connection,
    adapter: LLMAdapter,
    *,
    session_id: int,
    instruction_text: str,
    user_text: str,
    today: date,
) -> OnboardingTurnResult:
    """
    Send one athlete message of an open session and store what it gives.

    An `LLMError` reaches the caller and nothing is stored.
    Used as `onboarding_turn(connection, adapter, session_id=4,
    instruction_text=rendered, user_text="Oi", today=date(2026, 11, 10))`
    """
    turn = _Turn(connection, adapter, session_id, instruction_text, user_text, today)
    history = session_messages(connection, session_id)
    if refused := _refused_by_size(turn, history):
        return _logged(turn, refused)
    context_text = assemble_coach_context(connection, today).text
    outcome = _interview(turn, context_text, history)
    if isinstance(outcome, Rejected):
        return _store_result_failure(turn, outcome)
    if outcome.value.result is None:
        return _store_question(turn, outcome.value.visible)
    return _complete(turn, outcome.value.result, context_text, _closing_text(outcome))


def _refused_by_size(
    turn: _Turn, history: tuple[SessionMessage, ...]
) -> OnboardingTurnResult | None:
    if len(turn.user_text) > MAX_ATHLETE_MESSAGE_LENGTH:
        return OnboardingTurnResult("message_too_long", _MESSAGE_TOO_LONG_TEXT, None)
    history_length = sum(len(message.content) for message in history)
    if history_length + len(turn.user_text) > MAX_SESSION_HISTORY_LENGTH:
        return OnboardingTurnResult("session_too_long", _SESSION_TOO_LONG_TEXT, None)
    return None


def _interview(
    turn: _Turn, context_text: str, history: tuple[SessionMessage, ...]
) -> Accepted[InterviewReply] | Rejected:
    reopening = read_active_macro(turn.connection) is not None
    kickoff = kickoff_text(context_text, reopening=reopening)
    return request_with_one_retry(
        turn.adapter,
        LLMJob.CHAT,
        system=onboarding_system_text(turn.instruction_text),
        turns=conversation_turns(kickoff, history, turn.user_text),
        parse=lambda text: interview_reply_from_text(text, turn.today),
        rejection_headline=_RESULT_REJECTION_HEADLINE,
        retry_request=interview_retry_request,
    )


def _store_question(turn: _Turn, visible: str) -> OnboardingTurnResult:
    append_exchange(turn.connection, turn.session_id, turn.user_text, visible)
    return _logged(turn, OnboardingTurnResult("asking", visible, None))


def _store_result_failure(turn: _Turn, rejected: Rejected) -> OnboardingTurnResult:
    first_visible = split_result(rejected.replies[0].text).visible
    parts = (first_visible, _RESULT_FAILED_TEXT)
    message = "\n\n".join(part for part in parts if part)
    append_exchange(turn.connection, turn.session_id, turn.user_text, message)
    return _logged(turn, OnboardingTurnResult("result_rejected", message, None))


def _closing_text(outcome: Accepted[InterviewReply]) -> str:
    if outcome.value.visible:
        return outcome.value.visible
    first_visible = split_result(outcome.replies[0].text).visible
    return first_visible or _CLOSING_TEXT


def _complete(
    turn: _Turn, result: OnboardingResult, context_text: str, visible: str
) -> OnboardingTurnResult:
    macro = generate_macro(
        turn.adapter,
        instruction_text=turn.instruction_text,
        result=result,
        context_text=context_text,
        today=turn.today,
    )
    if isinstance(macro, Rejected):
        return _logged(
            turn, OnboardingTurnResult("macro_rejected", macro.chat_message, None)
        )
    macro_plan_id = store_onboarding_outcome(
        turn.connection,
        session_id=turn.session_id,
        result=result,
        plan=macro.value,
        final_exchange=SessionExchange(turn.user_text, visible),
    )
    return _logged(turn, OnboardingTurnResult("completed", visible, macro_plan_id))


def _logged(turn: _Turn, result: OnboardingTurnResult) -> OnboardingTurnResult:
    _LOGGER.info(
        "Onboarding turn session=%d status=%s macro_plan=%s",
        turn.session_id,
        result.status,
        result.macro_plan_id,
    )
    return result
