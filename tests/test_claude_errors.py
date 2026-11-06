"""Tests for the Anthropic adapter error mapping and retries."""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import anthropic
import httpx2
import pytest
from anthropic.resources.messages import Messages

from claude_fakes import (
    _SERVER_MESSAGE,
    _adapter,
    _clear_anthropic_variables,
    _error_payload,
    _json_handler,
    _message_payload,
    _turns,
)
from training_season.llm_base import (
    LLMAuthenticationError,
    LLMError,
    LLMJob,
    LLMRateLimitedError,
    LLMRequestRejectedError,
    LLMResponseError,
    LLMUnavailableError,
)


@pytest.fixture(autouse=True)
def clear_anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_anthropic_variables(monkeypatch)


def test_overloaded_then_success_is_retried() -> None:
    requests: list[httpx2.Request] = []
    attempts = 0

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        requests.append(request)
        attempts += 1
        if attempts < 3:
            return httpx2.Response(
                529,
                headers={"request-id": f"req_{attempts}", "retry-after-ms": "1"},
                json=_error_payload("overloaded_error"),
            )
        request_body = json.loads(request.content)
        return httpx2.Response(
            200,
            headers={"request-id": "req_success"},
            json=_message_payload(model=str(request_body["model"])),
        )

    with _adapter(handle) as adapter:
        reply = adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert attempts == 3
    assert len(requests) == 3
    assert reply.request_id == "req_success"


def test_overloaded_three_times_raises_unavailable() -> None:
    requests: list[httpx2.Request] = []
    handler = _json_handler(
        _error_payload("overloaded_error"),
        status_code=529,
        headers={"retry-after-ms": "1"},
        requests=requests,
    )

    with _adapter(handler) as adapter, pytest.raises(LLMUnavailableError) as raised:
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert len(requests) == 3
    assert raised.value.status_code == 529
    assert raised.value.error_type == "overloaded_error"
    assert raised.value.request_id == "req_test"


def test_bad_request_is_not_retried() -> None:
    requests: list[httpx2.Request] = []
    handler = _json_handler(
        _error_payload("invalid_request_error"),
        status_code=400,
        requests=requests,
    )

    with _adapter(handler) as adapter, pytest.raises(LLMRequestRejectedError):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert len(requests) == 1


def test_connection_error_retried_then_unavailable() -> None:
    attempts = 0

    def handle(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        raise httpx2.ConnectError("fake connection error")

    with _adapter(handle) as adapter, pytest.raises(LLMUnavailableError) as raised:
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert attempts == 3
    assert raised.value.status_code is None
    assert raised.value.error_type == "connection"
    assert str(raised.value) == "Could not reach Anthropic after retries (connection)."


def test_timeout_retried_then_unavailable() -> None:
    attempts = 0

    def handle(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        raise httpx2.ReadTimeout("fake read timeout")

    with _adapter(handle) as adapter, pytest.raises(LLMUnavailableError) as raised:
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert attempts == 3
    assert raised.value.status_code is None
    assert raised.value.error_type == "timeout"
    assert str(raised.value) == "Could not reach Anthropic after retries (timeout)."


@pytest.mark.parametrize(
    ("status_code", "error_type", "expected_error", "expected_attempts"),
    [
        (400, "invalid_request_error", LLMRequestRejectedError, 1),
        (404, "not_found_error", LLMRequestRejectedError, 1),
        (413, "request_too_large", LLMRequestRejectedError, 1),
        (422, "unprocessable_entity", LLMRequestRejectedError, 1),
        (401, "authentication_error", LLMAuthenticationError, 1),
        (403, "permission_error", LLMAuthenticationError, 1),
        (408, "timeout_error", LLMUnavailableError, 3),
        (409, "conflict_error", LLMUnavailableError, 3),
        (429, "rate_limit_error", LLMRateLimitedError, 3),
        (500, "internal_server_error", LLMUnavailableError, 3),
        (503, "service_unavailable", LLMUnavailableError, 3),
        (529, "overloaded_error", LLMUnavailableError, 3),
    ],
)
def test_status_maps_to_adapter_error(
    status_code: int,
    error_type: str,
    expected_error: type[LLMError],
    expected_attempts: int,
) -> None:
    requests: list[httpx2.Request] = []
    handler = _json_handler(
        _error_payload(error_type),
        status_code=status_code,
        headers={"retry-after-ms": "1"},
        requests=requests,
    )

    with _adapter(handler) as adapter, pytest.raises(expected_error) as raised:
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert len(requests) == expected_attempts
    assert raised.value.status_code == status_code
    assert raised.value.error_type == error_type
    assert raised.value.request_id == "req_test"
    phrases: dict[type[LLMError], str] = {
        LLMAuthenticationError: "rejected the API key",
        LLMRateLimitedError: "rate-limited the request after retries",
        LLMUnavailableError: "is unavailable after retries",
        LLMRequestRejectedError: "rejected the request",
    }
    assert str(raised.value) == (
        f"Anthropic {phrases[expected_error]} "
        f"(HTTP {status_code}, {error_type}, request_id req_test)."
    )


def test_error_message_excludes_server_text() -> None:
    handler = _json_handler(
        _error_payload("invalid_request_error"),
        status_code=400,
    )

    with (
        _adapter(handler) as adapter,
        pytest.raises(LLMRequestRejectedError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert _SERVER_MESSAGE not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_sdk_response_validation_error_maps_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sdk_response = httpx2.Response(
        200,
        headers={"request-id": "req_validation"},
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    sdk_error = anthropic.APIResponseValidationError(
        response=sdk_response,
        body={"message": _SERVER_MESSAGE},
    )

    with (
        caplog.at_level(logging.WARNING),
        _adapter(_json_handler(_message_payload())) as adapter,
        patch.object(Messages, "create", side_effect=sdk_error),
        pytest.raises(LLMResponseError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert str(raised.value) == (
        "Anthropic returned a response that does not match the SDK schema "
        "(request_id req_validation)."
    )
    assert raised.value.status_code == 200
    assert raised.value.request_id == "req_validation"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert _SERVER_MESSAGE not in str(raised.value)
    assert _SERVER_MESSAGE not in caplog.text
    failure_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "training_season.claude"
        and record.getMessage().startswith("Claude call failed:")
    ]
    assert len(failure_lines) == 1
    assert (
        "Claude call failed: job=plan model=claude-opus-5-5 "
        "error=LLMResponseError status=200 error_type=None "
        "request_id=req_validation duration_ms="
    ) in failure_lines[0]
