"""
File-backed Garmin authentication and restart-safe MFA.

Open one session per Garmin step. Do not hold it across an LLM call. Tell the
chat when ``login_reason`` is ``TOKEN_REJECTED`` or ``TOKEN_UNREADABLE``.
Never log or display credentials, tokens, MFA codes, or MFA client state.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import secrets
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from garminconnect import Garmin
from garminconnect.client import Client, token_file_path
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from training_season.timeformat import utc_now

GARMIN_TOKEN_DIR_ENV = "GARMIN_TOKEN_DIR"  # noqa: S105
GARMIN_EMAIL_ENV = "GARMIN_EMAIL"
GARMIN_PASSWORD_ENV = "GARMIN_PASSWORD"  # noqa: S105
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_INTERVAL_SECONDS = 0.05

_LEGACY_TOKEN_ENV = "GARMINTOKENS"  # noqa: S105
_MFA_STATE_FILENAME = "garmin_mfa_state.json"
_LOCK_FILENAME = "garmin.lock"
_MFA_STATE_VERSION = 1
_LOGGER = logging.getLogger(__name__)

GarminFactory = Callable[[str | None, str | None], Garmin]


class GarminLoginReason(StrEnum):
    """Reason the current Garmin session used a credential login."""

    NO_TOKEN = "no_token"  # noqa: S105
    TOKEN_REJECTED = "token_rejected"  # noqa: S105
    TOKEN_UNREADABLE = "token_unreadable"  # noqa: S105
    MANUAL = "manual"


class GarminAuthState(StrEnum):
    """Local state of Garmin authentication."""

    NO_TOKEN = "no_token"  # noqa: S105
    TOKEN_CACHED = "token_cached"  # noqa: S105
    MFA_PENDING = "mfa_pending"


@dataclass(frozen=True)
class GarminAuthConfig:
    """Validated paths and optional Garmin credentials."""

    token_dir: Path
    email: str | None = field(repr=False)
    password: str | None = field(repr=False)


@dataclass(frozen=True)
class GarminAuthStatus:
    """Garmin authentication state without contacting Garmin."""

    state: GarminAuthState
    mfa_requested_at: str | None


@dataclass(frozen=True)
class GarminSession:
    """Authenticated Garmin client and the reason for any credential login."""

    garmin: Garmin
    login_reason: GarminLoginReason | None


@dataclass(frozen=True)
class _PendingMfaState:
    reason: GarminLoginReason
    requested_at: str
    client_state: dict[str, Any]


class GarminError(Exception):
    """Base class for Garmin authentication errors."""


class GarminConfigurationError(GarminError):
    """Raised when Garmin environment configuration is invalid."""


class GarminTokenCacheError(GarminError):
    """Raised when the Garmin token cache cannot be used safely."""


class GarminLockTimeoutError(GarminError):
    """Raised when another process holds the Garmin lock."""


class GarminLoginRefusedError(GarminError):
    """Raised when Garmin refuses a credential login."""


class GarminTokenExpiredError(GarminError):
    """Raised when Garmin rejects a cached token."""


class GarminMfaRequiredError(GarminError):
    """Raised when Garmin is waiting for an MFA code."""

    def __init__(
        self,
        reason: GarminLoginReason,
        requested_at: str,
    ) -> None:
        self.reason = reason
        self.requested_at = requested_at
        if reason is GarminLoginReason.TOKEN_REJECTED:
            message = (
                "Garmin rejected the cached token (expired or revoked) and requires "
                "MFA. Enter the code in the chat to finish login."
            )
        elif reason is GarminLoginReason.TOKEN_UNREADABLE:
            message = (
                "Garmin token cache is unreadable. Garmin requires MFA. Enter the "
                "code in the chat to finish login."
            )
        else:
            message = "Garmin requires MFA. Enter the code in the chat to finish login."
        super().__init__(message)


class GarminMfaNotPendingError(GarminError):
    """Raised when no MFA login is waiting for a code."""


class GarminMfaCodeRejectedError(GarminError):
    """Raised when Garmin rejects an MFA code."""


class GarminMfaStateInvalidError(GarminError):
    """Raised when stored MFA state cannot be resumed."""


class GarminRateLimitedError(GarminError):
    """Raised when Garmin rate-limits or blocks an operation."""


class GarminUnavailableError(GarminError):
    """Raised when Garmin cannot complete an operation."""


def default_garmin_factory(
    email: str | None,
    password: str | None,
) -> Garmin:
    """Create a Garmin client that returns MFA state instead of prompting."""
    return Garmin(email=email, password=password, return_on_mfa=True)


def _validate_token_dir(path: Path) -> None:
    if not path.is_absolute() or path == path.parent:
        raise GarminConfigurationError(
            f"{GARMIN_TOKEN_DIR_ENV} must be an absolute path to a directory"
        )

    parent = path.parent
    if not parent.exists():
        raise GarminConfigurationError(
            f"parent directory for {GARMIN_TOKEN_DIR_ENV} does not exist"
        )
    if not parent.is_dir():
        raise GarminConfigurationError(
            f"parent path for {GARMIN_TOKEN_DIR_ENV} is not a directory"
        )
    if path.exists() and not path.is_dir():
        raise GarminConfigurationError(f"{GARMIN_TOKEN_DIR_ENV} must name a directory")


def garmin_auth_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> GarminAuthConfig:
    """Read Garmin credentials and the token directory from the environment."""
    environment = os.environ if environ is None else environ
    if _LEGACY_TOKEN_ENV in environment:
        raise GarminConfigurationError(
            f"{_LEGACY_TOKEN_ENV} is not supported; use {GARMIN_TOKEN_DIR_ENV}"
        )

    raw_token_dir = environment.get(GARMIN_TOKEN_DIR_ENV)
    if raw_token_dir is None or not raw_token_dir.strip():
        raise GarminConfigurationError(
            f"{GARMIN_TOKEN_DIR_ENV} must be an absolute path to the Garmin token "
            "directory"
        )

    token_dir = Path(raw_token_dir)
    _validate_token_dir(token_dir)

    email = environment.get(GARMIN_EMAIL_ENV)
    if email is not None and not email.strip():
        email = None

    password = environment.get(GARMIN_PASSWORD_ENV)
    if password is not None and not password.strip():
        password = None

    return GarminAuthConfig(token_dir, email, password)


def _token_file(config: GarminAuthConfig) -> Path:
    _validate_token_dir(config.token_dir)
    try:
        path = token_file_path(str(config.token_dir))
    except (OSError, ValueError) as error:
        raise GarminTokenCacheError(
            "Garmin token directory is not safe to use"
        ) from error
    if path == config.token_dir:
        raise GarminConfigurationError(
            f"{GARMIN_TOKEN_DIR_ENV} must name a directory, not a JSON file"
        )
    return path


def _ensure_token_dir(config: GarminAuthConfig) -> Path:
    path = _token_file(config)
    try:
        config.token_dir.mkdir(mode=0o700, exist_ok=True)
        if not config.token_dir.is_dir():
            raise GarminConfigurationError(
                f"{GARMIN_TOKEN_DIR_ENV} must name a directory"
            )
        config.token_dir.chmod(0o700)
    except OSError as error:
        raise GarminTokenCacheError(
            "Garmin token directory cannot be created or secured"
        ) from error
    return path


@contextmanager
def _locked_token_dir(
    config: GarminAuthConfig,
    lock_timeout: float,
) -> Iterator[Path]:
    token_path = _ensure_token_dir(config)
    if not math.isfinite(lock_timeout) or lock_timeout < 0:
        raise GarminConfigurationError("lock_timeout must be finite and non-negative")

    lock_path = config.token_dir / _LOCK_FILENAME
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise GarminTokenCacheError("Garmin lock file cannot be opened") from error

    try:
        os.fchmod(descriptor, 0o600)
    except OSError as error:
        os.close(descriptor)
        raise GarminTokenCacheError("Garmin lock file cannot be secured") from error

    locked = False
    try:
        deadline = time.monotonic() + lock_timeout
        while not locked:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GarminLockTimeoutError(
                        "Another Garmin operation is running; try again"
                    ) from error
                time.sleep(min(LOCK_POLL_INTERVAL_SECONDS, remaining))

        yield token_path
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as error:
            raise GarminTokenCacheError(
                "Garmin lock file could not be released"
            ) from error
        finally:
            os.close(descriptor)


def _token_cache_login_reason(token_path: Path) -> GarminLoginReason | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(token_path, flags)
    except FileNotFoundError:
        return GarminLoginReason.NO_TOKEN
    except OSError as error:
        raise GarminTokenCacheError("Garmin token cache cannot be read") from error

    try:
        with os.fdopen(descriptor, encoding="utf-8") as token_file:
            data = json.load(token_file)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return GarminLoginReason.TOKEN_UNREADABLE
    except OSError as error:
        raise GarminTokenCacheError("Garmin token cache cannot be read") from error

    if not isinstance(data, dict):
        return GarminLoginReason.TOKEN_UNREADABLE
    token = data.get("di_token")
    if not isinstance(token, str) or not token:
        return GarminLoginReason.TOKEN_UNREADABLE
    return None


def _state_path(config: GarminAuthConfig) -> Path:
    return config.token_dir / _MFA_STATE_FILENAME


def _remove_mfa_state(config: GarminAuthConfig) -> None:
    try:
        _state_path(config).unlink(missing_ok=True)
    except OSError as error:
        raise GarminTokenCacheError("Garmin MFA state could not be removed") from error


def _invalid_mfa_state(config: GarminAuthConfig) -> GarminMfaStateInvalidError:
    _remove_mfa_state(config)
    return GarminMfaStateInvalidError(
        "Stored Garmin MFA state is invalid. Start login again."
    )


def _read_mfa_state(config: GarminAuthConfig) -> _PendingMfaState | None:
    path = _state_path(config)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _invalid_mfa_state(config) from error

    try:
        with os.fdopen(descriptor, encoding="utf-8") as state_file:
            data = json.load(state_file)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        raise _invalid_mfa_state(config) from error

    if not isinstance(data, dict) or type(data.get("version")) is not int:
        raise _invalid_mfa_state(config)
    if data["version"] != _MFA_STATE_VERSION:
        raise _invalid_mfa_state(config)

    requested_at = data.get("created_at")
    reason_value = data.get("reason")
    client_state = data.get("client_state")
    if (
        not isinstance(requested_at, str)
        or not requested_at
        or not isinstance(reason_value, str)
        or not isinstance(client_state, dict)
        or not client_state
    ):
        raise _invalid_mfa_state(config)

    try:
        reason = GarminLoginReason(reason_value)
    except ValueError as error:
        raise _invalid_mfa_state(config) from error

    return _PendingMfaState(
        reason=reason,
        requested_at=requested_at,
        client_state=client_state,
    )


def _write_mfa_state(
    config: GarminAuthConfig,
    client_state: Any,
    reason: GarminLoginReason,
) -> _PendingMfaState:
    if not isinstance(client_state, dict) or not client_state:
        raise GarminMfaStateInvalidError(
            "Garmin returned invalid MFA state. Start login again."
        )

    requested_at = utc_now()
    payload = {
        "version": _MFA_STATE_VERSION,
        "created_at": requested_at,
        "reason": reason.value,
        "client_state": client_state,
    }
    try:
        serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as error:
        raise GarminMfaStateInvalidError(
            "Garmin returned invalid MFA state. Start login again."
        ) from error

    path = _state_path(config)
    temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    descriptor = -1
    try:
        descriptor = os.open(temporary_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        state_file_handle = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = -1
        with state_file_handle as state_file:
            state_file.write(serialized)
            state_file.flush()
            os.fsync(state_file.fileno())
        temporary_path.replace(path)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise GarminTokenCacheError(
                "Garmin MFA state could not be written or cleaned up"
            ) from cleanup_error
        raise GarminTokenCacheError("Garmin MFA state could not be written") from error

    return _PendingMfaState(reason, requested_at, client_state)


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


def _has_rejected_token_status(error: BaseException) -> bool:
    for cause in _exception_chain(error):
        if isinstance(cause, GarminConnectConnectionError):
            response = getattr(cause, "response", None)
            if getattr(response, "status_code", None) in (401, 403):
                return True
    return False


def _has_invalid_mfa_state_cause(error: BaseException) -> bool:
    return isinstance(
        error.__cause__,
        (AttributeError, KeyError, TypeError, ValueError),
    )


def _has_credentials(config: GarminAuthConfig) -> bool:
    return config.email is not None and config.password is not None


def _raise_token_cache_error(error: BaseException) -> None:
    raise GarminTokenCacheError("Garmin token cache cannot be written") from error


def _dump_and_verify_tokens(
    client: Client,
    config: GarminAuthConfig,
    token_path: Path,
) -> None:
    try:
        client.dump(str(config.token_dir))
    except (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        OSError,
        ValueError,
    ) as error:
        _raise_token_cache_error(error)

    if _token_cache_login_reason(token_path) is not None:
        raise GarminTokenCacheError("Garmin returned a session that cannot be cached")


def _load_saved_token(
    config: GarminAuthConfig,
    factory: GarminFactory,
) -> Garmin:
    garmin = factory(None, None)
    try:
        status, _ = garmin.login(tokenstore=str(config.token_dir))
    except GarminConnectAuthenticationError as error:
        if _has_rejected_token_status(error):
            raise GarminTokenExpiredError(
                "Garmin rejected the saved token (expired or revoked)"
            ) from error
        raise GarminUnavailableError(
            "Garmin could not load the saved token or profile"
        ) from error
    except GarminConnectTooManyRequestsError as error:
        raise GarminRateLimitedError(
            "Garmin rate-limited or blocked the login. Try again later."
        ) from error
    except GarminConnectConnectionError as error:
        raise GarminUnavailableError(
            "Garmin is unreachable while loading the saved token"
        ) from error
    except OSError as error:
        _raise_token_cache_error(error)

    if status is not None:
        raise GarminUnavailableError(
            "Garmin returned an unexpected MFA request while loading saved tokens"
        )
    return garmin


def _run_credential_login(
    config: GarminAuthConfig,
    token_path: Path,
    reason: GarminLoginReason,
    factory: GarminFactory,
) -> None:
    if not _has_credentials(config):
        raise GarminConfigurationError(
            f"{GARMIN_EMAIL_ENV} and {GARMIN_PASSWORD_ENV} are required for "
            "Garmin login"
        )

    garmin = factory(config.email, config.password)
    try:
        status, client_state = garmin.login()
    except GarminConnectAuthenticationError as error:
        raise GarminLoginRefusedError(
            "Garmin refused the credential login. Check the credentials or account "
            "status."
        ) from error
    except GarminConnectTooManyRequestsError as error:
        raise GarminRateLimitedError(
            "Garmin rate-limited or blocked the login. Try again later."
        ) from error
    except GarminConnectConnectionError as error:
        raise GarminUnavailableError(
            "Garmin is unreachable during credential login"
        ) from error

    if status == "needs_mfa":
        pending = _write_mfa_state(config, client_state, reason)
        raise GarminMfaRequiredError(pending.reason, pending.requested_at)
    if status is not None:
        raise GarminUnavailableError("Garmin returned an unexpected login status")

    _dump_and_verify_tokens(garmin.client, config, token_path)


def _login_from_cache(
    config: GarminAuthConfig,
    token_path: Path,
    factory: GarminFactory,
) -> GarminSession:
    try:
        garmin = factory(None, None)
        status, _ = garmin.login(tokenstore=str(config.token_dir))
    except GarminConnectAuthenticationError as error:
        if _has_rejected_token_status(error):
            if not _has_credentials(config):
                raise GarminTokenExpiredError(
                    "Garmin rejected the cached token (expired or revoked), and "
                    f"{GARMIN_EMAIL_ENV} and {GARMIN_PASSWORD_ENV} are not set"
                ) from error
            _LOGGER.warning(
                "Garmin rejected the cached token (expired or revoked); "
                "logging in again"
            )
            _run_credential_login(
                config,
                token_path,
                GarminLoginReason.TOKEN_REJECTED,
                factory,
            )
            return GarminSession(
                _load_saved_token(config, factory),
                GarminLoginReason.TOKEN_REJECTED,
            )
        raise GarminUnavailableError(
            "Garmin profile could not be loaded from cached tokens"
        ) from error
    except GarminConnectTooManyRequestsError as error:
        raise GarminRateLimitedError(
            "Garmin rate-limited or blocked the cached-token login. Try again later."
        ) from error
    except GarminConnectConnectionError as error:
        raise GarminUnavailableError(
            "Garmin is unreachable while loading cached tokens"
        ) from error
    except OSError as error:
        _raise_token_cache_error(error)

    if status is not None:
        raise GarminUnavailableError(
            "Garmin returned an unexpected MFA request while loading cached tokens"
        )
    return GarminSession(garmin, None)


@contextmanager
def garmin_session(
    config: GarminAuthConfig,
    *,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    factory: GarminFactory = default_garmin_factory,
) -> Iterator[GarminSession]:
    """Yield an authenticated client while holding the cross-process lock."""
    with _locked_token_dir(config, lock_timeout) as token_path:
        pending = _read_mfa_state(config)
        if pending is not None:
            raise GarminMfaRequiredError(pending.reason, pending.requested_at)

        login_reason = _token_cache_login_reason(token_path)
        if login_reason is None:
            session = _login_from_cache(config, token_path, factory)
        else:
            if login_reason is GarminLoginReason.TOKEN_UNREADABLE:
                _LOGGER.warning("Garmin token cache is unreadable; logging in again")
            _run_credential_login(
                config,
                token_path,
                login_reason,
                factory,
            )
            session = GarminSession(
                _load_saved_token(config, factory),
                login_reason,
            )

        try:
            yield session
        except GarminConnectAuthenticationError as error:
            raise GarminTokenExpiredError(
                "Garmin rejected the authenticated session; log in again"
            ) from error
        except GarminConnectTooManyRequestsError as error:
            raise GarminRateLimitedError(
                "Garmin rate-limited the authenticated request. Try again later."
            ) from error
        except GarminConnectConnectionError as error:
            if _has_rejected_token_status(error):
                raise GarminTokenExpiredError(
                    "Garmin rejected the authenticated session; log in again"
                ) from error
            raise GarminUnavailableError(
                "Garmin is unreachable during the authenticated request"
            ) from error


def start_login(
    config: GarminAuthConfig,
    *,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    factory: GarminFactory = default_garmin_factory,
) -> None:
    """Start a credential login and replace any pending MFA state."""
    with _locked_token_dir(config, lock_timeout) as token_path:
        _remove_mfa_state(config)
        _run_credential_login(
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

    with _locked_token_dir(config, lock_timeout) as token_path:
        pending = _read_mfa_state(config)
        if pending is None:
            raise GarminMfaNotPendingError("No Garmin MFA login is pending")

        garmin = factory(None, None)
        try:
            garmin.resume_login(pending.client_state, normalized_code)
        except GarminConnectAuthenticationError as error:
            if garmin.client.is_authenticated:
                _dump_and_verify_tokens(garmin.client, config, token_path)
                _remove_mfa_state(config)
                raise GarminUnavailableError(
                    "MFA was accepted, but loading the Garmin profile failed; "
                    "the token is saved. Retry later."
                ) from error
            if _has_invalid_mfa_state_cause(error):
                raise _invalid_mfa_state(config) from error
            raise GarminMfaCodeRejectedError(
                "Garmin rejected the MFA code. Try again or request a new code."
            ) from error
        except GarminConnectTooManyRequestsError as error:
            raise GarminRateLimitedError(
                "Garmin rate-limited the MFA attempt. Try again later."
            ) from error
        except GarminConnectConnectionError as error:
            if garmin.client.is_authenticated:
                _dump_and_verify_tokens(garmin.client, config, token_path)
                _remove_mfa_state(config)
                raise GarminUnavailableError(
                    "MFA was accepted, but loading the Garmin profile failed; "
                    "the token is saved. Retry later."
                ) from error
            raise GarminUnavailableError(
                "Garmin is unreachable while resuming MFA"
            ) from error

        _dump_and_verify_tokens(garmin.client, config, token_path)
        _remove_mfa_state(config)


def import_token(
    config: GarminAuthConfig,
    token_json: str,
    *,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
) -> None:
    """Validate and save token JSON without contacting Garmin."""
    with _locked_token_dir(config, lock_timeout) as token_path:
        client = Client()
        try:
            client.loads(token_json)
        except (
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
        ) as error:
            raise GarminTokenCacheError("Garmin token JSON is invalid") from error
        _dump_and_verify_tokens(client, config, token_path)


def auth_status(config: GarminAuthConfig) -> GarminAuthStatus:
    """Return local Garmin auth state without network access or a lock."""
    token_path = _token_file(config)
    pending = _read_mfa_state(config)
    if pending is not None:
        return GarminAuthStatus(GarminAuthState.MFA_PENDING, pending.requested_at)
    if _token_cache_login_reason(token_path) is None:
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
