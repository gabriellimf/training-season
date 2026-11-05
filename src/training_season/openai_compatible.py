"""
OpenAI-compatible chat completions adapter. OpenRouter is the default host.

Never log prompts, turns, responses, reasoning, server messages, or API keys.
"""

from __future__ import annotations

import logging
import math
import os
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypeGuard

import httpx2

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

OPENAI_COMPATIBLE_API_KEY_ENV = "OPENAI_COMPATIBLE_API_KEY"
OPENAI_COMPATIBLE_BASE_URL_ENV = "OPENAI_COMPATIBLE_BASE_URL"
OPENAI_COMPATIBLE_PLAN_MODEL_ENV = "OPENAI_COMPATIBLE_PLAN_MODEL"
OPENAI_COMPATIBLE_CHAT_MODEL_ENV = "OPENAI_COMPATIBLE_CHAT_MODEL"

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
_MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z", re.ASCII)
_API_KEY_PATTERN = re.compile(r"[\x21-\x7e]+")
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)
_MAX_ATTEMPTS = 3
# Free-tier daily limits send long Retry-After values. Fail at once, do not hang.
_MAX_RETRY_AFTER_SECONDS = 60.0
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OpenAICompatibleJobConfig:
    """Model settings for one job."""

    model: str
    max_tokens: int
    timeout: httpx2.Timeout


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """Validated OpenAI-compatible settings."""

    api_key: str = field(repr=False)
    base_url: str
    plan: OpenAICompatibleJobConfig
    chat: OpenAICompatibleJobConfig

    def for_job(self, job: LLMJob) -> OpenAICompatibleJobConfig:
        """Return settings for a model job."""
        if job is LLMJob.PLAN:
            return self.plan
        return self.chat


OpenAICompatibleClientFactory = Callable[[OpenAICompatibleConfig], httpx2.Client]


def _base_url(environment: Mapping[str, str], api_key: str) -> str:
    value = environment_value(
        environment, OPENAI_COMPATIBLE_BASE_URL_ENV, _DEFAULT_BASE_URL
    )
    try:
        url: httpx2.URL | None = httpx2.URL(value)
    except httpx2.InvalidURL:
        url = None
    if (
        url is None
        or url.scheme != "https"
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or api_key in value
    ):
        raise LLMConfigurationError(
            f"{OPENAI_COMPATIBLE_BASE_URL_ENV} must be an https URL such as "
            f"{_DEFAULT_BASE_URL}, with no user, password, query or fragment. "
            f"{config_value_detail(value, api_key)}"
        )
    return value.rstrip("/")


def openai_compatible_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> OpenAICompatibleConfig:
    """Read and validate OpenAI-compatible settings without contacting the API."""
    environment = os.environ if environ is None else environ
    api_key = environment.get(OPENAI_COMPATIBLE_API_KEY_ENV, "").strip()
    if not api_key:
        raise LLMConfigurationError(
            "OPENAI_COMPATIBLE_API_KEY is not set and LLM_PROVIDER is "
            "openai_compatible (the default). Set the key, or set "
            "LLM_PROVIDER=anthropic, in the environment or the Compose env file "
            "before starting."
        )

    if not _API_KEY_PATTERN.fullmatch(api_key):
        raise LLMConfigurationError(
            f"{OPENAI_COMPATIBLE_API_KEY_ENV} must hold only printable ASCII "
            "characters with no spaces."
        )

    base_url = _base_url(environment, api_key)
    plan_model = model_id(
        environment,
        OPENAI_COMPATIBLE_PLAN_MODEL_ENV,
        _DEFAULT_MODEL,
        api_key,
        pattern=_MODEL_ID_PATTERN,
    )
    chat_model = model_id(
        environment,
        OPENAI_COMPATIBLE_CHAT_MODEL_ENV,
        _DEFAULT_MODEL,
        api_key,
        pattern=_MODEL_ID_PATTERN,
    )

    return OpenAICompatibleConfig(
        api_key=api_key,
        base_url=base_url,
        plan=OpenAICompatibleJobConfig(
            model=plan_model,
            max_tokens=32_000,
            timeout=httpx2.Timeout(900.0, connect=5.0),
        ),
        chat=OpenAICompatibleJobConfig(
            model=chat_model,
            max_tokens=16_000,
            timeout=httpx2.Timeout(600.0, connect=5.0),
        ),
    )


