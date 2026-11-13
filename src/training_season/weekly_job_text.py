"""Portuguese chat text of a weekly run that the cycle could not report itself."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from training_season.coach_instruction import CoachInstructionError
from training_season.llm import LLMConfigurationError
from training_season.push_store import NoActiveMacroPlanError
from training_season.timeformat import UTC_TIMESTAMP_FORMAT, TrainingTimezoneError
from training_season.weekly_cycle_text import error_cause

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

_RETRY_HINT = "Rode training-season-weekly para tentar de novo."
_NOT_RUN_HEAD = "Ciclo semanal não rodou:"
_NOT_RUN = "As etapas seguintes não rodaram."

CRASH_TEXT = f"Ciclo semanal parou por um erro inesperado.\n{_NOT_RUN}\n{_RETRY_HINT}"
REFUSED_TEXT = f"{_NOT_RUN_HEAD} outra execução já estava em andamento."
LOCK_FAILED_TEXT = f"{_NOT_RUN_HEAD} a trava do ciclo semanal não pode ser usada."
_NO_MACRO_CAUSE = (
    "não há plano macro ativo. Conclua o onboarding com training-season-onboarding."
)

# Fixed causes, first match wins. Garmin and SQLite errors use the cycle causes.
_PRE_STEP_CAUSES: tuple[tuple[type[Exception], str], ...] = (
    (LLMConfigurationError, "a configuração do provedor do modelo não é válida."),
    (CoachInstructionError, "a instrução do treinador não é válida."),
    (TrainingTimezoneError, "o fuso horário em TZ não é válido."),
    (NoActiveMacroPlanError, _NO_MACRO_CAUSE),
)


def pre_step_text(error: Exception) -> str:
    """
    Say that the cycle did not start, with a fixed cause for the setup error.

    Used as `pre_step_text(TrainingTimezoneError("..."))`
    """
    for kind, cause in _PRE_STEP_CAUSES:
        if isinstance(error, kind):
            return f"{_NOT_RUN_HEAD} {cause}"
    return f"{_NOT_RUN_HEAD} {error_cause(error)}"


def _start_text(started_at: str, zone: ZoneInfo | None) -> str:
    moment = datetime.strptime(started_at, UTC_TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    if zone is None:
        return f"{moment:%d/%m} às {moment:%H:%M} UTC"
    local = moment.astimezone(zone)
    return f"{local:%d/%m} às {local:%H:%M}"


def interrupted_text(started_at: str, zone: ZoneInfo | None) -> str:
    """
    Name the start of a run that stopped before its end, in the training zone.

    With no valid zone the start is shown in UTC.
    Used as `interrupted_text("2026-11-08T20:00:00Z", ZoneInfo("America/Sao_Paulo"))`
    """
    return (
        f"A execução do ciclo semanal iniciada em {_start_text(started_at, zone)} "
        f"foi interrompida antes do fim. {_RETRY_HINT}"
    )
