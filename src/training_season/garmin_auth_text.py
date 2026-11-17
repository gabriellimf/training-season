"""Portuguese chat outcome of a Garmin login or an MFA code sent from the chat page."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from training_season.garmin_auth import (
    LOCK_TIMEOUT_SECONDS,
    GarminAuthState,
    GarminError,
    GarminLockTimeoutError,
    GarminMfaCodeRejectedError,
    GarminMfaNotPendingError,
    GarminMfaRequiredError,
    GarminMfaStateInvalidError,
    GarminUnavailableError,
    auth_status,
    default_garmin_factory,
    start_login,
    submit_mfa_code,
)
from training_season.weekly_cycle_text import error_cause

if TYPE_CHECKING:
    from collections.abc import Callable

    from training_season.garmin_auth import (
        GarminAuthConfig,
        GarminAuthStatus,
        GarminFactory,
    )

type GarminAuthOutcomeStatus = Literal[
    "connected",
    "mfa_required",
    "code_rejected",
    "not_pending",
    "state_invalid",
    "unavailable",
    "failed",
]
type AuthStatusReader = Callable[[GarminAuthConfig], GarminAuthStatus]

_RETRY_LATER = "Tente de novo em alguns minutos."


@dataclass(frozen=True, slots=True)
class GarminAuthOutcome:
    """The status the page acts on and the Portuguese text it shows."""

    status: GarminAuthOutcomeStatus
    message: str


_CONNECTED = GarminAuthOutcome(
    "connected", "Login no Garmin concluído. O token foi salvo."
)
_MFA_REQUIRED = GarminAuthOutcome(
    "mfa_required",
    "O Garmin enviou um código de verificação. Digite o código abaixo.",
)
_CODE_REJECTED = GarminAuthOutcome(
    "code_rejected", "O Garmin recusou o código. Confira o código e envie de novo."
)
_NOT_PENDING = GarminAuthOutcome(
    "not_pending", "Nenhum login do Garmin espera código agora. Peça um novo código."
)
_STATE_INVALID = GarminAuthOutcome(
    "state_invalid",
    "O login pendente do Garmin não pode continuar. Peça um novo código.",
)
_UNAVAILABLE_PENDING = GarminAuthOutcome(
    "unavailable", "O Garmin não respondeu. Envie o código de novo em alguns minutos."
)
_UNAVAILABLE_NO_TOKEN = GarminAuthOutcome(
    "unavailable",
    "O Garmin não respondeu e o login não terminou. Peça um novo código.",
)


def garmin_failure_cause(error: GarminError) -> str:
    """
    Return the fixed Portuguese cause; a held Garmin lock also says when to retry.

    Used as `f"A semana 1 não foi enviada: {garmin_failure_cause(error)}"`
    """
    cause = error_cause(error)
    if isinstance(error, GarminLockTimeoutError):
        return f"{cause} {_RETRY_LATER}"
    return cause


def _failed(head: str, error: GarminError) -> GarminAuthOutcome:
    return GarminAuthOutcome("failed", f"{head}: {garmin_failure_cause(error)}")


def _unavailable_outcome(
    config: GarminAuthConfig, read_status: AuthStatusReader
) -> GarminAuthOutcome:
    # Garmin can accept the code and save the token before a later call fails.
    try:
        state = read_status(config).state
    except GarminError:
        return _UNAVAILABLE_NO_TOKEN
    if state is GarminAuthState.TOKEN_CACHED:
        return _CONNECTED
    if state is GarminAuthState.MFA_PENDING:
        return _UNAVAILABLE_PENDING
    return _UNAVAILABLE_NO_TOKEN


def submit_mfa_outcome(
    config: GarminAuthConfig,
    code: str,
    *,
    factory: GarminFactory = default_garmin_factory,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    read_status: AuthStatusReader = auth_status,
) -> GarminAuthOutcome:
    """
    Send the MFA code and map the result to a page status and Portuguese text.

    Used as `submit_mfa_outcome(config, "123456", factory=garmin_factory)`
    """
    try:
        submit_mfa_code(config, code, lock_timeout=lock_timeout, factory=factory)
    except GarminMfaCodeRejectedError:
        return _CODE_REJECTED
    except GarminMfaNotPendingError:
        return _NOT_PENDING
    except GarminMfaStateInvalidError:
        return _STATE_INVALID
    except GarminUnavailableError:
        return _unavailable_outcome(config, read_status)
    except GarminError as error:
        return _failed("Não consegui concluir o login no Garmin", error)
    return _CONNECTED


def start_login_outcome(
    config: GarminAuthConfig,
    *,
    factory: GarminFactory = default_garmin_factory,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> GarminAuthOutcome:
    """
    Start a credential login and map the result to a page status and text.

    Used as `start_login_outcome(config, factory=garmin_factory)`
    """
    try:
        start_login(config, lock_timeout=lock_timeout, factory=factory)
    except GarminMfaRequiredError:
        return _MFA_REQUIRED
    except GarminError as error:
        return _failed("Não consegui entrar no Garmin", error)
    return _CONNECTED
