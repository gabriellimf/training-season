"""Readers and shape checks for OpenAI-compatible chat completion JSON bodies."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeGuard

from training_season.llm_base import CODE_PATTERN, LLMUsage

OPENAI_COMPATIBLE_MODEL_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z", re.ASCII
)
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class CheckedCompletion:
    """Completion fields that passed the shape checks."""

    model: str
    content: str | None
    finish_reason: str | None
    request_id: str | None
    usage: LLMUsage


def completion_field(value: object, name: str) -> object:
    """Read one key of a JSON object. A non-object gives None."""
    return value.get(name) if isinstance(value, dict) else None


def completion_request_id(body: object) -> str | None:
    """Read the request id of a response body. A bad shape gives None."""
    value = completion_field(body, "id")
    if isinstance(value, str) and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return None


def completion_error_type(error: object) -> str | None:
    """Read the provider error type of an error object. A bad shape gives None."""
    value = completion_field(completion_field(error, "metadata"), "error_type")
    if isinstance(value, str) and CODE_PATTERN.fullmatch(value):
        return value
    return None


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _optional_count(value: object) -> int | None:
    if value is None:
        return 0
    return value if _is_count(value) else None


def _cache_counts(details: object) -> tuple[int, int] | None:
    if details is not None and not isinstance(details, dict):
        return None
    cache_read = _optional_count(completion_field(details, "cached_tokens"))
    cache_write = _optional_count(completion_field(details, "cache_write_tokens"))
    if cache_read is None or cache_write is None:
        return None
    return cache_read, cache_write


def _is_reported_cost(value: object) -> TypeGuard[float | None]:
    return value is None or (
        isinstance(value, int | float) and not isinstance(value, bool)
    )


def _reported_cost_usd(cost: float | None) -> Decimal | None:
    if cost is None or not math.isfinite(cost) or cost < 0:
        return None
    return Decimal(str(cost))


def _token_counts(usage: dict[str, object]) -> tuple[int, int] | None:
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    if _is_count(input_tokens) and _is_count(output_tokens):
        return input_tokens, output_tokens
    return None


def _completion_usage(value: object) -> LLMUsage | None:
    if not isinstance(value, dict):
        return None
    tokens = _token_counts(value)
    cache = _cache_counts(value.get("prompt_tokens_details"))
    cost = value.get("cost")
    if tokens is None or cache is None or not _is_reported_cost(cost):
        return None
    return LLMUsage(
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        cache_read_input_tokens=cache[0],
        cache_write_input_tokens=cache[1],
        cost_usd=_reported_cost_usd(cost),
    )


def _is_optional_text(value: object) -> TypeGuard[str | None]:
    return value is None or isinstance(value, str)


def _is_completion_model(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and OPENAI_COMPATIBLE_MODEL_ID_PATTERN.fullmatch(value) is not None
    )


def _is_finish_reason(value: object) -> TypeGuard[str | None]:
    return value is None or (
        isinstance(value, str) and CODE_PATTERN.fullmatch(value) is not None
    )


def _checked_text_fields(
    body: dict[str, object], choice: dict[str, object]
) -> tuple[str, str | None, str | None] | None:
    message = choice.get("message")
    content = completion_field(message, "content")
    model = body.get("model")
    finish_reason = choice.get("finish_reason")
    if not (
        isinstance(message, dict)
        and _is_optional_text(content)
        and _is_completion_model(model)
        and _is_finish_reason(finish_reason)
    ):
        return None
    return model, content, finish_reason


def checked_completion(
    body: dict[str, object], choice: dict[str, object]
) -> CheckedCompletion | None:
    """Return the completion fields, or None when any of them has a bad shape."""
    fields = _checked_text_fields(body, choice)
    usage = _completion_usage(body.get("usage"))
    if fields is None or usage is None:
        return None
    model, content, finish_reason = fields
    return CheckedCompletion(
        model=model,
        content=content,
        finish_reason=finish_reason,
        request_id=completion_request_id(body),
        usage=usage,
    )
