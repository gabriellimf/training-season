"""Outcome of one week push and its Portuguese chat text."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from training_season.garmin_auth_base import GarminLoginReason

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

_LOGIN_NOTES = {
    GarminLoginReason.TOKEN_REJECTED: (
        "O Garmin recusou o token salvo, e o app entrou de novo com o e-mail e a senha."
    ),
    GarminLoginReason.NO_TOKEN: (
        "Não havia token do Garmin salvo, e o app entrou com o e-mail e a senha."
    ),
    GarminLoginReason.TOKEN_UNREADABLE: (
        "O token salvo do Garmin não pôde ser lido, e o app entrou de novo com o "
        "e-mail e a senha."
    ),
}


class PushStep(StrEnum):
    """The Garmin call that failed for a day."""

    REMOVE = "remove"
    UPLOAD = "upload"
    SCHEDULE = "schedule"


_STEP_TEXT = {
    PushStep.REMOVE: "na remoção do treino anterior",
    PushStep.UPLOAD: "no envio do treino",
    PushStep.SCHEDULE: "no agendamento",
}


@dataclass(frozen=True, slots=True)
class PushFailure:
    """The day whose Garmin call failed, with a fixed Portuguese reason."""

    day: date
    step: PushStep
    reason: str


@dataclass(frozen=True, slots=True)
class PushResult:
    """Days that landed, the failed day and the days not tried, in date order."""

    week_start: date
    landed: tuple[date, ...]
    failed: PushFailure | None
    not_attempted: tuple[date, ...]
    login_reason: GarminLoginReason | None = None

    @property
    def chat_message(self) -> str:
        """
        Portuguese chat text that names the days that landed.

        A credential login in the push session adds a note line first.
        Used as `sys.stdout.write(result.chat_message)`
        """
        reason = self.login_reason
        note = None if reason is None else _LOGIN_NOTES.get(reason)
        body = _push_message(self)
        return body if note is None else f"{note}\n{body}"


def _push_message(result: PushResult) -> str:
    week = _day_text(result.week_start)
    if result.failed is not None:
        return _partial_message(week, result, result.failed)
    if not result.landed:
        return (
            f"Semana de {week} sem treinos. Os treinos que este app enviou "
            "para a semana foram removidos do Garmin."
        )
    return (
        f"Semana de {week} enviada ao Garmin. Agendados: {_days_text(result.landed)}."
    )


def _partial_message(week: str, result: PushResult, failed: PushFailure) -> str:
    return (
        f"Envio da semana de {week} incompleto. "
        f"Agendados: {_days_text(result.landed)}. "
        f"Falhou: {_day_text(failed.day)}, {_STEP_TEXT[failed.step]}: "
        f"{failed.reason}. Não tentados: {_days_text(result.not_attempted)}."
    )


def _day_text(day: date) -> str:
    return f"{day:%d/%m}"


def _days_text(days: Sequence[date]) -> str:
    return ", ".join(map(_day_text, days)) or "nenhum"
