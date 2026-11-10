"""Ask the coach model for a workout, with one retry on a validation failure."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.validated_reply import Rejected, request_with_one_retry
from training_season.workout_validation import workout_from_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from training_season.llm import ChatTurn, LLMAdapter, LLMJob, LLMReply
    from training_season.workout_schema import Workout

JSON_ONLY_HEADER = """\
Formato da resposta

Responda apenas com um objeto JSON, sem texto antes ou depois."""

WORKOUT_FIELD_RULES = """\
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

WORKOUT_OUTPUT_CONTRACT = f"{JSON_ONLY_HEADER}\n{WORKOUT_FIELD_RULES}"

_REJECTION_HEADLINE = (
    "O treinador devolveu um treino inválido duas vezes. Nada foi enviado ao Garmin."
)


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
    outcome = request_with_one_retry(
        adapter,
        job,
        system=system,
        turns=turns,
        parse=workout_from_text,
        rejection_headline=_REJECTION_HEADLINE,
    )
    if isinstance(outcome, Rejected):
        return WorkoutRejected(
            outcome.chat_message, outcome.errors, outcome.attempts, outcome.replies
        )
    return WorkoutAccepted(outcome.value, outcome.attempts, outcome.replies)
