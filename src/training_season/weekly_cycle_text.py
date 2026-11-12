"""Portuguese chat text of the weekly cycle: the planned week or the failed step."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from enum import IntEnum
from typing import TYPE_CHECKING

from training_season.athlete_summary import AthleteSummaryRejectedError
from training_season.coach_context_store import read_running_metres_between
from training_season.coach_system import StoredProfileError
from training_season.garmin_auth import (
    GarminConfigurationError,
    GarminError,
    GarminLockTimeoutError,
    GarminLoginRefusedError,
    GarminMfaRequiredError,
    GarminRateLimitedError,
    GarminTokenCacheError,
    GarminTokenExpiredError,
)
from training_season.llm import (
    LLMAuthenticationError,
    LLMError,
    LLMRateLimitedError,
    LLMRefusalError,
)
from training_season.week_generation import WEEK_REJECTION_HEADLINE
from training_season.week_volume import week_volume
from training_season.workout_validation import MAX_ERROR_LINES

if TYPE_CHECKING:
    from collections.abc import Sequence

    from training_season.push_week import PushWeek


class CycleStep(IntEnum):
    """Weekly cycle step numbers. Step 4 also checks the workout names (step 5)."""

    PULL = 1
    CONTEXT = 2
    GENERATE = 3
    VALIDATE = 4
    PUSH = 6
    SUMMARY = 7


_STEP_TEXT = {
    CycleStep.PULL: "coleta do Garmin",
    CycleStep.CONTEXT: "contexto do treinador",
    CycleStep.GENERATE: "geração da semana",
    CycleStep.VALIDATE: "validação da semana e dos nomes dos treinos",
    CycleStep.PUSH: "envio ao Garmin",
    CycleStep.SUMMARY: "resumo do atleta",
}
_NOT_RUN = "As etapas seguintes não rodaram."
_WEEKDAY_TEXT = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")

# Fixed causes, first match wins: error text from Garmin or the model never
# reaches the chat.
_ERROR_CAUSES: tuple[tuple[type[Exception], str], ...] = (
    (GarminMfaRequiredError, "o Garmin pede um código MFA para o login."),
    (GarminLockTimeoutError, "outro comando está usando a sessão do Garmin."),
    (GarminRateLimitedError, "o Garmin limitou as requisições. Tente mais tarde."),
    (GarminTokenExpiredError, "o Garmin recusou o login."),
    (GarminLoginRefusedError, "o Garmin recusou o login."),
    (GarminConfigurationError, "a configuração do Garmin não é válida."),
    (GarminTokenCacheError, "o cache do token do Garmin não pode ser usado."),
    (GarminError, "o Garmin não respondeu como esperado."),
    (LLMAuthenticationError, "o provedor do modelo recusou a chave de API."),
    (
        LLMRateLimitedError,
        "o provedor do modelo limitou as requisições. Tente mais tarde.",
    ),
    (LLMRefusalError, "o modelo recusou o pedido."),
    (LLMError, "o modelo não respondeu como esperado."),
    (
        AthleteSummaryRejectedError,
        "o resumo novo saiu fora do formato. O resumo anterior continua.",
    ),
    (StoredProfileError, "o perfil salvo do atleta não é válido."),
    (sqlite3.Error, "o banco de dados falhou na leitura ou na gravação."),
)
STEP_ERRORS = tuple(kind for kind, _ in _ERROR_CAUSES)


def error_cause(error: Exception) -> str:
    """
    Return the fixed Portuguese cause for an error a cycle step catches.

    Used as `error_cause(GarminRateLimitedError("..."))`
    """
    for kind, cause in _ERROR_CAUSES:
        if isinstance(error, kind):
            return cause
    raise TypeError(f"error is {type(error).__name__}, expected one of STEP_ERRORS")


def rejected_week_cause(errors: Sequence[str]) -> str:
    """
    Name the rejected week and count its errors, with no reply text in it.

    Used as `rejected_week_cause(outcome.errors)`
    """
    count = len(errors)
    counted = f"mais de {MAX_ERROR_LINES}" if count > MAX_ERROR_LINES else str(count)
    noun = "erro" if count == 1 else "erros"
    return f"{WEEK_REJECTION_HEADLINE} {counted} {noun} de validação."


def failure_text(step: CycleStep, cause: str) -> str:
    """
    Name the failed step and its cause, and say that no later step ran.

    Used as `failure_text(CycleStep.PUSH, result.chat_message)`
    """
    head = f"Ciclo semanal parou na etapa {step.value} ({_STEP_TEXT[step]})"
    return f"{head}: {cause}\n{_NOT_RUN}"


def planned_week_lines(week: PushWeek) -> tuple[str, ...]:
    """
    Return a heading and one line per workout day: weekday and workout name.

    Used as `lines = planned_week_lines(week)`
    """
    heading = f"Treinos da semana de {week.week_start:%d/%m} a {week.week_end:%d/%m}:"
    days = tuple(
        f"- {_WEEKDAY_TEXT[entry.date.weekday()]} {entry.workout.name}"
        for entry in week.ordered_entries()
    )
    return (heading, *(days or ("- nenhum treino",)))


def week_comparison_lines(
    connection: sqlite3.Connection, week: PushWeek
) -> tuple[str, ...]:
    """
    Return the volume against last week's runs and the easy/hard split.

    Used as `week_comparison_lines(connection, week)`
    """
    last_sunday = week.week_start - timedelta(days=1)
    last_monday = last_sunday - timedelta(days=6)
    run_m = read_running_metres_between(connection, last_monday, last_sunday)
    volume = week_volume(week)
    easy_hm, hard_hm = _hectometres(volume.easy_m), _hectometres(volume.hard_m)
    return (
        _volume_line(easy_hm + hard_hm, _hectometres(run_m)),
        f"Leve: {_km_text(easy_hm)} km · Forte: {_km_text(hard_hm)} km.",
    )


def _hectometres(metres: float) -> int:
    return round(metres / 100)


def _km_text(hectometres: int) -> str:
    return f"{hectometres / 10:.1f}".replace(".", ",")


def _signed_km(delta_hm: int) -> str:
    size = _km_text(abs(delta_hm))
    if delta_hm == 0:
        return f"±{size}"
    return f"{'+' if delta_hm > 0 else '-'}{size}"


def _volume_line(planned_hm: int, run_hm: int) -> str:
    return (
        f"Volume planejado: {_km_text(planned_hm)} km "
        f"({_signed_km(planned_hm - run_hm)} km "
        f"contra {_km_text(run_hm)} km corridos na semana passada)."
    )
