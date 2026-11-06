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

import httpx2

from training_season.llm_base import (
    ChatTurn,
    LazyLLMClient,
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMJob,
    LLMProviderReporter,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMReply,
    LLMResponseError,
    LLMUnavailableError,
    check_request,
    config_value_detail,
    environment_value,
    model_id,
    raise_http_logger_levels,
)
from training_season.openai_compatible_body import (
    OPENAI_COMPATIBLE_MODEL_ID_PATTERN,
    CheckedCompletion,
    checked_completion,
    completion_error_type,
    completion_field,
    completion_request_id,
)

OPENAI_COMPATIBLE_API_KEY_ENV = "OPENAI_COMPATIBLE_API_KEY"
OPENAI_COMPATIBLE_BASE_URL_ENV = "OPENAI_COMPATIBLE_BASE_URL"
OPENAI_COMPATIBLE_PLAN_MODEL_ENV = "OPENAI_COMPATIBLE_PLAN_MODEL"
OPENAI_COMPATIBLE_CHAT_MODEL_ENV = "OPENAI_COMPATIBLE_CHAT_MODEL"

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
_API_KEY_PATTERN = re.compile(r"[\x21-\x7e]+")
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


@dataclass(frozen=True, slots=True)
class _OpenAICompatibleJobDefaults:
    model_variable: str
    max_tokens: int
    read_timeout_seconds: float


_OPENAI_COMPATIBLE_PLAN_DEFAULTS = _OpenAICompatibleJobDefaults(
    OPENAI_COMPATIBLE_PLAN_MODEL_ENV, 32_000, 900.0
)
_OPENAI_COMPATIBLE_CHAT_DEFAULTS = _OpenAICompatibleJobDefaults(
    OPENAI_COMPATIBLE_CHAT_MODEL_ENV, 16_000, 600.0
)


def _is_safe_base_url(value: str, api_key: str) -> bool:
    try:
        url = httpx2.URL(value)
    except httpx2.InvalidURL:
        return False
    return (
        url.scheme == "https"
        and bool(url.host)
        and not url.userinfo
        and not url.query
        and not url.fragment
        and api_key not in value
    )


def _base_url(environment: Mapping[str, str], api_key: str) -> str:
    value = environment_value(
        environment, OPENAI_COMPATIBLE_BASE_URL_ENV, _DEFAULT_BASE_URL
    )
    if not _is_safe_base_url(value, api_key):
        raise LLMConfigurationError(
            f"{OPENAI_COMPATIBLE_BASE_URL_ENV} must be an https URL such as "
            f"{_DEFAULT_BASE_URL}, with no user, password, query or fragment. "
            f"{config_value_detail(value, api_key)}"
        )
    return value.rstrip("/")


def _openai_compatible_api_key(environment: Mapping[str, str]) -> str:
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

    return api_key


def _openai_compatible_job_config(
    environment: Mapping[str, str],
    api_key: str,
    defaults: _OpenAICompatibleJobDefaults,
) -> OpenAICompatibleJobConfig:
    return OpenAICompatibleJobConfig(
        model=model_id(
            environment,
            defaults.model_variable,
            _DEFAULT_MODEL,
            api_key,
            pattern=OPENAI_COMPATIBLE_MODEL_ID_PATTERN,
        ),
        max_tokens=defaults.max_tokens,
        timeout=httpx2.Timeout(defaults.read_timeout_seconds, connect=5.0),
    )


