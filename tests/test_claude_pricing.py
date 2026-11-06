"""Tests for the Anthropic adapter cost calculation in claude_pricing.py."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from claude_fakes import (
    _adapter,
    _clear_anthropic_variables,
    _json_handler,
    _message_payload,
    _turns,
)
from training_season.llm_base import LLMJob, LLMRefusalError


@pytest.fixture(autouse=True)
def clear_anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_anthropic_variables(monkeypatch)


@pytest.mark.parametrize(
    ("model", "expected_cost"),
    [
        ("claude-opus-5-5", Decimal("0.001536")),
        ("claude-sonnet-5-5", Decimal("0.000768")),
    ],
)
def test_cost_known_model(model: str, expected_cost: Decimal) -> None:
    payload = _message_payload(
        model=model,
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 20,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 10,
                "ephemeral_1h_input_tokens": 10,
            },
        },
    )

    with _adapter(_json_handler(payload)) as adapter:
        reply = adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert reply.usage.cost_usd == expected_cost


def test_cost_unknown_model_logs_unknown_and_returns_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _message_payload(model="claude-test-model")

    with caplog.at_level(logging.INFO), _adapter(_json_handler(payload)) as adapter:
        reply = adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert reply.usage.cost_usd is None
    assert "cost_usd=unknown" in caplog.text


@pytest.mark.parametrize(
    ("usage_overrides", "stop_reason"),
    [
        ({"service_tier": "priority"}, "end_turn"),
        ({"speed": "fast"}, "end_turn"),
        ({"inference_geo": "us"}, "end_turn"),
        ({"cache_creation_input_tokens": 20}, "end_turn"),
        ({}, "refusal"),
    ],
)
def test_cost_unknown_when_pricing_modifiers_present(
    usage_overrides: dict[str, object],
    stop_reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    usage: dict[str, object] = {"input_tokens": 100, "output_tokens": 50}
    usage.update(usage_overrides)
    details = (
        {"type": "refusal", "category": "general_harms"}
        if stop_reason == "refusal"
        else None
    )
    payload = _message_payload(
        stop_reason=stop_reason,
        stop_details=details,
        usage=usage,
    )

    with caplog.at_level(logging.INFO), _adapter(_json_handler(payload)) as adapter:
        if stop_reason == "refusal":
            with pytest.raises(LLMRefusalError):
                adapter.complete(LLMJob.PLAN, system="system", turns=_turns())
        else:
            reply = adapter.complete(
                LLMJob.PLAN,
                system="system",
                turns=_turns(),
            )
            assert reply.usage.cost_usd is None

    assert "cost_usd=unknown" in caplog.text
