"""Pending Garmin MFA state file: atomic write, checked read and removal."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from training_season.garmin_auth_base import (
    GarminAuthConfig,
    GarminLoginReason,
    GarminMfaStateInvalidError,
    GarminTokenCacheError,
    no_follow_flags,
)
from training_season.timeformat import utc_now

if TYPE_CHECKING:
    from pathlib import Path

_MFA_STATE_FILENAME = "garmin_mfa_state.json"
_MFA_STATE_VERSION = 1
_RETURNED_MFA_STATE_ERROR = "Garmin returned invalid MFA state. Start login again."


@dataclass(frozen=True)
class _PendingMfaState:
    reason: GarminLoginReason
    requested_at: str
    client_state: dict[str, Any] = field(repr=False)


def _mfa_state_path(config: GarminAuthConfig) -> Path:
    return config.token_dir / _MFA_STATE_FILENAME


def remove_mfa_state(config: GarminAuthConfig) -> None:
    """Delete the pending MFA state file if it exists."""
    try:
        _mfa_state_path(config).unlink(missing_ok=True)
    except OSError as error:
        raise GarminTokenCacheError("Garmin MFA state could not be removed") from error


def invalid_mfa_state(config: GarminAuthConfig) -> GarminMfaStateInvalidError:
    """Remove the stored MFA state and return the error that says it is invalid."""
    remove_mfa_state(config)
    return GarminMfaStateInvalidError(
        "Stored Garmin MFA state is invalid. Start login again."
    )


def read_mfa_state(config: GarminAuthConfig) -> _PendingMfaState | None:
    """Return the pending MFA login, or None. Remove the file and raise when invalid."""
    flags = no_follow_flags(os.O_RDONLY)
    try:
        descriptor = os.open(_mfa_state_path(config), flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise invalid_mfa_state(config) from error

    stored = _stored_mfa_json(config, descriptor)
    if not isinstance(stored, dict) or type(stored.get("version")) is not int:
        raise invalid_mfa_state(config)
    if stored["version"] != _MFA_STATE_VERSION:
        raise invalid_mfa_state(config)
    return _pending_mfa_from_json(config, stored)


def _stored_mfa_json(config: GarminAuthConfig, descriptor: int) -> object:
    try:
        with os.fdopen(descriptor, encoding="utf-8") as state_file:
            return json.load(state_file)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
        raise invalid_mfa_state(config) from error


def _pending_mfa_from_json(
    config: GarminAuthConfig, stored: dict[str, object]
) -> _PendingMfaState:
    requested_at = stored.get("created_at")
    reason_value = stored.get("reason")
    client_state = stored.get("client_state")
    if (
        not isinstance(requested_at, str)
        or not requested_at
        or not isinstance(reason_value, str)
        or not isinstance(client_state, dict)
        or not client_state
    ):
        raise invalid_mfa_state(config)

    try:
        reason = GarminLoginReason(reason_value)
    except ValueError as error:
        raise invalid_mfa_state(config) from error
    return _PendingMfaState(reason, requested_at, client_state)


def write_mfa_state(
    config: GarminAuthConfig,
    client_state: object,
    reason: GarminLoginReason,
) -> _PendingMfaState:
    """Save the MFA client state atomically with mode 0600 and return it as pending."""
    if not isinstance(client_state, dict) or not client_state:
        raise GarminMfaStateInvalidError(_RETURNED_MFA_STATE_ERROR)

    requested_at = utc_now()
    serialized = _serialized_mfa_state(requested_at, reason, client_state)
    _replace_mfa_state_file(_mfa_state_path(config), serialized)
    return _PendingMfaState(reason, requested_at, client_state)


def _serialized_mfa_state(
    requested_at: str, reason: GarminLoginReason, client_state: dict[str, object]
) -> str:
    mfa_state_fields = {
        "version": _MFA_STATE_VERSION,
        "created_at": requested_at,
        "reason": reason.value,
        "client_state": client_state,
    }
    try:
        return json.dumps(mfa_state_fields, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as error:
        raise GarminMfaStateInvalidError(_RETURNED_MFA_STATE_ERROR) from error


def _replace_mfa_state_file(path: Path, serialized: str) -> None:
    temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = no_follow_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)

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
        _discard_temporary_state(temporary_path, descriptor)
        raise GarminTokenCacheError("Garmin MFA state could not be written") from error


def _discard_temporary_state(temporary_path: Path, descriptor: int) -> None:
    if descriptor >= 0:
        os.close(descriptor)
    try:
        temporary_path.unlink(missing_ok=True)
    except OSError as cleanup_error:
        raise GarminTokenCacheError(
            "Garmin MFA state could not be written or cleaned up"
        ) from cleanup_error
