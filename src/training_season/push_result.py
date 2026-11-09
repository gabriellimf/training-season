"""Outcome of one week push and its Portuguese chat text."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date


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

    @property
    def chat_message(self) -> str:
        """
        Portuguese chat text that names the days that landed.

        Used as `sys.stdout.write(result.chat_message)`
        """
        week = _day_text(self.week_start)
        if self.failed is not None:
            return _partial_message(week, self, self.failed)
        if not self.landed:
            return (
                f"Semana de {week} sem treinos. Os treinos que este app enviou "
                "para a semana foram removidos do Garmin."
            )
        return (
            f"Semana de {week} enviada ao Garmin. Agendados: {_days_text(self.landed)}."
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
