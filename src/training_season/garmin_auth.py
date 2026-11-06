"""
File-backed Garmin authentication and restart-safe MFA.

Open one session per Garmin step. Do not hold it across an LLM call. Tell the
chat when ``login_reason`` is ``TOKEN_REJECTED`` or ``TOKEN_UNREADABLE``.
Never log or display credentials, tokens, MFA codes, or MFA client state.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from garminconnect import Garmin
from garminconnect.client import Client
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from training_season.garmin_auth_base import (
    GARMIN_EMAIL_ENV,
    GARMIN_PASSWORD_ENV,
    GARMIN_TOKEN_DIR_ENV,
    GarminAuthConfig,
    GarminConfigurationError,
    GarminError,
    GarminFactory,
    GarminLockTimeoutError,
    GarminLoginReason,
    GarminLoginRefusedError,
    GarminMfaCodeRejectedError,
    GarminMfaNotPendingError,
    GarminMfaRequiredError,
    GarminMfaStateInvalidError,
    GarminRateLimitedError,
    GarminSession,
    GarminTokenCacheError,
    GarminTokenExpiredError,
    GarminUnavailableError,
)
from training_season.garmin_login import (
    GARMIN_REQUEST_ERRORS,
    credential_login_session,
    has_rejected_token_status,
    login_from_cache,
    resume_mfa_login,
    run_credential_login,
)
from training_season.garmin_pending_mfa import read_mfa_state, remove_mfa_state
from training_season.garmin_token_cache import (
    LOCK_POLL_INTERVAL_SECONDS,
    dump_and_verify_tokens,
    locked_token_dir,
    token_cache_login_reason,
    token_cache_path,
    validate_token_dir,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

__all__ = [
    "GARMIN_EMAIL_ENV",
    "GARMIN_PASSWORD_ENV",
    "GARMIN_TOKEN_DIR_ENV",
    "LOCK_POLL_INTERVAL_SECONDS",
    "LOCK_TIMEOUT_SECONDS",
    "GarminAuthConfig",
    "GarminAuthState",
    "GarminAuthStatus",
    "GarminConfigurationError",
    "GarminError",
    "GarminFactory",
    "GarminLockTimeoutError",
    "GarminLoginReason",
    "GarminLoginRefusedError",
    "GarminMfaCodeRejectedError",
    "GarminMfaNotPendingError",
    "GarminMfaRequiredError",
    "GarminMfaStateInvalidError",
    "GarminRateLimitedError",
    "GarminSession",
    "GarminTokenCacheError",
    "GarminTokenExpiredError",
    "GarminUnavailableError",
    "auth_status",
    "default_garmin_factory",
    "garmin_auth_config_from_environment",
    "garmin_session",
    "import_token",
    "main",
    "start_login",
    "submit_mfa_code",
]

LOCK_TIMEOUT_SECONDS = 10.0

_LEGACY_TOKEN_ENV = "GARMINTOKENS"  # noqa: S105
_LOGGER = logging.getLogger(__name__)
_SESSION_REJECTED_ERROR = "Garmin rejected the authenticated session; log in again"


class GarminAuthState(StrEnum):
    """Local state of Garmin authentication."""

    NO_TOKEN = "no_token"  # noqa: S105
    TOKEN_CACHED = "token_cached"  # noqa: S105
    MFA_PENDING = "mfa_pending"


@dataclass(frozen=True)
class GarminAuthStatus:
    """Garmin authentication state without contacting Garmin."""

    state: GarminAuthState
    mfa_requested_at: str | None


def default_garmin_factory(
    email: str | None,
    password: str | None,
) -> Garmin:
    """Create a Garmin client that returns MFA state instead of prompting."""
    return Garmin(email=email, password=password, return_on_mfa=True)


def garmin_auth_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> GarminAuthConfig:
    """Read Garmin credentials and the token directory from the environment."""
    environment = os.environ if environ is None else environ
    if _LEGACY_TOKEN_ENV in environment:
        raise GarminConfigurationError(
            f"{_LEGACY_TOKEN_ENV} is not supported; use {GARMIN_TOKEN_DIR_ENV}"
        )

    token_dir = _token_dir_from_environment(environment)
    email = _optional_credential(environment, GARMIN_EMAIL_ENV)
    password = _optional_credential(environment, GARMIN_PASSWORD_ENV)
    return GarminAuthConfig(token_dir, email, password)


def _token_dir_from_environment(environment: Mapping[str, str]) -> Path:
    raw_token_dir = environment.get(GARMIN_TOKEN_DIR_ENV)
    if raw_token_dir is None or not raw_token_dir.strip():
        raise GarminConfigurationError(
            f"{GARMIN_TOKEN_DIR_ENV} must be an absolute path to the Garmin token "
            "directory"
        )

    token_dir = Path(raw_token_dir)
    validate_token_dir(token_dir)
    return token_dir


def _optional_credential(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name)
    if value is not None and not value.strip():
        return None
    return value


def _session_request_error(error: Exception) -> GarminError:
    if isinstance(error, GarminConnectAuthenticationError):
        return GarminTokenExpiredError(_SESSION_REJECTED_ERROR)
    if isinstance(error, GarminConnectTooManyRequestsError):
        return GarminRateLimitedError(
            "Garmin rate-limited the authenticated request. Try again later."
        )
    if has_rejected_token_status(error):
        return GarminTokenExpiredError(_SESSION_REJECTED_ERROR)
    return GarminUnavailableError(
        "Garmin is unreachable during the authenticated request"
    )


def _authenticated_session(
    config: GarminAuthConfig,
    token_path: Path,
    factory: GarminFactory,
) -> GarminSession:
    pending = read_mfa_state(config)
    if pending is not None:
        raise GarminMfaRequiredError(pending.reason, pending.requested_at)

    login_reason = token_cache_login_reason(token_path)
    if login_reason is None:
        return login_from_cache(config, token_path, factory, _LOGGER)
    if login_reason is GarminLoginReason.TOKEN_UNREADABLE:
        _LOGGER.warning("Garmin token cache is unreadable; logging in again")
    return credential_login_session(config, token_path, login_reason, factory)


@contextmanager
def garmin_session(
    config: GarminAuthConfig,
    *,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    factory: GarminFactory = default_garmin_factory,
) -> Iterator[GarminSession]:
    """Yield an authenticated client while holding the cross-process lock."""
    with locked_token_dir(config, lock_timeout) as token_path:
        session = _authenticated_session(config, token_path, factory)
        try:
            yield session
        except GARMIN_REQUEST_ERRORS as error:
            raise _session_request_error(error) from error


def start_login(
    config: GarminAuthConfig,
    *,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    factory: GarminFactory = default_garmin_factory,
) -> None:
    """Start a credential login and replace any pending MFA state."""
    with locked_token_dir(config, lock_timeout) as token_path:
        remove_mfa_state(config)
        run_credential_login(
            config,
            token_path,
            GarminLoginReason.MANUAL,
            factory,
        )


def submit_mfa_code(
    config: GarminAuthConfig,
    code: str,
    *,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    factory: GarminFactory = default_garmin_factory,
) -> None:
    """Resume a pending MFA login and persist its token."""
    normalized_code = code.strip()
    if not normalized_code:
        raise GarminMfaCodeRejectedError("Garmin MFA code cannot be empty")

    with locked_token_dir(config, lock_timeout) as token_path:
        pending = read_mfa_state(config)
        if pending is None:
            raise GarminMfaNotPendingError("No Garmin MFA login is pending")

        resume_mfa_login(
            config, token_path, pending.client_state, normalized_code, factory
        )


def import_token(
    config: GarminAuthConfig,
    token_json: str,
    *,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> None:
    """Validate and save token JSON without contacting Garmin."""
    with locked_token_dir(config, lock_timeout) as token_path:
        client = Client()
        try:
            client.loads(token_json)
        except (
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
        ) as error:
            raise GarminTokenCacheError("Garmin token JSON is invalid") from error
        dump_and_verify_tokens(client, config, token_path)


def auth_status(config: GarminAuthConfig) -> GarminAuthStatus:
    """Return local Garmin auth state without network access or a lock."""
    token_path = token_cache_path(config)
    pending = read_mfa_state(config)
    if pending is not None:
        return GarminAuthStatus(GarminAuthState.MFA_PENDING, pending.requested_at)
    if token_cache_login_reason(token_path) is None:
        return GarminAuthStatus(GarminAuthState.TOKEN_CACHED, None)
    return GarminAuthStatus(GarminAuthState.NO_TOKEN, None)


def _print_usage() -> None:
    sys.stderr.write(
        "Usage: training-season-garmin {login|mfa|import-token|check|status}\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Garmin authentication command-line interface."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--help"], ["-h"]):
        _print_usage()
        return 0
    if len(arguments) != 1 or arguments[0] not in {
        "login",
        "mfa",
        "import-token",
        "check",
        "status",
    }:
        _print_usage()
        return 2

    command = arguments[0]
    try:
        config = garmin_auth_config_from_environment()
        if command == "login":
            start_login(config)
        elif command == "mfa":
            try:
                code = sys.stdin.readline()
            except OSError:
                sys.stderr.write("Could not read command input from stdin\n")
                return 1
            submit_mfa_code(config, code)
        elif command == "import-token":
            try:
                token_json = sys.stdin.read()
            except OSError:
                sys.stderr.write("Could not read command input from stdin\n")
                return 1
            import_token(config, token_json)
        elif command == "check":
            with garmin_session(config):
                sys.stdout.write("Garmin session OK\n")
        else:
            status = auth_status(config)
            if status.mfa_requested_at is None:
                sys.stdout.write(f"{status.state.value}\n")
            else:
                sys.stdout.write(f"{status.state.value} {status.mfa_requested_at}\n")
    except GarminMfaRequiredError as error:
        sys.stderr.write(f"{error}\n")
        return 3
    except GarminConfigurationError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    except GarminError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
