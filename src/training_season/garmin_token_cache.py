"""Garmin token directory, token cache file and the cross-process lock."""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from garminconnect.client import token_file_path
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

from training_season.garmin_auth_base import (
    GARMIN_TOKEN_DIR_ENV,
    GarminAuthConfig,
    GarminConfigurationError,
    GarminLockTimeoutError,
    GarminLoginReason,
    GarminTokenCacheError,
    no_follow_flags,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from garminconnect.client import Client

LOCK_POLL_INTERVAL_SECONDS = 0.05

_LOCK_FILENAME = "garmin.lock"
_NOT_A_DIRECTORY_ERROR = f"{GARMIN_TOKEN_DIR_ENV} must name a directory"
_TOKEN_CACHE_READ_ERROR = "Garmin token cache cannot be read"  # noqa: S105
TOKEN_CACHE_WRITE_ERROR = "Garmin token cache cannot be written"  # noqa: S105


def validate_token_dir(path: Path) -> None:
    """Raise ``GarminConfigurationError`` unless the path can hold the token dir."""
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
        raise GarminConfigurationError(_NOT_A_DIRECTORY_ERROR)


def token_cache_path(config: GarminAuthConfig) -> Path:
    """Return the token file path inside the configured token directory."""
    validate_token_dir(config.token_dir)
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
    path = token_cache_path(config)
    try:
        config.token_dir.mkdir(mode=0o700, exist_ok=True)
        if not config.token_dir.is_dir():
            raise GarminConfigurationError(_NOT_A_DIRECTORY_ERROR)
        config.token_dir.chmod(0o700)
    except OSError as error:
        raise GarminTokenCacheError(
            "Garmin token directory cannot be created or secured"
        ) from error
    return path


@contextmanager
def locked_token_dir(
    config: GarminAuthConfig,
    lock_timeout: float,
) -> Iterator[Path]:
    """Yield the token file path while holding the cross-process Garmin lock."""
    token_path = _ensure_token_dir(config)
    if not math.isfinite(lock_timeout) or lock_timeout < 0:
        raise GarminConfigurationError("lock_timeout must be finite and non-negative")

    descriptor = _open_lock_file(config.token_dir / _LOCK_FILENAME)
    locked = False
    try:
        _wait_for_lock(descriptor, lock_timeout)
        locked = True
        yield token_path
    finally:
        _release_lock(descriptor, locked=locked)


def _open_lock_file(lock_path: Path) -> int:
    try:
        descriptor = os.open(lock_path, no_follow_flags(os.O_CREAT | os.O_RDWR), 0o600)
    except OSError as error:
        raise GarminTokenCacheError("Garmin lock file cannot be opened") from error

    try:
        os.fchmod(descriptor, 0o600)
    except OSError as error:
        os.close(descriptor)
        raise GarminTokenCacheError("Garmin lock file cannot be secured") from error
    return descriptor


def _wait_for_lock(descriptor: int, lock_timeout: float) -> None:
    deadline = time.monotonic() + lock_timeout
    locked = False
    while not locked:
        locked = _try_lock(descriptor, deadline)


def _try_lock(descriptor: int, deadline: float) -> bool:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        _sleep_until_retry(deadline, error)
        return False
    return True


def _sleep_until_retry(deadline: float, error: BlockingIOError) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GarminLockTimeoutError(
            "Another Garmin operation is running; try again"
        ) from error
    time.sleep(min(LOCK_POLL_INTERVAL_SECONDS, remaining))


def _release_lock(descriptor: int, *, locked: bool) -> None:
    try:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as error:
        raise GarminTokenCacheError("Garmin lock file could not be released") from error
    finally:
        os.close(descriptor)


def token_cache_login_reason(token_path: Path) -> GarminLoginReason | None:
    """Return why the token cache needs a credential login, or None if usable."""
    try:
        descriptor = os.open(token_path, no_follow_flags(os.O_RDONLY))
    except FileNotFoundError:
        return GarminLoginReason.NO_TOKEN
    except OSError as error:
        raise GarminTokenCacheError(_TOKEN_CACHE_READ_ERROR) from error

    token_json = _token_json(descriptor)
    if not isinstance(token_json, dict):
        return GarminLoginReason.TOKEN_UNREADABLE
    token = token_json.get("di_token")
    if not isinstance(token, str) or not token:
        return GarminLoginReason.TOKEN_UNREADABLE
    return None


def _token_json(descriptor: int) -> object:
    try:
        with os.fdopen(descriptor, encoding="utf-8") as token_file:
            return json.load(token_file)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    except OSError as error:
        raise GarminTokenCacheError(_TOKEN_CACHE_READ_ERROR) from error


def dump_and_verify_tokens(
    client: Client,
    config: GarminAuthConfig,
    token_path: Path,
) -> None:
    """Write the client tokens to the cache and check that they read back."""
    try:
        client.dump(str(config.token_dir))
    except (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        OSError,
        ValueError,
    ) as error:
        raise GarminTokenCacheError(TOKEN_CACHE_WRITE_ERROR) from error

    if token_cache_login_reason(token_path) is not None:
        raise GarminTokenCacheError("Garmin returned a session that cannot be cached")
