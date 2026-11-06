"""Tests for the OpenAI-compatible body parsing in openai_compatible_body.py."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import httpx2
import pytest

from openai_compatible_fakes import (
    _DEFAULT_MODEL,
    _MALFORMED_BODY_SENTINEL,
    _adapter,
    _clear_openai_compatible_variables,
    _completion,
    _json_handler,
    _turns,
)
from training_season.llm_base import LLMJob, LLMRequestRejectedError, LLMResponseError


@pytest.fixture(autouse=True)
def clear_openai_compatible_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_openai_compatible_variables(monkeypatch)


def test_reply_maps_content_usage_cost_and_request_id() -> None:
    payload = _completion(
        content="reply text",
        usage={
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 10},
            "cost": 0.00123,
        },
    )
    choices = payload["choices"]
    assert isinstance(choices, list)
    choices[0]["message"]["reasoning"] = "reasoning text"

    with _adapter(_json_handler(payload)) as adapter:
        reply = adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert reply.text == "reply text"
    assert reply.job is LLMJob.CHAT
    assert reply.model == _DEFAULT_MODEL
    assert reply.stop_reason == "stop"
    assert reply.request_id == "gen-test"
    assert reply.usage.input_tokens == 120
    assert reply.usage.output_tokens == 30
    assert reply.usage.cache_read_input_tokens == 20
    assert reply.usage.cache_write_input_tokens == 5
    assert reply.usage.cost_usd == Decimal("0.00123")


def test_missing_cost_logs_unknown_and_invalid_id_gives_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _completion(
        usage={"prompt_tokens": 1, "completion_tokens": 2},
        request_id="not a safe id",
    )

    with caplog.at_level(logging.INFO), _adapter(_json_handler(payload)) as adapter:
        reply = adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert reply.usage.cost_usd is None
    assert reply.request_id is None
    assert "cost_usd=unknown" in caplog.text
    assert "request_id=None" in caplog.text


def test_error_type_must_be_a_code() -> None:
    payload = {"error": {"metadata": {"error_type": "Free Text"}}}

    with (
        _adapter(_json_handler(payload, status_code=400)) as adapter,
        pytest.raises(LLMRequestRejectedError) as error,
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert error.value.error_type is None
    assert str(error.value) == (
        "The OpenAI-compatible provider rejected the request (HTTP 400)."
    )


def _malformed_cases() -> list[tuple[str, bytes]]:
    no_choices = _completion(content=_MALFORMED_BODY_SENTINEL)
    no_choices["choices"] = []
    no_usage = _completion(content=_MALFORMED_BODY_SENTINEL)
    no_usage.pop("usage")
    bool_tokens = _completion(
        content=_MALFORMED_BODY_SENTINEL,
        usage={"prompt_tokens": True, "completion_tokens": 1},
    )
    negative_tokens = _completion(
        content=_MALFORMED_BODY_SENTINEL,
        usage={"prompt_tokens": -1, "completion_tokens": 1},
    )
    string_cost = _completion(
        content=_MALFORMED_BODY_SENTINEL,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": "0.1"},
    )
    content_object = _completion(content={"text": _MALFORMED_BODY_SENTINEL})
    free_text_finish = _completion(finish_reason=_MALFORMED_BODY_SENTINEL)
    forged_model = _completion(model=f"x\n{_MALFORMED_BODY_SENTINEL}")
    return [
        ("text/html", f"<html>{_MALFORMED_BODY_SENTINEL}</html>".encode()),
        ("application/json", b"null"),
        ("application/json", json.dumps([_MALFORMED_BODY_SENTINEL]).encode()),
        ("application/json", json.dumps(no_choices).encode()),
        ("application/json", json.dumps(no_usage).encode()),
        ("application/json", json.dumps(bool_tokens).encode()),
        ("application/json", json.dumps(negative_tokens).encode()),
        ("application/json", json.dumps(string_cost).encode()),
        ("application/json", json.dumps(content_object).encode()),
        ("application/json", json.dumps(free_text_finish).encode()),
        ("application/json", json.dumps(forged_model).encode()),
    ]


@pytest.mark.parametrize(("content_type", "body"), _malformed_cases())
def test_malformed_success_response(
    content_type: str,
    body: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200, headers={"content-type": content_type}, content=body
        )

    with (
        caplog.at_level(logging.WARNING),
        _adapter(handle) as adapter,
        pytest.raises(LLMResponseError) as error,
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert type(error.value) is LLMResponseError
    assert str(error.value).startswith(
        "The OpenAI-compatible provider returned a malformed response"
    )
    assert len(requests) == 1
    assert _MALFORMED_BODY_SENTINEL not in caplog.text
    assert _MALFORMED_BODY_SENTINEL not in str(error.value)
