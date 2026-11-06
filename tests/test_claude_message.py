"""Tests for the Anthropic adapter message parsing in claude_message.py."""

from __future__ import annotations

import json
import logging

import httpx2
import pytest

from claude_fakes import (
    RequestHandler,
    _adapter,
    _clear_anthropic_variables,
    _json_handler,
    _message_payload,
    _turns,
)
from training_season.llm_base import LLMJob, LLMRefusalError, LLMResponseError

_MALFORMED_BODY_SENTINEL = "private malformed response body sentinel"


@pytest.fixture(autouse=True)
def clear_anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_anthropic_variables(monkeypatch)


def _raw_json_handler(content_type: str, body: bytes) -> RequestHandler:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={
                "content-type": content_type,
                "request-id": "req_malformed",
            },
            content=body,
        )

    return handle


def _malformed_response_cases() -> list[tuple[str, bytes, bool]]:
    missing_usage = _message_payload(
        content=[{"type": "text", "text": _MALFORMED_BODY_SENTINEL}]
    )
    missing_usage.pop("usage")
    invalid_text = _message_payload(
        content=[
            {
                "type": "text",
                "text": None,
                "private": _MALFORMED_BODY_SENTINEL,
            }
        ]
    )
    string_token_count = _message_payload(
        content=[{"type": "text", "text": _MALFORMED_BODY_SENTINEL}],
        usage={"input_tokens": "not-an-integer", "output_tokens": 50},
    )
    boolean_token_count = _message_payload(
        content=[{"type": "text", "text": _MALFORMED_BODY_SENTINEL}],
        usage={"input_tokens": True, "output_tokens": 50},
    )
    non_object_content = _message_payload()
    non_object_content["content"] = [_MALFORMED_BODY_SENTINEL]

    return [
        (
            "text/html",
            f"<html>{_MALFORMED_BODY_SENTINEL}</html>".encode(),
            False,
        ),
        ("application/json", b"null", False),
        (
            "application/json",
            f'{{"body":"{_MALFORMED_BODY_SENTINEL}"'.encode(),
            False,
        ),
        (
            "application/json",
            json.dumps([_MALFORMED_BODY_SENTINEL]).encode(),
            False,
        ),
        ("application/json", b"{}", True),
        ("application/json", json.dumps(missing_usage).encode(), True),
        ("application/json", json.dumps(invalid_text).encode(), True),
        ("application/json", json.dumps(string_token_count).encode(), True),
        ("application/json", json.dumps(boolean_token_count).encode(), True),
        ("application/json", json.dumps(non_object_content).encode(), True),
    ]


def test_text_joins_text_blocks_and_skips_thinking() -> None:
    content = [
        {
            "type": "thinking",
            "thinking": "private thinking sentinel",
            "signature": "fake-signature",
        },
        {"type": "text", "text": "first "},
        {"type": "text", "text": "second"},
    ]

    with _adapter(_json_handler(_message_payload(content=content))) as adapter:
        reply = adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert reply.text == "first second"


def test_refusal_omits_invalid_category_and_missing_request_id() -> None:
    payload = _message_payload(
        stop_reason="refusal",
        stop_details={
            "type": "refusal",
            "category": "not a category",
            "explanation": "private refusal explanation sentinel",
        },
    )

    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=payload)

    with (
        _adapter(handle) as adapter,
        pytest.raises(LLMRefusalError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert raised.value.category is None
    assert raised.value.request_id is None
    assert str(raised.value) == "Claude refused the request."
    assert "private refusal explanation sentinel" not in str(raised.value)


@pytest.mark.parametrize(
    ("content_type", "body", "is_message"),
    _malformed_response_cases(),
)
def test_malformed_success_response_is_safe_error(
    content_type: str,
    body: bytes,
    is_message: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected_request_id = "req_malformed" if is_message else None
    expected_message = (
        "Anthropic returned a malformed response (request_id req_malformed)."
        if is_message
        else "Anthropic returned a malformed response."
    )

    with (
        caplog.at_level(logging.WARNING),
        _adapter(_raw_json_handler(content_type, body)) as adapter,
        pytest.raises(LLMResponseError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert str(raised.value) == expected_message
    assert raised.value.request_id == expected_request_id
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert body.decode() not in str(raised.value)
    assert body.decode() not in caplog.text
    assert _MALFORMED_BODY_SENTINEL not in str(raised.value)
    assert _MALFORMED_BODY_SENTINEL not in caplog.text

    failure_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "training_season.claude"
        and record.getMessage().startswith("Claude call failed:")
    ]
    assert len(failure_lines) == 1
    assert (
        f"Claude call failed: job=plan model=claude-opus-5-5 "
        f"error=LLMResponseError status=None error_type=None "
        f"request_id={expected_request_id} duration_ms="
    ) in failure_lines[0]


def test_malformed_message_omits_missing_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _message_payload(
        content=[{"type": "text", "text": _MALFORMED_BODY_SENTINEL}]
    )
    payload.pop("usage")
    body = json.dumps(payload).encode()

    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            content=body,
        )

    with (
        caplog.at_level(logging.WARNING),
        _adapter(handle) as adapter,
        pytest.raises(LLMResponseError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert str(raised.value) == "Anthropic returned a malformed response."
    assert raised.value.request_id is None
    assert raised.value.__context__ is None
    assert _MALFORMED_BODY_SENTINEL not in str(raised.value)
    assert _MALFORMED_BODY_SENTINEL not in caplog.text
    failure_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "training_season.claude"
        and record.getMessage().startswith("Claude call failed:")
    ]
    assert len(failure_lines) == 1
    assert (
        "Claude call failed: job=plan model=claude-opus-5-5 "
        "error=LLMResponseError status=None error_type=None "
        "request_id=None duration_ms="
    ) in failure_lines[0]
