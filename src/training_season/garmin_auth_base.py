"""Types, errors and settings shared by the Garmin authentication modules."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from garminconnect import Garmin

if TYPE_CHECKING:
    from pathlib import Path

GARMIN_TOKEN_DIR_ENV = "GARMIN_TOKEN_DIR"  # noqa: S105
GARMIN_EMAIL_ENV = "GARMIN_EMAIL"
GARMIN_PASSWORD_ENV = "GARMIN_PASSWORD"  # noqa: S105

GarminFactory = Callable[[str | None, str | None], Garmin]


class GarminLoginReason(StrEnum):
    """Reason the current Garmin session used a credential login."""

    NO_TOKEN = "no_token"  # noqa: S105
    TOKEN_REJECTED = "token_rejected"  # noqa: S105
    TOKEN_UNREADABLE = "token_unreadable"  # noqa: S105
    MANUAL = "manual"


@dataclass(frozen=True)
class GarminAuthConfig:
    """Validated paths and optional Garmin credentials."""

    token_dir: Path
    email: str | None = field(repr=False)
    password: str | None = field(repr=False)


@dataclass(frozen=True)
class GarminSession:
    """Authenticated Garmin client and the reason for any credential login."""

    garmin: Garmin
    login_reason: GarminLoginReason | None


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


_MFA_REQUIRED_MESSAGES: Mapping[GarminLoginReason, str] = {
    GarminLoginReason.TOKEN_REJECTED: (
        "Garmin rejected the cached token (expired or revoked) and requires "
        "MFA. Enter the code in the chat to finish login."
    ),
    GarminLoginReason.TOKEN_UNREADABLE: (
        "Garmin token cache is unreadable. Garmin requires MFA. Enter the "
        "code in the chat to finish login."
    ),
}
_MFA_REQUIRED_DEFAULT_MESSAGE = (
    "Garmin requires MFA. Enter the code in the chat to finish login."
)


class GarminMfaRequiredError(GarminError):
    """Raised when Garmin is waiting for an MFA code."""

    def __init__(self, reason: GarminLoginReason, requested_at: str) -> None:
        self.reason = reason
        self.requested_at = requested_at
        message = _MFA_REQUIRED_MESSAGES.get(reason, _MFA_REQUIRED_DEFAULT_MESSAGE)
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


def no_follow_flags(flags: int) -> int:
    """Add ``O_NOFOLLOW`` when the platform has it, so ``os.open`` refuses a symlink."""
    if hasattr(os, "O_NOFOLLOW"):
        return flags | os.O_NOFOLLOW
    return flags
