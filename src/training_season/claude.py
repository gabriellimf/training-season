"""
Anthropic adapter. Read configuration before any Garmin or database work.

Never log prompts, turns, responses, refusals, server messages, or API keys. Do not
hold a Garmin session across an LLM call. Use the default HTTP client in production.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

import anthropic

if TYPE_CHECKING:
    import httpx2
    from anthropic.types import (
        AssistantMessageParam,
        MessageParam,
        OutputConfigParam,
        Usage,
        UserMessageParam,
    )

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
CLAUDE_PLAN_MODEL_ENV = "CLAUDE_PLAN_MODEL"
CLAUDE_PLAN_EFFORT_ENV = "CLAUDE_PLAN_EFFORT"
CLAUDE_CHAT_MODEL_ENV = "CLAUDE_CHAT_MODEL"
CLAUDE_CHAT_EFFORT_ENV = "CLAUDE_CHAT_EFFORT"

Effort = Literal["low", "medium", "high", "xhigh", "max"]

_DEFAULT_PLAN_MODEL = "claude-opus-5-5"
_DEFAULT_CHAT_MODEL = "claude-sonnet-5-5"
_DEFAULT_EFFORT = "medium"
_MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z", re.ASCII)
_LOGGER = logging.getLogger(__name__)

_EFFORT_VALUES: dict[str, Effort | None] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "none": None,
}


class ClaudeJob(StrEnum):
    """Model job selected by a consumer."""

    PLAN = "plan"
    CHAT = "chat"


@dataclass(frozen=True, slots=True)
class ClaudeJobConfig:
    """Model settings for one job."""

    model: str
    effort: Effort | None
    max_tokens: int
    timeout: anthropic.Timeout


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    """Validated Anthropic settings."""

    api_key: str = field(repr=False)
    plan: ClaudeJobConfig
    chat: ClaudeJobConfig

    def for_job(self, job: ClaudeJob) -> ClaudeJobConfig:
        """Return settings for a model job."""
        if job is ClaudeJob.PLAN:
            return self.plan
        return self.chat


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One user or assistant message."""

    role: Literal["user", "assistant"]
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ClaudeUsage:
    """Token counts and estimated USD cost for one response."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_write_input_tokens: int
    cost_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class ClaudeReply:
    """Text and safe metadata returned by Claude."""

    text: str = field(repr=False)
    job: ClaudeJob
    model: str
    stop_reason: str
    request_id: str | None
    usage: ClaudeUsage
    duration_seconds: float


class ClaudeError(Exception):
    """Base class for Anthropic adapter errors."""

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


class ClaudeConfigurationError(ClaudeError):
    """Raised when Anthropic environment configuration is invalid."""


class ClaudeAuthenticationError(ClaudeError):
    """Raised when Anthropic rejects authentication."""


class ClaudeRequestRejectedError(ClaudeError):
    """Raised when Anthropic rejects a request."""


class ClaudeRateLimitedError(ClaudeError):
    """Raised when Anthropic rate-limits a request after retries."""


class ClaudeUnavailableError(ClaudeError):
    """Raised when Anthropic is unavailable after retries."""


class ClaudeResponseError(ClaudeError):
    """Raised when Anthropic returns unusable model output."""

    def __init__(
        self,
        message: str,
        *,
        stop_reason: str | None = None,
        status_code: int | None = None,
        error_type: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.stop_reason = stop_reason
        super().__init__(
            message,
            status_code=status_code,
            error_type=error_type,
            request_id=request_id,
        )


class ClaudeRefusalError(ClaudeResponseError):
    """Raised when the model refuses a request."""

    def __init__(self, *, category: str | None, request_id: str | None) -> None:
        self.category = category
        details = f" (category {category})" if category is not None else ""
        super().__init__(
            f"Claude refused the request{details}.",
            stop_reason="refusal",
            request_id=request_id,
        )


@dataclass(frozen=True, slots=True)
class _ModelPrice:
    input: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal
    output: Decimal


# Prices from https://platform.claude.com/docs/en/about-claude/pricing, read 2026-10-27.
_PRICES_USD_PER_MILLION_TOKENS: Mapping[str, _ModelPrice] = {
    "claude-opus-5-5": _ModelPrice(
        input=Decimal(4),
        cache_write_5m=Decimal(5),
        cache_write_1h=Decimal(8),
        cache_read=Decimal("0.20"),
        output=Decimal(20),
    ),
    "claude-sonnet-5-5": _ModelPrice(
        input=Decimal(2),
        cache_write_5m=Decimal("2.50"),
        cache_write_1h=Decimal(4),
        cache_read=Decimal("0.10"),
        output=Decimal(10),
    ),
}

ClaudeClientFactory = Callable[[ClaudeConfig], anthropic.Anthropic]


def _environment_value(
    environment: Mapping[str, str],
    variable: str,
    default: str,
) -> str:
    value = environment.get(variable)
    if value is None or not value.strip():
        return default
    return value.strip()


def _model_id(
    environment: Mapping[str, str],
    variable: str,
    default: str,
    api_key: str,
) -> str:
    model = _environment_value(environment, variable, default)
    if not _MODEL_ID_PATTERN.fullmatch(model) or api_key in model:
        raise ClaudeConfigurationError(
            f"{variable} must be a model id with no whitespace, using ASCII "
            "characters, and at most 128 characters."
        )
    return model


def _effort(
    environment: Mapping[str, str],
    variable: str,
) -> Effort | None:
    value = _environment_value(environment, variable, _DEFAULT_EFFORT)
    if value not in _EFFORT_VALUES:
        raise ClaudeConfigurationError(
            f"{variable} must be one of low, medium, high, xhigh, max, none."
        )
    return _EFFORT_VALUES[value]


def claude_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> ClaudeConfig:
    """Read and validate Anthropic settings without contacting the API."""
    environment = os.environ if environ is None else environ
    api_key = environment.get(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        raise ClaudeConfigurationError(
            "ANTHROPIC_API_KEY is not set. Set it in the environment or the Compose "
            "env file before starting."
        )

    plan_model = _model_id(
        environment, CLAUDE_PLAN_MODEL_ENV, _DEFAULT_PLAN_MODEL, api_key
    )
    plan_effort = _effort(environment, CLAUDE_PLAN_EFFORT_ENV)
    chat_model = _model_id(
        environment, CLAUDE_CHAT_MODEL_ENV, _DEFAULT_CHAT_MODEL, api_key
    )
    chat_effort = _effort(environment, CLAUDE_CHAT_EFFORT_ENV)

    return ClaudeConfig(
        api_key=api_key,
        plan=ClaudeJobConfig(
            model=plan_model,
            effort=plan_effort,
            max_tokens=32_000,
            timeout=anthropic.Timeout(900.0, connect=5.0),
        ),
        chat=ClaudeJobConfig(
            model=chat_model,
            effort=chat_effort,
            max_tokens=16_000,
            timeout=anthropic.Timeout(600.0, connect=5.0),
        ),
    )


def new_anthropic_client(
    config: ClaudeConfig,
    *,
    http_client: httpx2.Client | None = None,
) -> anthropic.Anthropic:
    """Create the SDK client. Inject a mock HTTP client only in tests."""
    for logger_name in ("anthropic", "httpx2", "httpcore2"):
        logging.getLogger(logger_name).setLevel(logging.INFO)

    if http_client is None:
        return anthropic.Anthropic(
            api_key=config.api_key,
            max_retries=2,
            open_telemetry=False,
        )
    return anthropic.Anthropic(
        api_key=config.api_key,
        max_retries=2,
        open_telemetry=False,
        http_client=http_client,
    )


def _cost_usd(model: str, usage: Usage, stop_reason: str | None) -> Decimal | None:
    if stop_reason == "refusal":
        return None

    price = _PRICES_USD_PER_MILLION_TOKENS.get(model)
    if price is None:
        return None
    if usage.service_tier not in (None, "standard"):
        return None
    if usage.speed not in (None, "standard"):
        return None
    if usage.inference_geo not in (None, "global"):
        return None

    cache_write_tokens = usage.cache_creation_input_tokens or 0
    cache_creation = usage.cache_creation
    if cache_write_tokens > 0 and cache_creation is None:
        return None

    cache_write_5m = (
        cache_creation.ephemeral_5m_input_tokens if cache_creation is not None else 0
    )
    cache_write_1h = (
        cache_creation.ephemeral_1h_input_tokens if cache_creation is not None else 0
    )
    counts = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens or 0,
        cache_write_5m,
        cache_write_1h,
    )
    if any(token_count < 0 for token_count in counts):
        return None

    total = (
        Decimal(usage.input_tokens) * price.input
        + Decimal(cache_write_5m) * price.cache_write_5m
        + Decimal(cache_write_1h) * price.cache_write_1h
        + Decimal(usage.cache_read_input_tokens or 0) * price.cache_read
        + Decimal(usage.output_tokens) * price.output
    )
    return total / Decimal(1_000_000)


def _usage_for_response(
    model: str,
    usage: Usage,
    stop_reason: str | None,
) -> ClaudeUsage:
    return ClaudeUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        cache_write_input_tokens=usage.cache_creation_input_tokens or 0,
        cost_usd=_cost_usd(model, usage, stop_reason),
    )


def _error_details(
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


def _status_error(error: anthropic.APIStatusError) -> ClaudeError:
    status_code = error.status_code
    error_type = error.type
    request_id = error.request_id
    details = _error_details(status_code, error_type, request_id)

    if status_code in (401, 403):
        return ClaudeAuthenticationError(
            f"Anthropic rejected the API key{details}.",
            status_code=status_code,
            error_type=error_type,
            request_id=request_id,
        )
    if status_code == 429:
        return ClaudeRateLimitedError(
            f"Anthropic rate-limited the request after retries{details}.",
            status_code=status_code,
            error_type=error_type,
            request_id=request_id,
        )
    if status_code in (408, 409) or status_code >= 500:
        return ClaudeUnavailableError(
            f"Anthropic is unavailable after retries{details}.",
            status_code=status_code,
            error_type=error_type,
            request_id=request_id,
        )
    return ClaudeRequestRejectedError(
        f"Anthropic rejected the request{details}.",
        status_code=status_code,
        error_type=error_type,
        request_id=request_id,
    )


def _duration_ms(duration_seconds: float) -> int:
    return max(0, int(duration_seconds * 1000))


def _log_failure(
    model: str,
    error: ClaudeError,
    duration_seconds: float,
) -> None:
    _LOGGER.warning(
        "Claude call failed: model=%s error=%s status=%s error_type=%s "
        "request_id=%s duration_ms=%d",
        model,
        type(error).__name__,
        error.status_code,
        error.error_type,
        error.request_id,
        _duration_ms(duration_seconds),
    )


def _log_response(
    model: str,
    request_id: str | None,
    usage: ClaudeUsage,
    duration_seconds: float,
) -> None:
    cost = "unknown" if usage.cost_usd is None else f"{usage.cost_usd:.6f}"
    _LOGGER.info(
        "Claude call: model=%s input_tokens=%d output_tokens=%d "
        "cache_read_tokens=%d cache_write_tokens=%d cost_usd=%s duration_ms=%d "
        "request_id=%s",
        model,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens,
        usage.cache_write_input_tokens,
        cost,
        _duration_ms(duration_seconds),
        request_id,
    )


class ClaudeAdapter:
    """Send every model request through one configured SDK client."""

    def __init__(
        self,
        config: ClaudeConfig,
        *,
        factory: ClaudeClientFactory | None = None,
    ) -> None:
        self._config = config
        self._factory = new_anthropic_client if factory is None else factory
        self._client: anthropic.Anthropic | None = None

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = self._factory(self._config)
        return self._client

    def complete(
        self,
        job: ClaudeJob,
        *,
        system: str,
        turns: Sequence[ChatTurn],
    ) -> ClaudeReply:
        """Complete one plan or chat request."""
        if not system.strip():
            raise ValueError("Claude system text is required")
        if not turns:
            raise ValueError("At least one Claude turn is required")
        if any(not turn.text.strip() for turn in turns):
            raise ValueError("Claude turn text is required")
        if turns[-1].role != "user":
            raise ValueError("The last Claude turn must be from the user")

        job_config = self._config.for_job(job)
        messages: list[MessageParam] = []
        for turn in turns:
            if turn.role == "user":
                user_message: UserMessageParam = {
                    "role": "user",
                    "content": turn.text,
                }
                messages.append(user_message)
            else:
                assistant_message: AssistantMessageParam = {
                    "role": "assistant",
                    "content": turn.text,
                }
                messages.append(assistant_message)
        client = self._get_client()
        started_at = time.monotonic()

        try:
            if job_config.effort is None:
                response = client.messages.create(
                    model=job_config.model,
                    max_tokens=job_config.max_tokens,
                    system=system,
                    messages=messages,
                    timeout=job_config.timeout,
                )
            else:
                output_config: OutputConfigParam = {"effort": job_config.effort}
                response = client.messages.create(
                    model=job_config.model,
                    max_tokens=job_config.max_tokens,
                    system=system,
                    messages=messages,
                    output_config=output_config,
                    timeout=job_config.timeout,
                )
        except anthropic.APIStatusError as sdk_error:
            failure = _status_error(sdk_error)
        except anthropic.APIConnectionError as sdk_error:
            error_type = (
                "timeout"
                if isinstance(sdk_error, anthropic.APITimeoutError)
                else "connection"
            )
            failure = ClaudeUnavailableError(
                f"Could not reach Anthropic after retries ({error_type}).",
                error_type=error_type,
            )
        except anthropic.APIResponseValidationError as sdk_error:
            failure = ClaudeResponseError(
                "Anthropic returned a response that does not match the SDK schema.",
                status_code=sdk_error.status_code,
                request_id=sdk_error.response.headers.get("request-id"),
            )
        else:
            duration_seconds = time.monotonic() - started_at
            return self._reply(job, response, duration_seconds)

        duration_seconds = time.monotonic() - started_at
        _log_failure(job_config.model, failure, duration_seconds)
        raise failure from None

    def _reply(
        self,
        job: ClaudeJob,
        response: anthropic.types.Message,
        duration_seconds: float,
    ) -> ClaudeReply:
        request_id = response._request_id
        stop_reason = response.stop_reason
        usage = _usage_for_response(response.model, response.usage, stop_reason)
        _log_response(
            response.model,
            request_id,
            usage,
            duration_seconds,
        )

        if stop_reason == "refusal":
            category = (
                response.stop_details.category
                if response.stop_details is not None
                else None
            )
            raise ClaudeRefusalError(category=category, request_id=request_id)
        if stop_reason == "max_tokens":
            raise ClaudeResponseError(
                "Claude stopped at the max_tokens limit.",
                stop_reason=stop_reason,
                request_id=request_id,
            )
        if stop_reason == "model_context_window_exceeded":
            raise ClaudeResponseError(
                "Claude exceeded the model context window.",
                stop_reason=stop_reason,
                request_id=request_id,
            )
        if stop_reason not in ("end_turn", "stop_sequence"):
            raise ClaudeResponseError(
                "Claude returned an unsupported stop reason.",
                stop_reason=stop_reason,
                request_id=request_id,
            )

        text = "".join(block.text for block in response.content if block.type == "text")
        if stop_reason == "end_turn" and not text:
            raise ClaudeResponseError(
                "Claude returned no text.",
                stop_reason=stop_reason,
                request_id=request_id,
            )

        return ClaudeReply(
            text=text,
            job=job,
            model=response.model,
            stop_reason=stop_reason,
            request_id=request_id,
            usage=usage,
            duration_seconds=duration_seconds,
        )

    def close(self) -> None:
        """Close the SDK client if it has been created."""
        if self._client is not None:
            self._client.close()
            self._client = None


def _print_usage() -> None:
    sys.stderr.write("Usage: training-season-claude check\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Check local Anthropic configuration without making an API call."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--help"], ["-h"]):
        _print_usage()
        return 0
    if arguments != ["check"]:
        _print_usage()
        return 2

    try:
        config = claude_config_from_environment()
    except ClaudeConfigurationError as error:
        sys.stderr.write(f"{error}\n")
        return 2

    sys.stdout.write(f"model={config.plan.model}\nmodel={config.chat.model}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