def new_openai_compatible_client(
    config: OpenAICompatibleConfig,
    *,
    transport: httpx2.BaseTransport | None = None,
) -> httpx2.Client:
    """Create the HTTP client. Inject a mock transport only in tests."""
    raise_http_logger_levels("httpx2", "httpcore2")
    return httpx2.Client(
        base_url=config.base_url,
        headers={"Authorization": f"Bearer {config.api_key}"},
        transport=transport,
    )


def _field(value: object, name: str) -> object:
    return value.get(name) if isinstance(value, dict) else None


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _optional_count(value: object) -> int | None:
    if value is None:
        return 0
    return value if _is_count(value) else None


def _json_body(response: httpx2.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _request_id(body: object) -> str | None:
    value = _field(body, "id")
    if isinstance(value, str) and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return None


def _error_type(error: object) -> str | None:
    value = _field(_field(error, "metadata"), "error_type")
    if isinstance(value, str) and CODE_PATTERN.fullmatch(value):
        return value
    return None


def _usage(value: object) -> LLMUsage | None:
    if not isinstance(value, dict):
        return None
    input_tokens = value.get("prompt_tokens")
    output_tokens = value.get("completion_tokens")
    details = value.get("prompt_tokens_details")
    if details is not None and not isinstance(details, dict):
        return None
    cache_read = _optional_count(_field(details, "cached_tokens"))
    cache_write = _optional_count(_field(details, "cache_write_tokens"))
    cost = value.get("cost")
    if (
        not _is_count(input_tokens)
        or not _is_count(output_tokens)
        or cache_read is None
        or cache_write is None
    ):
        return None
    if cost is not None and (
        not isinstance(cost, int | float) or isinstance(cost, bool)
    ):
        return None
    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        cost_usd=(
            Decimal(str(cost))
            if cost is not None and math.isfinite(cost) and cost >= 0
            else None
        ),
    )


def _status_error(
    status_code: int,
    error_type: str | None,
    request_id: str | None,
) -> LLMError:
    details = error_details(status_code, error_type, request_id)
    error_class: type[LLMError]
    if status_code == 401:
        error_class = LLMAuthenticationError
        message = "rejected the API key"
    elif status_code == 429:
        error_class = LLMRateLimitedError
        message = "rate-limited the request after retries"
    elif status_code in (408, 409) or status_code >= 500:
        error_class = LLMUnavailableError
        message = "is unavailable after retries"
    else:
        error_class = LLMRequestRejectedError
        message = "rejected the request"
    return error_class(
        f"The OpenAI-compatible provider {message}{details}.",
        status_code=status_code,
        error_type=error_type,
        request_id=request_id,
    )


def _embedded_error(error: object, body: object) -> LLMError:
    code = _field(error, "code")
    status_code = (
        code
        if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599
        else 502
    )
    return _status_error(status_code, _error_type(error), _request_id(body))


def _malformed_response_error(body: object) -> LLMResponseError:
    return LLMResponseError(
        "The OpenAI-compatible provider returned a malformed response.",
        request_id=_request_id(body),
    )


def _retry_delay(
    failure: LLMError,
    retry_after: str | None,
    attempt: int,
) -> float | None:
    if attempt + 1 >= _MAX_ATTEMPTS or not isinstance(
        failure, LLMRateLimitedError | LLMUnavailableError
    ):
        return None
    try:
        seconds = float(retry_after) if retry_after is not None else 0.0
    except ValueError:
        seconds = 0.0
    if math.isfinite(seconds) and seconds > 0:
        return None if seconds > _MAX_RETRY_AFTER_SECONDS else seconds
    jitter = 1 - 0.25 * random.random()  # noqa: S311
    return min(0.5 * 2.0**attempt, 8.0) * jitter


def _log_failure(
    job: LLMJob,
    job_config: OpenAICompatibleJobConfig,
    error: LLMError,
    duration_seconds: float,
) -> None:
    _LOGGER.warning(
        "OpenAI-compatible call failed: job=%s model=%s error=%s status=%s "
        "error_type=%s request_id=%s duration_ms=%d",
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
    model: str,
    finish_reason: str | None,
    request_id: str | None,
    usage: LLMUsage,
    duration_seconds: float,
) -> None:
    cost = "unknown" if usage.cost_usd is None else f"{usage.cost_usd:.6f}"
    _LOGGER.info(
        "OpenAI-compatible call: job=%s model=%s finish_reason=%s "
        "input_tokens=%d output_tokens=%d cache_read_tokens=%d "
        "cache_write_tokens=%d cost_usd=%s duration_ms=%d request_id=%s",
        job.value,
        model,
        finish_reason,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens,
        usage.cache_write_input_tokens,
        cost,
        duration_ms(duration_seconds),
        request_id,
    )


