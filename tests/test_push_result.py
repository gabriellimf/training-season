"""Tests for the login note at the start of the push chat message."""

from __future__ import annotations

from datetime import date

import pytest

from training_season.garmin_auth_base import GarminLoginReason
from training_season.push_result import PushResult

_PUSHED = "Semana de 04/01 enviada ao Garmin. Agendados: 04/01."


def _result(login_reason: GarminLoginReason | None) -> PushResult:
    return PushResult(
        date(2027, 1, 4), (date(2027, 1, 4),), None, (), login_reason=login_reason
    )


@pytest.mark.parametrize(
    ("reason", "note"),
    [
        (
            GarminLoginReason.TOKEN_REJECTED,
            (
                "O Garmin recusou o token salvo, e o app entrou de novo com o "
                "e-mail e a senha."
            ),
        ),
        (
            GarminLoginReason.NO_TOKEN,
            "Não havia token do Garmin salvo, e o app entrou com o e-mail e a senha.",
        ),
        (
            GarminLoginReason.TOKEN_UNREADABLE,
            (
                "O token salvo do Garmin não pôde ser lido, e o app entrou de novo "
                "com o e-mail e a senha."
            ),
        ),
    ],
)
def test_credential_login_adds_its_note_line_first(
    reason: GarminLoginReason, note: str
) -> None:
    assert _result(reason).chat_message == f"{note}\n{_PUSHED}"


def test_token_login_adds_no_note() -> None:
    assert _result(None).chat_message == _PUSHED
