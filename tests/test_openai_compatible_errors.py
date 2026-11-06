"""Tests for the OpenAI-compatible retries and error mapping in openai_compatible.py."""

from __future__ import annotations

import logging

import httpx2
import pytest

from openai_compatible_fakes import (
    _MALFORMED_BODY_SENTINEL,
    _SERVER_MESSAGE,
    _adapter,
    _choice_error_payload,
    _clear_openai_compatible_variables,
    _completion,
    _error_payload,
    _json_handler,
    _top_level_error_body,
    _turns,
)
from training_season.llm_base import (
    LLMAuthenticationError,
    LLMError,
    LLMJob,
    LLMRateLimitedError,
    LLMRequestRejectedError,
    LLMUnavailableError,
)


@pytest.fixture(autouse=True)
def clear_openai_compatible_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_openai_compatible_variables(monkeypatch)


@pytest.mark.parametrize(
    ("status_code", "error_class", "message", "attempts"),
    [
        (
            401,
            LLMAuthenticationError,
            "The OpenAI-compatible provider rejected the API key",
            1,
        ),
        *(
            (
                status_code,
                LLMRequestRejectedError,
                "The OpenAI-compatible provider rejected the request",
                1,
            )
            for status_code in (400, 402, 403, 404, 413, 422)
        ),
        *(
            (
                status_code,
                LLMUnavailableError,
                "The OpenAI-compatible provider is unavailable after retries",
                3,
            )
            for status_code in (408, 409, 500, 502, 503)
        ),
        (
            429,
            LLMRateLimitedError,
            "The OpenAI-compatible provider rate-limited the request after retries",
            3,
        ),
    ],
)
def test_status_errors(
    status_code: int,
    error_class: type[LLMError],
    message: str,
    attempts: int,
) -> None:
    requests: list[httpx2.Request] = []
    sleeps: list[float] = []
    handler = _json_handler(
        _error_payload(status_code),
        status_code=status_code,
        requests=requests,
    )

    with (
        _adapter(handler, sleeps=sleeps) as adapter,
        pytest.raises(error_class) as error,
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert type(error.value) is error_class
    assert str(error.value) == f"{message} (HTTP {status_code}, provider_error)."
    assert error.value.status_code == status_code
    assert error.value.error_type == "provider_error"
    assert len(requests) == attempts
    assert len(sleeps) == attempts - 1


def test_retry_after_header_sets_sleep() -> None:
    sleeps: list[float] = []
    handler = _json_handler(
        _error_payload(429), status_code=429, headers={"retry-after": "2"}
    )

    with (
        _adapter(handler, sleeps=sleeps) as adapter,
        pytest.raises(LLMRateLimitedError),
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert sleeps == [2.0, 2.0]


def test_long_retry_after_fails_at_once() -> None:
    requests: list[httpx2.Request] = []
    sleeps: list[float] = []
    handler = _json_handler(
        _error_payload(429),
        status_code=429,
        headers={"retry-after": "120"},
        requests=requests,
    )

    with (
        _adapter(handler, sleeps=sleeps) as adapter,
        pytest.raises(LLMRateLimitedError),
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert len(requests) == 1
    assert sleeps == []


@pytest.mark.parametrize("retry_after", [None, "0", "soon", "nan"])
def test_backoff_without_usable_retry_after(retry_after: str | None) -> None:
    sleeps: list[float] = []
    headers = None if retry_after is None else {"retry-after": retry_after}
    handler = _json_handler(_error_payload(503), status_code=503, headers=headers)

    with (
        _adapter(handler, sleeps=sleeps) as adapter,
        pytest.raises(LLMUnavailableError),
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert len(sleeps) == 2
    assert 0.375 <= sleeps[0] <= 0.5
    assert 0.75 <= sleeps[1] <= 1.0


def test_retry_then_success() -> None:
    responses = [
        httpx2.Response(503, json=_error_payload(503)),
        httpx2.Response(200, json=_completion(content="after retry")),
    ]

    with _adapter(lambda _request: responses.pop(0)) as adapter:
        reply = adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert reply.text == "after retry"
    assert responses == []


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (httpx2.ConnectError("connect failed"), "connection"),
        (httpx2.ReadTimeout("read timed out"), "timeout"),
        (httpx2.DecodingError("bad content encoding"), "connection"),
    ],
)
def test_transport_errors_retry_then_fail(
    error: httpx2.RequestError,
    error_type: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    def handle(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        raise error

    with (
        caplog.at_level(logging.WARNING),
        _adapter(handle) as adapter,
        pytest.raises(LLMUnavailableError) as raised,
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert caplog.text.count("OpenAI-compatible call failed") == 1
    assert calls == 3
    assert raised.value.error_type == error_type
    assert raised.value.status_code is None
    assert str(raised.value) == (
        f"Could not reach the OpenAI-compatible provider after retries ({error_type})."
    )


@pytest.mark.parametrize(
    ("code", "error_class", "status_code", "attempts"),
    [
        (429, LLMRateLimitedError, 429, 3),
        (400, LLMRequestRejectedError, 400, 1),
        (None, LLMUnavailableError, 502, 3),
        ("429", LLMUnavailableError, 502, 3),
    ],
)
def test_choice_error_maps_code_and_discards_content(
    code: object,
    error_class: type[LLMError],
    status_code: int,
    attempts: int,
) -> None:
    requests: list[httpx2.Request] = []
    payload = _choice_error_payload(code, content=_MALFORMED_BODY_SENTINEL)

    with (
        _adapter(_json_handler(payload, requests=requests)) as adapter,
        pytest.raises(error_class) as error,
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert type(error.value) is error_class
    assert error.value.status_code == status_code
    assert error.value.error_type == "provider_error"
    assert len(requests) == attempts
    assert _MALFORMED_BODY_SENTINEL not in str(error.value)
    assert _SERVER_MESSAGE not in str(error.value)


@pytest.mark.parametrize(
    ("code", "error_class", "status_code", "attempts"),
    [
        (504, LLMUnavailableError, 504, 3),
        (429, LLMRateLimitedError, 429, 3),
        (401, LLMAuthenticationError, 401, 1),
        (400, LLMRequestRejectedError, 400, 1),
        (None, LLMUnavailableError, 502, 3),
        ("504", LLMUnavailableError, 502, 3),
        (True, LLMUnavailableError, 502, 3),
        (200, LLMUnavailableError, 502, 3),
    ],
)
def test_top_level_error_in_200_body_maps_code(
    code: object,
    error_class: type[LLMError],
    status_code: int,
    attempts: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            content=_top_level_error_body(code),
        )

    with (
        caplog.at_level(logging.WARNING),
        _adapter(handle) as adapter,
        pytest.raises(error_class) as error,
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert type(error.value) is error_class
    assert error.value.status_code == status_code
    assert error.value.error_type == "timeout"
    assert error.value.request_id == "gen-1"
    assert len(requests) == attempts
    assert caplog.text.count("OpenAI-compatible call failed") == 1
    assert _SERVER_MESSAGE not in caplog.text
    assert _SERVER_MESSAGE not in str(error.value)
