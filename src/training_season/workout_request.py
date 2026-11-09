"""Ask the coach model for a workout, with one retry on a validation failure."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.llm import ChatTurn
from training_season.workout_validation import (
    WorkoutValidationError,
    workout_from_text,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from training_season.llm import LLMAdapter, LLMJob, LLMReply
    from training_season.workout_schema import Workout

_LOGGER = logging.getLogger(__name__)

WORKOUT_OUTPUT_CONTRACT = """\
Formato da resposta

Responda apenas com um objeto JSON, sem texto antes ou depois.
Campos do objeto:
- name: texto de 1 a 15 caracteres.
- description: texto opcional.
- steps: lista de 1 a 50 passos. Contam todos os passos, inclusive os de dentro de repeat.
Cada passo tem type: warmup, interval, recovery, cooldown ou repeat.
Passo que não é repeat:
- duration: exatamente uma chave, meters ou seconds, inteiro maior que zero.
- pace_mps: {"min": ..., "max": ...} em metros por segundo, min <= max. É o alvo principal.
- heart_rate_zone: inteiro de 1 a 5, opcional.
- cadence_spm: {"min": ..., "max": ...} em passos por minuto, opcional.
- notes: texto opcional.
Passo repeat: count de 1 a 99 e steps com os passos repetidos. Não coloque repeat dentro de repeat.
Não use outras chaves.
Exemplo:
{"name": "26/10 10x400", "description": "Tiros de 400 m com trote de recuperação.",
 "steps": [
  {"type": "warmup", "duration": {"meters": 2000}, "pace_mps": {"min": 2.6, "max": 2.9}, "notes": "Trote leve"},
  {"type": "repeat", "count": 10, "steps": [
   {"type": "interval", "duration": {"meters": 400}, "pace_mps": {"min": 4.0, "max": 4.3}, "cadence_spm": {"min": 175, "max": 185}},
   {"type": "recovery", "duration": {"seconds": 90}, "pace_mps": {"min": 2.2, "max": 2.8}, "heart_rate_zone": 2}]},
  {"type": "cooldown", "duration": {"seconds": 600}, "pace_mps": {"min": 2.6, "max": 2.9}}]}"""  # noqa: E501

_REJECTION_HEADLINE = (
    "O treinador devolveu um treino inválido duas vezes. Nada foi enviado ao Garmin."
)
_RETRY_HEADLINE = "O JSON anterior falhou na validação:"
_RETRY_REQUEST = "Responda apenas com o JSON corrigido."
_EMPTY_REPLY_PLACEHOLDER = "(resposta vazia)"


@dataclass(frozen=True, slots=True)
class WorkoutAccepted:
    """A workout that passed validation, with the replies that produced it."""

    workout: Workout
    attempts: int
    replies: tuple[LLMReply, ...]


@dataclass(frozen=True, slots=True)
class WorkoutRejected:
    """Two invalid replies. `chat_message` is the Portuguese text for the chat."""

    chat_message: str
    errors: tuple[str, ...]
    attempts: int
    replies: tuple[LLMReply, ...]


def workout_system_text(instruction_text: str) -> str:
    """
    Append the workout output contract to the rendered coach instruction.

    Used as `workout_system_text(instruction.render(...))`
    """
    return f"{instruction_text}\n\n{WORKOUT_OUTPUT_CONTRACT}"


def _retry_turn(errors: tuple[str, ...]) -> ChatTurn:
    lines = "\n".join(f"- {line}" for line in errors)
    return ChatTurn("user", f"{_RETRY_HEADLINE}\n{lines}\n{_RETRY_REQUEST}")


def _rejection_message(errors: tuple[str, ...]) -> str:
    return "\n".join((_REJECTION_HEADLINE, *errors))


def _log_failed_attempt(attempt: int, error: WorkoutValidationError) -> None:
    _LOGGER.warning(
        "Workout validation failed attempt=%d error_count=%d",
        attempt,
        len(error.errors),
    )


def request_workout(
    adapter: LLMAdapter,
    job: LLMJob,
    *,
    system: str,
    turns: Sequence[ChatTurn],
) -> WorkoutAccepted | WorkoutRejected:
    """
    Request one workout. On an invalid reply, ask once more with the errors.

    Used as `request_workout(adapter, LLMJob.PLAN, system=text, turns=turns)`
    """
    first = adapter.complete(job, system=system, turns=turns)
    try:
        return WorkoutAccepted(workout_from_text(first.text), 1, (first,))
    except WorkoutValidationError as error:
        _log_failed_attempt(1, error)
        retry = _retry_turn(error.errors)
    echoed = first.text if first.text.strip() else _EMPTY_REPLY_PLACEHOLDER
    retry_turns = [*turns, ChatTurn("assistant", echoed), retry]
    second = adapter.complete(job, system=system, turns=retry_turns)
    return _second_outcome(first, second)


def _second_outcome(
    first: LLMReply, second: LLMReply
) -> WorkoutAccepted | WorkoutRejected:
    replies = (first, second)
    try:
        return WorkoutAccepted(workout_from_text(second.text), 2, replies)
    except WorkoutValidationError as error:
        _log_failed_attempt(2, error)
        message = _rejection_message(error.errors)
        return WorkoutRejected(message, error.errors, 2, replies)
