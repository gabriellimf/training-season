"""Checks and readers for the Anthropic SDK Message."""

from __future__ import annotations

from collections.abc import Mapping

import anthropic

from training_season.llm_base import CODE_PATTERN


def _response_field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_optional_integer(value: object) -> bool:
    return value is None or _is_integer(value)


def _block_is_well_formed(block: object) -> bool:
    block_type = _response_field(block, "type")
    if not isinstance(block_type, str):
        return False
    return block_type != "text" or isinstance(_response_field(block, "text"), str)


def _cache_creation_is_well_formed(cache_creation: object) -> bool:
    return cache_creation is None or (
        _is_integer(getattr(cache_creation, "ephemeral_5m_input_tokens", None))
        and _is_integer(getattr(cache_creation, "ephemeral_1h_input_tokens", None))
    )


def _usage_is_well_formed(usage: object) -> bool:
    if not isinstance(usage, anthropic.types.Usage):
        return False
    if not _is_integer(usage.input_tokens) or not _is_integer(usage.output_tokens):
        return False
    if not _is_optional_integer(usage.cache_read_input_tokens):
        return False
    if not _is_optional_integer(usage.cache_creation_input_tokens):
        return False
    labels = (usage.service_tier, usage.speed, usage.inference_geo)
    return _cache_creation_is_well_formed(usage.cache_creation) and all(
        label is None or isinstance(label, str) for label in labels
    )


def message_is_well_formed(response: anthropic.types.Message) -> bool:
    """Return whether the SDK Message has the fields the adapter reads."""
    if not isinstance(response.model, str) or not response.model.strip():
        return False
    if not isinstance(response.stop_reason, str) or not response.stop_reason.strip():
        return False
    if not isinstance(response.content, list):
        return False
    return all(
        _block_is_well_formed(block) for block in response.content
    ) and _usage_is_well_formed(response.usage)


def message_request_id(response: object) -> str | None:
    """Return the request id the SDK attached to a Message."""
    if not isinstance(response, anthropic.types.Message):
        return None
    request_id = getattr(response, "_request_id", None)
    return request_id if isinstance(request_id, str) else None


def _block_text(block: object) -> str:
    if _response_field(block, "type") != "text":
        return ""
    text = _response_field(block, "text")
    return text if isinstance(text, str) else ""


def message_text(response: anthropic.types.Message) -> str:
    """Join the text blocks of a Message."""
    return "".join(_block_text(block) for block in response.content)


def message_refusal_category(response: anthropic.types.Message) -> str | None:
    """Return the refusal category when it is a safe code, else None."""
    category = _response_field(response.stop_details, "category")
    if isinstance(category, str) and CODE_PATTERN.fullmatch(category):
        return category
    return None
