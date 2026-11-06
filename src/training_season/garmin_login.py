"""Garmin login flows: cached token, credential login and MFA resume."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from training_season.garmin_auth_base import (
    GARMIN_EMAIL_ENV,
    GARMIN_PASSWORD_ENV,
    GarminConfigurationError,
    GarminError,
    GarminLoginReason,
    GarminLoginRefusedError,
    GarminMfaCodeRejectedError,
    GarminMfaRequiredError,
    GarminRateLimitedError,
    GarminSession,
    GarminTokenCacheError,
    GarminTokenExpiredError,
    GarminUnavailableError,
)
from training_season.garmin_pending_mfa import (
    invalid_mfa_state,
    remove_mfa_state,
    write_mfa_state,
)
from training_season.garmin_token_cache import (
    TOKEN_CACHE_WRITE_ERROR,
    dump_and_verify_tokens,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from logging import Logger
    from pathlib import Path

    from garminconnect import Garmin
    from garminconnect.client import Client

    from training_season.garmin_auth_base import GarminAuthConfig, GarminFactory

_LOGIN_RATE_LIMITED_ERROR = "Garmin rate-limited or blocked the login. Try again later."
_CREDENTIALS_REQUIRED_ERROR = (
    f"{GARMIN_EMAIL_ENV} and {GARMIN_PASSWORD_ENV} are required for Garmin login"
)
_MFA_ACCEPTED_ERROR = (
    "MFA was accepted, but loading the Garmin profile failed; "
    "the token is saved. Retry later."
)


@dataclass(frozen=True, slots=True)
class _TokenLoginMessages:
    rejected: str
    profile_failed: str
    rate_limited: str
    unreachable: str
    unexpected_mfa: str


_SAVED_TOKEN_MESSAGES = _TokenLoginMessages(
    rejected="Garmin rejected the saved token (expired or revoked)",
    profile_failed="Garmin could not load the saved token or profile",
    rate_limited=_LOGIN_RATE_LIMITED_ERROR,
    unreachable="Garmin is unreachable while loading the saved token",
    unexpected_mfa=(
        "Garmin returned an unexpected MFA request while loading saved tokens"
    ),
)
_CACHED_TOKEN_MESSAGES = _TokenLoginMessages(
    rejected=(
        "Garmin rejected the cached token (expired or revoked), and "
        f"{GARMIN_EMAIL_ENV} and {GARMIN_PASSWORD_ENV} are not set"
    ),
    profile_failed="Garmin profile could not be loaded from cached tokens",
    rate_limited=(
        "Garmin rate-limited or blocked the cached-token login. Try again later."
    ),
    unreachable="Garmin is unreachable while loading cached tokens",
    unexpected_mfa=(
        "Garmin returned an unexpected MFA request while loading cached tokens"
    ),
)
GARMIN_REQUEST_ERRORS = (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
    GarminConnectConnectionError,
)
_TOKEN_LOGIN_ERRORS = (*GARMIN_REQUEST_ERRORS, OSError)


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)


def _is_rejected_token_response(cause: BaseException) -> bool:
    if not isinstance(cause, GarminConnectConnectionError):
        return False
    response = getattr(cause, "response", None)
    return getattr(response, "status_code", None) in (401, 403)


def has_rejected_token_status(error: BaseException) -> bool:
    """True when a 401 or 403 response is anywhere in the exception chain."""
    return any(_is_rejected_token_response(cause) for cause in _exception_chain(error))


def _has_invalid_mfa_state_cause(error: BaseException) -> bool:
    return isinstance(
        error.__cause__,
        (AttributeError, KeyError, TypeError, ValueError),
    )


def _has_credentials(config: GarminAuthConfig) -> bool:
    return config.email is not None and config.password is not None


def _token_login_error(error: Exception, messages: _TokenLoginMessages) -> GarminError:
    if isinstance(error, GarminConnectAuthenticationError):
        if has_rejected_token_status(error):
            return GarminTokenExpiredError(messages.rejected)
        return GarminUnavailableError(messages.profile_failed)
    if isinstance(error, GarminConnectTooManyRequestsError):
        return GarminRateLimitedError(messages.rate_limited)
    if isinstance(error, GarminConnectConnectionError):
        return GarminUnavailableError(messages.unreachable)
    return GarminTokenCacheError(TOKEN_CACHE_WRITE_ERROR)


def _load_saved_token(config: GarminAuthConfig, factory: GarminFactory) -> Garmin:
    garmin = factory(None, None)
    try:
        status, _ = garmin.login(tokenstore=str(config.token_dir))
    except _TOKEN_LOGIN_ERRORS as error:
        raise _token_login_error(error, _SAVED_TOKEN_MESSAGES) from error
    if status is not None:
        raise GarminUnavailableError(_SAVED_TOKEN_MESSAGES.unexpected_mfa)
    return garmin


def _credential_login_status(garmin: Garmin) -> tuple[str | None, object]:
    try:
        login_status = garmin.login()
    except GarminConnectAuthenticationError as error:
        raise GarminLoginRefusedError(
            "Garmin refused the credential login. Check the credentials or account "
            "status."
        ) from error
    except GarminConnectTooManyRequestsError as error:
        raise GarminRateLimitedError(_LOGIN_RATE_LIMITED_ERROR) from error
    except GarminConnectConnectionError as error:
        raise GarminUnavailableError(
            "Garmin is unreachable during credential login"
        ) from error
    return login_status


def run_credential_login(
    config: GarminAuthConfig,
    token_path: Path,
    reason: GarminLoginReason,
    factory: GarminFactory,
) -> None:
    """Log in with credentials and cache the token, or save MFA state and raise."""
    if not _has_credentials(config):
        raise GarminConfigurationError(_CREDENTIALS_REQUIRED_ERROR)

    garmin = factory(config.email, config.password)
    status, client_state = _credential_login_status(garmin)
    if status == "needs_mfa":
        pending = write_mfa_state(config, client_state, reason)
        raise GarminMfaRequiredError(pending.reason, pending.requested_at)
    if status is not None:
        raise GarminUnavailableError("Garmin returned an unexpected login status")

    dump_and_verify_tokens(garmin.client, config, token_path)


def credential_login_session(
    config: GarminAuthConfig,
    token_path: Path,
    reason: GarminLoginReason,
    factory: GarminFactory,
) -> GarminSession:
    """Log in with credentials, then open a session from the new token."""
    run_credential_login(config, token_path, reason, factory)
    return GarminSession(_load_saved_token(config, factory), reason)


def _relogin_after_rejected_token(
    config: GarminAuthConfig,
    token_path: Path,
    factory: GarminFactory,
    logger: Logger,
) -> GarminSession:
    logger.warning(
        "Garmin rejected the cached token (expired or revoked); logging in again"
    )
    return credential_login_session(
        config, token_path, GarminLoginReason.TOKEN_REJECTED, factory
    )


def login_from_cache(
    config: GarminAuthConfig,
    token_path: Path,
    factory: GarminFactory,
    logger: Logger,
) -> GarminSession:
    """Log in with the cached token. Use credentials if Garmin rejects it."""
    try:
        garmin = factory(None, None)
        status, _ = garmin.login(tokenstore=str(config.token_dir))
    except GarminConnectAuthenticationError as error:
        if not has_rejected_token_status(error) or not _has_credentials(config):
            raise _token_login_error(error, _CACHED_TOKEN_MESSAGES) from error
        return _relogin_after_rejected_token(config, token_path, factory, logger)
    except _TOKEN_LOGIN_ERRORS as error:
        raise _token_login_error(error, _CACHED_TOKEN_MESSAGES) from error
    if status is not None:
        raise GarminUnavailableError(_CACHED_TOKEN_MESSAGES.unexpected_mfa)
    return GarminSession(garmin, None)


def _save_resumed_login(
    client: Client, config: GarminAuthConfig, token_path: Path
) -> None:
    dump_and_verify_tokens(client, config, token_path)
    remove_mfa_state(config)


def _mfa_resume_error(
    garmin: Garmin, config: GarminAuthConfig, token_path: Path, error: Exception
) -> GarminError:
    if isinstance(error, GarminConnectTooManyRequestsError):
        return GarminRateLimitedError(
            "Garmin rate-limited the MFA attempt. Try again later."
        )
    if garmin.client.is_authenticated:
        _save_resumed_login(garmin.client, config, token_path)
        return GarminUnavailableError(_MFA_ACCEPTED_ERROR)
    if isinstance(error, GarminConnectConnectionError):
        return GarminUnavailableError("Garmin is unreachable while resuming MFA")
    if _has_invalid_mfa_state_cause(error):
        return invalid_mfa_state(config)
    return GarminMfaCodeRejectedError(
        "Garmin rejected the MFA code. Try again or request a new code."
    )


def resume_mfa_login(
    config: GarminAuthConfig,
    token_path: Path,
    client_state: dict[str, Any],
    code: str,
    factory: GarminFactory,
) -> None:
    """Send the MFA code, then cache the token and remove the MFA state."""
    garmin = factory(None, None)
    try:
        garmin.resume_login(client_state, code)
    except GARMIN_REQUEST_ERRORS as error:
        raise _mfa_resume_error(garmin, config, token_path, error) from error
    _save_resumed_login(garmin.client, config, token_path)
