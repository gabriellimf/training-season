"""Tests for the Portuguese chat outcome of a Garmin login and an MFA code."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

from garmin_auth_fakes import _begin_mfa
from training_season.garmin_auth import (
    GarminAuthState,
    GarminAuthStatus,
    auth_status,
)
from training_season.garmin_auth_text import (
    GarminAuthOutcome,
    start_login_outcome,
    submit_mfa_outcome,
)
from training_season.garmin_token_cache import locked_token_dir

if TYPE_CHECKING:
    from collections.abc import Callable

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory

_CONNECTED = GarminAuthOutcome(
    "connected", "Login no Garmin concluído. O token foi salvo."
)


def _fixed_status(
    state: GarminAuthState,
) -> Callable[[GarminAuthConfig], GarminAuthStatus]:
    def read_status(_config: GarminAuthConfig) -> GarminAuthStatus:
        return GarminAuthStatus(state, None)

    return read_status


def test_accepted_code_connects_and_saves_the_token(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)

    outcome = submit_mfa_outcome(auth_config, fake_server.valid_code, factory=factory)

    assert outcome == _CONNECTED
    assert auth_status(auth_config).state is GarminAuthState.TOKEN_CACHED


def test_rejected_code_keeps_the_login_pending(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)

    outcome = submit_mfa_outcome(auth_config, "999999", factory=factory)

    assert outcome == GarminAuthOutcome(
        "code_rejected",
        "O Garmin recusou o código. Confira o código e envie de novo.",
    )
    assert auth_status(auth_config).state is GarminAuthState.MFA_PENDING


def test_code_without_a_pending_login_asks_for_a_new_code(
    auth_config: GarminAuthConfig, factory: GarminFactory
) -> None:
    outcome = submit_mfa_outcome(auth_config, "123456", factory=factory)

    assert outcome == GarminAuthOutcome(
        "not_pending",
        "Nenhum login do Garmin espera código agora. Peça um novo código.",
    )


def test_invalid_pending_state_asks_for_a_new_code(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)
    fake_server.issued_state = {"flow": "changed"}

    outcome = submit_mfa_outcome(auth_config, fake_server.valid_code, factory=factory)

    assert outcome == GarminAuthOutcome(
        "state_invalid",
        "O login pendente do Garmin não pode continuar. Peça um novo código.",
    )


def test_unavailable_after_the_token_was_saved_counts_as_connected(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)
    fake_server.profile_error_after_resume = GarminConnectConnectionError("down")

    outcome = submit_mfa_outcome(auth_config, fake_server.valid_code, factory=factory)

    assert outcome == _CONNECTED


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (
            GarminAuthState.MFA_PENDING,
            "O Garmin não respondeu. Envie o código de novo em alguns minutos.",
        ),
        (
            GarminAuthState.NO_TOKEN,
            "O Garmin não respondeu e o login não terminou. Peça um novo código.",
        ),
    ],
)
def test_unavailable_without_a_token_names_the_next_step(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
    state: GarminAuthState,
    expected: str,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)
    fake_server.resume_error = GarminConnectConnectionError("down")

    outcome = submit_mfa_outcome(
        auth_config,
        fake_server.valid_code,
        factory=factory,
        read_status=_fixed_status(state),
    )

    assert outcome == GarminAuthOutcome("unavailable", expected)


def test_other_mfa_error_shows_its_fixed_cause_only(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    _begin_mfa(auth_config, fake_server, factory)

    with locked_token_dir(auth_config, 10.0):
        outcome = submit_mfa_outcome(
            auth_config, fake_server.valid_code, factory=factory, lock_timeout=0.0
        )

    assert outcome == GarminAuthOutcome(
        "failed",
        "Não consegui concluir o login no Garmin: outro comando está usando a "
        "sessão do Garmin. Tente de novo em alguns minutos.",
    )


def test_login_that_needs_a_code_asks_for_it(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    fake_server.credential_outcomes.append("mfa")

    outcome = start_login_outcome(auth_config, factory=factory)

    assert outcome == GarminAuthOutcome(
        "mfa_required",
        "O Garmin enviou um código de verificação. Digite o código abaixo.",
    )
    assert auth_status(auth_config).state is GarminAuthState.MFA_PENDING


def test_login_without_a_code_connects(
    auth_config: GarminAuthConfig, factory: GarminFactory
) -> None:
    assert start_login_outcome(auth_config, factory=factory) == _CONNECTED


def test_refused_login_shows_its_fixed_cause_only(
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    fake_server.credential_outcomes.append(
        GarminConnectAuthenticationError("bad password for runner@example.test")
    )

    outcome = start_login_outcome(auth_config, factory=factory)

    assert outcome == GarminAuthOutcome(
        "failed", "Não consegui entrar no Garmin: o Garmin recusou o login."
    )
