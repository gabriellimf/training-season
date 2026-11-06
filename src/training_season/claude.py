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
from typing import TYPE_CHECKING, Literal, TypeGuard

import anthropic

from training_season.claude_message import (
    message_is_well_formed,
    message_refusal_category,
    message_request_id,
    message_text,
)
from training_season.claude_pricing import claude_usage
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

if TYPE_CHECKING:
    import httpx2
    from anthropic.types import (
        AssistantMessageParam,
        MessageParam,
        OutputConfigParam,
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
class _ClaudeJobDefaults:
    model_variable: str
    default_model: str
    effort_variable: str
    max_tokens: int
    read_timeout_seconds: float


_CLAUDE_PLAN_DEFAULTS = _ClaudeJobDefaults(
    CLAUDE_PLAN_MODEL_ENV, _DEFAULT_PLAN_MODEL, CLAUDE_PLAN_EFFORT_ENV, 32_000, 900.0
)
_CLAUDE_CHAT_DEFAULTS = _ClaudeJobDefaults(
    CLAUDE_CHAT_MODEL_ENV, _DEFAULT_CHAT_MODEL, CLAUDE_CHAT_EFFORT_ENV, 16_000, 600.0
)

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


def _anthropic_api_key(environment: Mapping[str, str]) -> str:
    api_key = environment.get(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        raise LLMConfigurationError(
            "ANTHROPIC_API_KEY is not set. Set it in the environment or the Compose "
            "env file before starting."
        )
    return api_key


def _claude_job_config(
    environment: Mapping[str, str],
    api_key: str,
    defaults: _ClaudeJobDefaults,
) -> ClaudeJobConfig:
    model = model_id(
        environment,
        defaults.model_variable,
        defaults.default_model,
        api_key,
        pattern=_MODEL_ID_PATTERN,
    )
    return ClaudeJobConfig(
        model=model,
        effort=_effort(environment, defaults.effort_variable, api_key),
        max_tokens=defaults.max_tokens,
        timeout=anthropic.Timeout(defaults.read_timeout_seconds, connect=5.0),
    )


def claude_config_from_environment(
    environ: Mapping[str, str] | None = None,
) -> ClaudeConfig:
    """Read and validate Anthropic settings without contacting the API."""
    environment = os.environ if environ is None else environ
    api_key = _anthropic_api_key(environment)
    plan = _claude_job_config(environment, api_key, _CLAUDE_PLAN_DEFAULTS)
    chat = _claude_job_config(environment, api_key, _CLAUDE_CHAT_DEFAULTS)
    return ClaudeConfig(api_key=api_key, plan=plan, chat=chat)


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


_ANTHROPIC_STATUS_ERRORS: Mapping[int, type[LLMError]] = {
    401: LLMAuthenticationError,
    403: LLMAuthenticationError,
    408: LLMUnavailableError,
    409: LLMUnavailableError,
    429: LLMRateLimitedError,
}
_ANTHROPIC = LLMProviderReporter(
    subject_name="Anthropic",
    object_name="Anthropic",
    log_prefix="Claude call",
    logger=_LOGGER,
    status_errors=_ANTHROPIC_STATUS_ERRORS,
)
_CLAUDE_STOP_FAILURES: Mapping[str, str] = {
    "max_tokens": "Claude stopped at the max_tokens limit.",
    "model_context_window_exceeded": "Claude exceeded the model context window.",
}


def _anthropic_message(turn: ChatTurn) -> MessageParam:
    if turn.role == "user":
        user_message: UserMessageParam = {"role": "user", "content": turn.text}
        return user_message
    assistant_message: AssistantMessageParam = {
        "role": "assistant",
        "content": turn.text,
    }
    return assistant_message


def _create_claude_message(
    client: anthropic.Anthropic,
    job_config: ClaudeJobConfig,
    system: str,
    messages: list[MessageParam],
) -> anthropic.types.Message:
    output_config: OutputConfigParam | anthropic.Omit = (
        anthropic.omit if job_config.effort is None else {"effort": job_config.effort}
    )
    return client.messages.create(
        model=job_config.model,
        max_tokens=job_config.max_tokens,
        system=system,
        messages=messages,
        output_config=output_config,
        timeout=job_config.timeout,
    )


def _schema_mismatch_error(
    sdk_error: anthropic.APIResponseValidationError,
) -> LLMResponseError:
    request_id = sdk_error.response.headers.get("request-id")
    return LLMResponseError(
        "Anthropic returned a response that does not match the SDK schema.",
        status_code=sdk_error.status_code,
        request_id=request_id if isinstance(request_id, str) else None,
    )


def _send_claude_request(
    client: anthropic.Anthropic,
    job_config: ClaudeJobConfig,
    system: str,
    messages: list[MessageParam],
) -> anthropic.types.Message | LLMError:
    try:
        return _create_claude_message(client, job_config, system, messages)
    except anthropic.APIStatusError as sdk_error:
        return _ANTHROPIC.status_error(
            sdk_error.status_code, sdk_error.type, sdk_error.request_id
        )
    except anthropic.APIConnectionError as sdk_error:
        return _ANTHROPIC.unreachable_error(
            timed_out=isinstance(sdk_error, anthropic.APITimeoutError)
        )
    except anthropic.APIResponseValidationError as sdk_error:
        return _schema_mismatch_error(sdk_error)
    except json.JSONDecodeError:
        return _ANTHROPIC.malformed_error(None)


def _is_usable_message(outcome: object) -> TypeGuard[anthropic.types.Message]:
    return isinstance(outcome, anthropic.types.Message) and message_is_well_formed(
        outcome
    )


def _claude_failure(outcome: object) -> LLMError:
    if isinstance(outcome, LLMError):
        return outcome
    return _ANTHROPIC.malformed_error(message_request_id(outcome))


def _log_claude_reply(reply: LLMReply, effort: Effort | None) -> None:
    effort_label = "none" if effort is None else effort
    _ANTHROPIC.log_response(
        reply.job,
        reply.model,
        f"effort={effort_label} stop_reason={reply.stop_reason}",
        reply.request_id,
        reply.usage,
        reply.duration_seconds,
    )


def _claude_stop_failure(reply: LLMReply) -> str | None:
    if reply.stop_reason in _CLAUDE_STOP_FAILURES:
        return _CLAUDE_STOP_FAILURES[reply.stop_reason]
    if reply.stop_reason not in ("end_turn", "stop_sequence"):
        return "Claude returned an unsupported stop reason."
    if reply.stop_reason == "end_turn" and not reply.text:
        return "Claude returned no text."
    return None


def _raise_for_claude_stop(response: anthropic.types.Message, reply: LLMReply) -> None:
    if reply.stop_reason == "refusal":
        raise LLMRefusalError(
            "Claude refused the request.",
            category=message_refusal_category(response),
            request_id=reply.request_id,
        )
    message = _claude_stop_failure(reply)
    if message is not None:
        raise LLMResponseError(
            message, stop_reason=reply.stop_reason, request_id=reply.request_id
        )


def _claude_reply(
    job: LLMJob,
    job_config: ClaudeJobConfig,
    response: anthropic.types.Message,
    stop_reason: str,
    started_at: float,
) -> LLMReply:
    reply = LLMReply(
        text=message_text(response),
        job=job,
        model=response.model,
        stop_reason=stop_reason,
        request_id=message_request_id(response),
        usage=claude_usage(response.model, response.usage, stop_reason),
        duration_seconds=time.monotonic() - started_at,
    )
    _log_claude_reply(reply, job_config.effort)
    _raise_for_claude_stop(response, reply)
    return reply


class ClaudeAdapter:
    """Send every model request through one configured SDK client."""

    def __init__(
        self,
        config: ClaudeConfig,
        *,
        factory: ClaudeClientFactory | None = None,
    ) -> None:
        self._config = config
        self._client = LazyLLMClient(
            config, new_anthropic_client if factory is None else factory
        )

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        """Complete one plan or chat request."""
        check_request(system, turns)
        job_config = self._config.for_job(job)
        messages = [_anthropic_message(turn) for turn in turns]
        client = self._client.get_or_create()
        started_at = time.monotonic()
        outcome = _send_claude_request(client, job_config, system, messages)
        if _is_usable_message(outcome) and isinstance(outcome.stop_reason, str):
            return _claude_reply(
                job, job_config, outcome, outcome.stop_reason, started_at
            )
        failure = _claude_failure(outcome)
        _ANTHROPIC.log_failure(job, job_config.model, failure, started_at)
        raise failure from None

    def close(self) -> None:
        """Close the SDK client if it has been created."""
        self._client.close()