def openai_compatible_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> OpenAICompatibleConfig:
    """Read and validate OpenAI-compatible settings without contacting the API."""
    environment = os.environ if environ is None else environ
    api_key = _openai_compatible_api_key(environment)
    return OpenAICompatibleConfig(
        api_key=api_key,
        base_url=_base_url(environment, api_key),
        plan=_openai_compatible_job_config(
            environment, api_key, _OPENAI_COMPATIBLE_PLAN_DEFAULTS
        ),
        chat=_openai_compatible_job_config(
            environment, api_key, _OPENAI_COMPATIBLE_CHAT_DEFAULTS
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


def _json_body(response: httpx2.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


_OPENAI_COMPATIBLE_STATUS_ERRORS: Mapping[int, type[LLMError]] = {
    401: LLMAuthenticationError,
    408: LLMUnavailableError,
    409: LLMUnavailableError,
    429: LLMRateLimitedError,
}
_OPENAI_COMPATIBLE = LLMProviderReporter(
    subject_name="The OpenAI-compatible provider",
    object_name="the OpenAI-compatible provider",
    log_prefix="OpenAI-compatible call",
    logger=_LOGGER,
    status_errors=_OPENAI_COMPATIBLE_STATUS_ERRORS,
)


def _embedded_error(error: object, body: object) -> LLMError:
    code = completion_field(error, "code")
    status_code = (
        code
        if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599
        else 502
    )
    return _OPENAI_COMPATIBLE.status_error(
        status_code, completion_error_type(error), completion_request_id(body)
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


def _chat_completions_body(
    job_config: OpenAICompatibleJobConfig, system: str, turns: Sequence[ChatTurn]
) -> dict[str, object]:
    return {
        "model": job_config.model,
        "max_tokens": job_config.max_tokens,
        "messages": [
            {"role": "system", "content": system},
            *({"role": turn.role, "content": turn.text} for turn in turns),
        ],
    }


def _malformed_completion_error(body: object) -> LLMResponseError:
    return _OPENAI_COMPATIBLE.malformed_error(completion_request_id(body))


def _parsed_completion(body: object) -> CheckedCompletion | LLMError:
    if not isinstance(body, dict):
        return _malformed_completion_error(body)
    # A provider failure after the headers is sent as HTTP 200 with only an error.
    if isinstance(body.get("error"), dict):
        return _embedded_error(body["error"], body)
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        return _malformed_completion_error(body)
    if choice.get("finish_reason") == "error":
        # Providers can report an upstream error as HTTP 200 with partial content.
        return _embedded_error(choice.get("error"), body)
    completion = checked_completion(body, choice)
    return _malformed_completion_error(body) if completion is None else completion


def _stop_failure_message(finish_reason: str | None) -> str:
    if finish_reason == "length":
        return "The model stopped at the max_tokens limit."
    if finish_reason != "stop":
        return "The model returned an unsupported finish reason."
    return "The model returned no text."


def _completion_text(completion: CheckedCompletion) -> str:
    reason = completion.finish_reason
    if reason == "content_filter":
        raise LLMRefusalError(
            "The model refused the request.",
            category=None,
            request_id=completion.request_id,
        )
    if reason == "stop" and completion.content:
        return completion.content
    raise LLMResponseError(
        _stop_failure_message(reason),
        stop_reason=reason,
        request_id=completion.request_id,
    )


def _log_completion(
    job: LLMJob, completion: CheckedCompletion, duration_seconds: float
) -> None:
    _OPENAI_COMPATIBLE.log_response(
        job,
        completion.model,
        f"finish_reason={completion.finish_reason}",
        completion.request_id,
        completion.usage,
        duration_seconds,
    )


def _completion_reply(
    job: LLMJob, completion: CheckedCompletion, started_at: float
) -> LLMReply:
    duration_seconds = time.monotonic() - started_at
    _log_completion(job, completion, duration_seconds)
    return LLMReply(
        text=_completion_text(completion),
        job=job,
        model=completion.model,
        stop_reason="stop",
        request_id=completion.request_id,
        usage=completion.usage,
        duration_seconds=duration_seconds,
    )


def _chat_completion_outcome(
    job: LLMJob, response: httpx2.Response, started_at: float
) -> LLMReply | LLMError:
    body = _json_body(response)
    if not response.is_success:
        error_type = completion_error_type(completion_field(body, "error"))
        return _OPENAI_COMPATIBLE.status_error(response.status_code, error_type, None)
    completion = _parsed_completion(body)
    if isinstance(completion, LLMError):
        return completion
    return _completion_reply(job, completion, started_at)


def _attempt_chat_completion(
    client: httpx2.Client,
    job: LLMJob,
    job_config: OpenAICompatibleJobConfig,
    body: dict[str, object],
    started_at: float,
) -> LLMReply | tuple[LLMError, str | None]:
    try:
        response = client.post(
            "/chat/completions", json=body, timeout=job_config.timeout
        )
    except httpx2.RequestError as transport_error:
        timed_out = isinstance(transport_error, httpx2.TimeoutException)
        return _OPENAI_COMPATIBLE.unreachable_error(timed_out=timed_out), None
    outcome = _chat_completion_outcome(job, response, started_at)
    if isinstance(outcome, LLMReply):
        return outcome
    return outcome, response.headers.get("retry-after")


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
        self._client = LazyLLMClient(
            config, new_openai_compatible_client if factory is None else factory
        )
        self._sleep = sleep

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        """Complete one plan or chat request."""
        check_request(system, turns)
        job_config = self._config.for_job(job)
        body = _chat_completions_body(job_config, system, turns)
        client = self._client.get_or_create()
        started_at = time.monotonic()
        for attempt in range(_MAX_ATTEMPTS):
            attempt_outcome = _attempt_chat_completion(
                client, job, job_config, body, started_at
            )
            if isinstance(attempt_outcome, LLMReply):
                return attempt_outcome
            failure, retry_after = attempt_outcome
            delay = _retry_delay(failure, retry_after, attempt)
            if delay is None:
                break
            self._sleep(delay)
        _OPENAI_COMPATIBLE.log_failure(job, job_config.model, failure, started_at)
        raise failure from None

    def close(self) -> None:
        """Close the HTTP client if it has been created."""
        self._client.close()
