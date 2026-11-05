"""
Provider-neutral LLM types, errors and shared adapter helpers.

Never log prompts, turns, responses, refusals, server messages, or API keys.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal

CODE_PATTERN = re.compile(r"[a-z_]{1,64}\Z", re.ASCII)


class LLMJob(StrEnum):
    """Model job selected by a consumer."""

    PLAN = "plan"
    CHAT = "chat"


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One user or assistant message."""

    role: Literal["user", "assistant"]
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token counts and estimated USD cost for one response."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_write_input_tokens: int
    cost_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class LLMReply:
    """Text and safe metadata returned by a provider."""

    text: str = field(repr=False)
    job: LLMJob
    model: str
    stop_reason: str
    request_id: str | None
    usage: LLMUsage
    duration_seconds: float


class LLMError(Exception):
    """Base class for LLM adapter errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_type = error_type
        self.request_id = request_id
        super().__init__(message)


class LLMConfigurationError(LLMError):
    """Raised when LLM environment configuration is invalid."""


class LLMAuthenticationError(LLMError):
    """Raised when the provider rejects authentication."""


class LLMRequestRejectedError(LLMError):
    """Raised when the provider rejects a request."""


class LLMRateLimitedError(LLMError):
    """Raised when the provider rate-limits a request after retries."""


class LLMUnavailableError(LLMError):
    """Raised when the provider is unavailable after retries."""


class LLMResponseError(LLMError):
    """Raised when the provider returns unusable model output."""

    def __init__(
        self,
        message: str,
        *,
        stop_reason: str | None = None,
        category: str | None = None,
        status_code: int | None = None,
        error_type: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.stop_reason = stop_reason
        super().__init__(
            response_error_message(
                message,
                category=category,
                request_id=request_id,
            ),
            status_code=status_code,
            error_type=error_type,
            request_id=request_id,
        )


class LLMRefusalError(LLMResponseError):
    """Raised when the model refuses a request."""

    def __init__(
        self,
        message: str,
        *,
        category: str | None,
        request_id: str | None,
    ) -> None:
        self.category = category
        super().__init__(
            message,
            stop_reason="refusal",
            category=category,
            request_id=request_id,
        )


class LLMAdapter(Protocol):
    """One provider behind the neutral interface."""

    def complete(
        self,
        job: LLMJob,
        *,
        system: str,
        turns: Sequence[ChatTurn],
    ) -> LLMReply:
        """Complete one plan or chat request."""
        ...

    def close(self) -> None:
        """Release the provider client."""
        ...


def check_request(system: str, turns: Sequence[ChatTurn]) -> None:
    """Reject a request that no provider can send."""
    if not system.strip():
        raise ValueError("LLM system text is required")
    if not turns:
        raise ValueError("At least one LLM turn is required")
    if any(not turn.text.strip() for turn in turns):
        raise ValueError("LLM turn text is required")
    if turns[-1].role != "user":
        raise ValueError("The last LLM turn must be from the user")


def environment_value(
    environment: Mapping[str, str],
    variable: str,
    default: str,
) -> str:
    value = environment.get(variable)
    if value is None or not value.strip():
        return default
    return value.strip()


def config_value_detail(value: str, *secrets: str) -> str:
    if len(value) <= 64 and not any(secret and secret in value for secret in secrets):
        return f"Got: {value!r}."
    return f"Got a value of {len(value)} characters."


def model_id(
    environment: Mapping[str, str],
    variable: str,
    default: str,
    api_key: str,
    *,
    pattern: re.Pattern[str],
) -> str:
    model = environment_value(environment, variable, default)
    if not pattern.fullmatch(model) or api_key in model:
        raise LLMConfigurationError(
            f"{variable} must be a model id such as {default}: ASCII, no whitespace, "
            f"at most 128 characters. {config_value_detail(model, api_key)}"
        )
    return model


def error_details(
    status_code: int | None,
    error_type: str | None,
    request_id: str | None,
) -> str:
    details: list[str] = []
    if status_code is not None:
        details.append(f"HTTP {status_code}")
    if error_type is not None:
        details.append(error_type)
    if request_id is not None:
        details.append(f"request_id {request_id}")
    return f" ({', '.join(details)})" if details else ""


def duration_ms(duration_seconds: float) -> int:
    return max(0, int(duration_seconds * 1000))


def response_error_message(
    message: str,
    *,
    category: str | None,
    request_id: str | None,
) -> str:
    details: list[str] = []
    if category is not None and CODE_PATTERN.fullmatch(category):
        details.append(f"category {category}")
    if isinstance(request_id, str) and request_id:
        details.append(f"request_id {request_id}")
    if not details:
        return message
    sentence = message.removesuffix(".")
    return f"{sentence} ({', '.join(details)})."


def raise_http_logger_levels(*names: str) -> None:
    """Raise noisy HTTP loggers to INFO. Never lower a level."""
    for name in names:
        logger = logging.getLogger(name)
        if logger.level < logging.INFO:
            logger.setLevel(logging.INFO)