class OpenAICompatibleAdapter:
    """Send every model request through one configured HTTP client."""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        factory: OpenAICompatibleClientFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._factory = new_openai_compatible_client if factory is None else factory
        self._sleep = sleep
        self._client: httpx2.Client | None = None

    def _get_client(self) -> httpx2.Client:
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
        body = {
            "model": job_config.model,
            "max_tokens": job_config.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                *({"role": turn.role, "content": turn.text} for turn in turns),
            ],
        }
        client = self._get_client()
        started_at = time.monotonic()

        for attempt in range(_MAX_ATTEMPTS):
            retry_after: str | None = None
            try:
                response = client.post(
                    "/chat/completions", json=body, timeout=job_config.timeout
                )
            except httpx2.RequestError as transport_error:
                error_type = (
                    "timeout"
                    if isinstance(transport_error, httpx2.TimeoutException)
                    else "connection"
                )
                failure: LLMError = LLMUnavailableError(
                    "Could not reach the OpenAI-compatible provider after retries "
                    f"({error_type}).",
                    error_type=error_type,
                )
            else:
                result = self._result(job, response, started_at)
                if isinstance(result, LLMReply):
                    return result
                failure = result
                retry_after = response.headers.get("retry-after")
            delay = _retry_delay(failure, retry_after, attempt)
            if delay is None:
                break
            self._sleep(delay)

        _log_failure(job, job_config, failure, time.monotonic() - started_at)
        raise failure from None

    def _result(
        self,
        job: LLMJob,
        response: httpx2.Response,
        started_at: float,
    ) -> LLMReply | LLMError:
        body = _json_body(response)
        if not response.is_success:
            return _status_error(
                response.status_code, _error_type(_field(body, "error")), None
            )
        if not isinstance(body, dict):
            return _malformed_response_error(body)
        # A provider failure after the headers is sent as HTTP 200 with only an error.
        if isinstance(body.get("error"), dict):
            return _embedded_error(body["error"], body)
        choices = body.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        if not isinstance(choice, dict):
            return _malformed_response_error(body)

        finish_reason = choice.get("finish_reason")
        if finish_reason == "error":
            # Providers can report an upstream error as HTTP 200 with partial content.
            return _embedded_error(choice.get("error"), body)

        message = choice.get("message")
        content = _field(message, "content")
        model = body.get("model")
        usage = _usage(body.get("usage"))
        if (
            not isinstance(message, dict)
            or (content is not None and not isinstance(content, str))
            or not isinstance(model, str)
            or not _MODEL_ID_PATTERN.fullmatch(model)
            or (
                finish_reason is not None
                and not (
                    isinstance(finish_reason, str)
                    and CODE_PATTERN.fullmatch(finish_reason)
                )
            )
            or usage is None
        ):
            return _malformed_response_error(body)

        request_id = _request_id(body)
        duration_seconds = time.monotonic() - started_at
        _log_response(job, model, finish_reason, request_id, usage, duration_seconds)

        if finish_reason == "length":
            raise LLMResponseError(
                "The model stopped at the max_tokens limit.",
                stop_reason=finish_reason,
                request_id=request_id,
            )
        if finish_reason == "content_filter":
            raise LLMRefusalError(
                "The model refused the request.",
                category=None,
                request_id=request_id,
            )
        if finish_reason != "stop":
            raise LLMResponseError(
                "The model returned an unsupported finish reason.",
                stop_reason=finish_reason,
                request_id=request_id,
            )
        if not content:
            raise LLMResponseError(
                "The model returned no text.",
                stop_reason=finish_reason,
                request_id=request_id,
            )

        return LLMReply(
            text=content,
            job=job,
            model=model,
            stop_reason=finish_reason,
            request_id=request_id,
            usage=usage,
            duration_seconds=duration_seconds,
        )

    def close(self) -> None:
        """Close the HTTP client if it has been created."""
        if self._client is not None:
            self._client.close()
            self._client = None
