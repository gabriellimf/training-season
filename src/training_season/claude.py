"""
Anthropic adapter. Read configuration before any Garmin or database work.

Never log prompts, turns, responses, refusals, server messages, or API keys. Do not
hold a Garmin session across an LLM call. Use the default HTTP client in production.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import anthropic

from training_season.llm_base import (
    CODE_PATTERN,
    ChatTurn,
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMJob,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMReply,
    LLMRequestRejectedError,
    LLMResponseError,
    LLMUnavailableError,
    LLMUsage,
    check_request,
    config_value_detail,
    duration_ms,
    environment_value,
    error_details,
    model_id,
    raise_http_logger_levels,
)

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

    def for_job(self, job: LLMJob) -> ClaudeJobConfig:
        """Return settings for a model job."""
        if job is LLMJob.PLAN:
            return self.plan
        return self.chat


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


def _effort(
    environment: Mapping[str, str],
    variable: str,
    api_key: str,
) -> Effort | None:
    value = environment_value(environment, variable, _DEFAULT_EFFORT)
    if value not in _EFFORT_VALUES:
        raise LLMConfigurationError(
            f"{variable} must be one of low, medium, high, xhigh, max, none. "
            f"{config_value_detail(value, api_key)}"
        )
    return _EFFORT_VALUES[value]


def claude_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> ClaudeConfig:
    """Read and validate Anthropic settings without contacting the API."""
    environment = os.environ if environ is None else environ
    api_key = environment.get(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        raise LLMConfigurationError(
            "ANTHROPIC_API_KEY is not set. Set it in the environment or the Compose "
            "env file before starting."
        )

    plan_model = model_id(
        environment,
        CLAUDE_PLAN_MODEL_ENV,
        _DEFAULT_PLAN_MODEL,
        api_key,
        pattern=_MODEL_ID_PATTERN,
    )
    plan_effort = _effort(environment, CLAUDE_PLAN_EFFORT_ENV, api_key)
    chat_model = model_id(
        environment,
        CLAUDE_CHAT_MODEL_ENV,
        _DEFAULT_CHAT_MODEL,
        api_key,
        pattern=_MODEL_ID_PATTERN,
    )
    chat_effort = _effort(environment, CLAUDE_CHAT_EFFORT_ENV, api_key)

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
    raise_http_logger_levels("anthropic", "httpx2", "httpcore2")

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
) -> LLMUsage:
    return LLMUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        cache_write_input_tokens=usage.cache_creation_input_tokens or 0,
        cost_usd=_cost_usd(model, usage, stop_reason),
    )


def _response_field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_optional_integer(value: object) -> bool:
    return value is None or _is_integer(value)


def _message_is_well_formed(response: anthropic.types.Message) -> bool:
    if not isinstance(response.model, str) or not response.model.strip():
        return False
    if not isinstance(response.stop_reason, str) or not response.stop_reason.strip():
        return False
    if not isinstance(response.content, list):
        return False

    for block in response.content:
        block_type = _response_field(block, "type")
        if not isinstance(block_type, str):
            return False
        if block_type == "text" and not isinstance(_response_field(block, "text"), str):
            return False

    usage = response.usage
    if not isinstance(usage, anthropic.types.Usage):
        return False
    if not _is_integer(usage.input_tokens) or not _is_integer(usage.output_tokens):
        return False
    if not _is_optional_integer(usage.cache_read_input_tokens):
        return False
    if not _is_optional_integer(usage.cache_creation_input_tokens):
        return False
    cache_creation = usage.cache_creation
    if cache_creation is not None and (
        not _is_integer(getattr(cache_creation, "ephemeral_5m_input_tokens", None))
        or not _is_integer(getattr(cache_creation, "ephemeral_1h_input_tokens", None))
    ):
        return False
    return not any(
        value is not None and not isinstance(value, str)
        for value in (usage.service_tier, usage.speed, usage.inference_geo)
    )


def _message_request_id(response: object) -> str | None:
    if not isinstance(response, anthropic.types.Message):
        return None
    request_id = getattr(response, "_request_id", None)
    return request_id if isinstance(request_id, str) else None


def _malformed_response_error(response: object) -> LLMResponseError:
    return LLMResponseError(
        "Anthropic returned a malformed response.",
        request_id=_message_request_id(response),
    )


def _status_error(error: anthropic.APIStatusError) -> LLMError:
    status_code = error.status_code
    error_type = error.type
    request_id = error.request_id
    details = error_details(status_code, error_type, request_id)

    if status_code in (401, 403):
        return LLMAuthenticationError(
            f"Anthropic rejected the API key{details}.",
            status_code=status_code,
            error_type=error_type,
            request_id=request_id,
        )
    if status_code == 429:
        return LLMRateLimitedError(
            f"Anthropic rate-limited the request after retries{details}.",
            status_code=status_code,
            error_type=error_type,
            request_id=request_id,
        )
    if status_code in (408, 409) or status_code >= 500:
        return LLMUnavailableError(
            f"Anthropic is unavailable after retries{details}.",
            status_code=status_code,
            error_type=error_type,
            request_id=request_id,
        )
    return LLMRequestRejectedError(
        f"Anthropic rejected the request{details}.",
        status_code=status_code,
        error_type=error_type,
        request_id=request_id,
    )


def _log_failure(
    job: LLMJob,
    job_config: ClaudeJobConfig,
    error: LLMError,
    duration_seconds: float,
) -> None:
    _LOGGER.warning(
        "Claude call failed: job=%s model=%s error=%s status=%s error_type=%s "
        "request_id=%s duration_ms=%d",
        job.value,
        job_config.model,
        type(error).__name__,
        error.status_code,
        error.error_type,
        error.request_id,
        duration_ms(duration_seconds),
    )


def _log_response(
    job: LLMJob,
    job_config: ClaudeJobConfig,
    model: str,
    stop_reason: str,
    request_id: str | None,
    usage: LLMUsage,
    duration_seconds: float,
) -> None:
    cost = "unknown" if usage.cost_usd is None else f"{usage.cost_usd:.6f}"
    _LOGGER.info(
        "Claude call: job=%s model=%s effort=%s stop_reason=%s "
        "input_tokens=%d output_tokens=%d cache_read_tokens=%d "
        "cache_write_tokens=%d cost_usd=%s duration_ms=%d request_id=%s",
        job.value,
        model,
        "none" if job_config.effort is None else job_config.effort,
        stop_reason,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens,
        usage.cache_write_input_tokens,
        cost,
        duration_ms(duration_seconds),
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
        job: LLMJob,
        *,
        system: str,
        turns: Sequence[ChatTurn],
    ) -> LLMReply:
        """Complete one plan or chat request."""
        check_request(system, turns)

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
            failure = LLMUnavailableError(
                f"Could not reach Anthropic after retries ({error_type}).",
                error_type=error_type,
            )
        except anthropic.APIResponseValidationError as sdk_error:
            request_id = sdk_error.response.headers.get("request-id")
            failure = LLMResponseError(
                "Anthropic returned a response that does not match the SDK schema.",
                status_code=sdk_error.status_code,
                request_id=request_id if isinstance(request_id, str) else None,
            )
        except json.JSONDecodeError:
            failure = _malformed_response_error(None)
        else:
            if (
                isinstance(response, anthropic.types.Message)
                and _message_is_well_formed(response)
                and isinstance(response.stop_reason, str)
            ):
                return self._reply(
                    job,
                    job_config,
                    response,
                    response.stop_reason,
                    time.monotonic() - started_at,
                )
            failure = _malformed_response_error(response)

        duration_seconds = time.monotonic() - started_at
        _log_failure(job, job_config, failure, duration_seconds)
        raise failure from None

    def _reply(
        self,
        job: LLMJob,
        job_config: ClaudeJobConfig,
        response: anthropic.types.Message,
        stop_reason: str,
        duration_seconds: float,
    ) -> LLMReply:
        request_id = _message_request_id(response)
        usage = _usage_for_response(response.model, response.usage, stop_reason)
        _log_response(
            job,
            job_config,
            response.model,
            stop_reason,
            request_id,
            usage,
            duration_seconds,
        )

        if stop_reason == "refusal":
            raw_category = _response_field(response.stop_details, "category")
            category = (
                raw_category
                if isinstance(raw_category, str)
                and CODE_PATTERN.fullmatch(raw_category)
                else None
            )
            raise LLMRefusalError(
                "Claude refused the request.",
                category=category,
                request_id=request_id,
            )
        if stop_reason == "max_tokens":
            raise LLMResponseError(
                "Claude stopped at the max_tokens limit.",
                stop_reason=stop_reason,
                request_id=request_id,
            )
        if stop_reason == "model_context_window_exceeded":
            raise LLMResponseError(
                "Claude exceeded the model context window.",
                stop_reason=stop_reason,
                request_id=request_id,
            )
        if stop_reason not in ("end_turn", "stop_sequence"):
            raise LLMResponseError(
                "Claude returned an unsupported stop reason.",
                stop_reason=stop_reason,
                request_id=request_id,
            )

        text_parts: list[str] = []
        for block in response.content:
            if _response_field(block, "type") == "text":
                block_text = _response_field(block, "text")
                if isinstance(block_text, str):
                    text_parts.append(block_text)
        text = "".join(text_parts)
        if stop_reason == "end_turn" and not text:
            raise LLMResponseError(
                "Claude returned no text.",
                stop_reason=stop_reason,
                request_id=request_id,
            )

        return LLMReply(
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
