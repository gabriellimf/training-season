"""Claude token prices and the cost of one response."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from training_season.llm_base import LLMUsage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from anthropic.types import Usage


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


def _is_standard_pricing(usage: Usage) -> bool:
    return (
        usage.service_tier in (None, "standard")
        and usage.speed in (None, "standard")
        and usage.inference_geo in (None, "global")
    )


def _cache_write_counts(usage: Usage) -> tuple[int, int] | None:
    cache_creation = usage.cache_creation
    if cache_creation is None:
        return None if (usage.cache_creation_input_tokens or 0) > 0 else (0, 0)
    return (
        cache_creation.ephemeral_5m_input_tokens,
        cache_creation.ephemeral_1h_input_tokens,
    )


def _priced_total(
    price: _ModelPrice,
    usage: Usage,
    cache_write_5m: int,
    cache_write_1h: int,
) -> Decimal:
    return (
        Decimal(usage.input_tokens) * price.input
        + Decimal(cache_write_5m) * price.cache_write_5m
        + Decimal(cache_write_1h) * price.cache_write_1h
        + Decimal(usage.cache_read_input_tokens or 0) * price.cache_read
        + Decimal(usage.output_tokens) * price.output
    )


def _cost_usd(model: str, usage: Usage, stop_reason: str | None) -> Decimal | None:
    price = _PRICES_USD_PER_MILLION_TOKENS.get(model)
    if stop_reason == "refusal" or price is None or not _is_standard_pricing(usage):
        return None
    write_counts = _cache_write_counts(usage)
    if write_counts is None:
        return None
    counts = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens or 0,
        *write_counts,
    )
    if any(token_count < 0 for token_count in counts):
        return None
    return _priced_total(price, usage, *write_counts) / Decimal(1_000_000)


def claude_usage(model: str, usage: Usage, stop_reason: str) -> LLMUsage:
    """Return the token counts and the cost of one response."""
    return LLMUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        cache_write_input_tokens=usage.cache_creation_input_tokens or 0,
        cost_usd=_cost_usd(model, usage, stop_reason),
    )
