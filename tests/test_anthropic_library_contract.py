"""Offline contract tests for the pinned Anthropic SDK."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal, get_args, get_origin, get_type_hints

import anthropic
import httpx2
import pytest
from anthropic._constants import (
    DEFAULT_MAX_RETRIES,
    INITIAL_RETRY_DELAY,
    MAX_RETRY_DELAY,
)
from anthropic.resources.messages.messages import DEPRECATED_MODELS
from anthropic.types import CacheCreation, OutputConfigParam, Usage

_FAKE_KEY = "sk-ant-test-not-a-real-key"
_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_LOG",
    "ANTHROPIC_PROFILE",
    "CLAUDE_PLAN_MODEL",
    "CLAUDE_PLAN_EFFORT",
    "CLAUDE_CHAT_MODEL",
    "CLAUDE_CHAT_EFFORT",
)
RequestHandler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture(autouse=True)
def clear_anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in _ENVIRONMENT_NAMES:
        monkeypatch.delenv(variable, raising=False)


def _message_payload() -> dict[str, object]:
    return {
        "id": "msg_contract",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "safe response sentinel"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _handler() -> RequestHandler:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"request-id": "req_contract"},
            json=_message_payload(),
        )

    return handle


@contextmanager
def _sdk_client(
    handler: RequestHandler | None = None,
) -> Iterator[anthropic.Anthropic]:
    http_client = (
        None
        if handler is None
        else httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    client = anthropic.Anthropic(
        api_key=_FAKE_KEY,
        open_telemetry=False,
        http_client=http_client,
    )
    try:
        yield client
    finally:
        client.close()


def _status_error(
    client: anthropic.Anthropic,
    status_code: int,
    error_type: str = "invalid_request_error",
) -> anthropic.APIStatusError:
    response = httpx2.Response(
        status_code,
        headers={"request-id": "req_contract"},
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    body = {"type": "error", "error": {"type": error_type, "message": "safe"}}
    return client._make_status_error(
        "safe SDK error",
        body=body,
        response=response,
    )


def test_anthropic_version_is_pinned() -> None:
    assert anthropic.__version__ == "2.3.0"


def test_retry_defaults() -> None:
    assert DEFAULT_MAX_RETRIES == 2
    assert INITIAL_RETRY_DELAY == 0.5
    assert MAX_RETRY_DELAY == 8.0


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (408, True),
        (409, True),
        (429, True),
        (500, True),
        (503, True),
        (529, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (413, False),
        (422, False),
    ],
)
def test_retry_status_policy(status_code: int, expected: bool) -> None:
    with _sdk_client() as client:
        response = httpx2.Response(
            status_code,
            request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"),
        )

        assert client._should_retry(response) is expected


@pytest.mark.parametrize(
    ("status_code", "expected_type"),
    [
        (400, anthropic.BadRequestError),
        (401, anthropic.AuthenticationError),
        (403, anthropic.PermissionDeniedError),
        (404, anthropic.NotFoundError),
        (413, anthropic.RequestTooLargeError),
        (422, anthropic.UnprocessableEntityError),
        (429, anthropic.RateLimitError),
        (500, anthropic.InternalServerError),
        (503, anthropic.InternalServerError),
        (529, anthropic.OverloadedError),
    ],
)
def test_status_error_class_map(
    status_code: int,
    expected_type: type[anthropic.APIStatusError],
) -> None:
    with _sdk_client() as client:
        error = _status_error(client, status_code)

    assert type(error) is expected_type


def test_nonstreaming_guard_threshold_and_bypass() -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            headers={"request-id": "req_contract"},
            json=_message_payload(),
        )

    with _sdk_client(handle) as client:
        with pytest.raises(ValueError, match="Streaming is required"):
            client.messages.create(
                model="claude-test-model",
                max_tokens=21_334,
                system="safe system",
                messages=[{"role": "user", "content": "safe turn"}],
            )
        assert requests == []

        client.messages.create(
            model="claude-test-model",
            max_tokens=21_333,
            system="safe system",
            messages=[{"role": "user", "content": "safe turn"}],
        )
        client.messages.create(
            model="claude-test-model",
            max_tokens=32_000,
            system="safe system",
            messages=[{"role": "user", "content": "safe turn"}],
            timeout=anthropic.Timeout(900.0, connect=5.0),
        )

    assert len(requests) == 2


def test_output_config_effort_values() -> None:
    effort_annotation = get_type_hints(OutputConfigParam)["effort"]
    literal = next(
        item for item in get_args(effort_annotation) if get_origin(item) is Literal
    )

    assert get_args(literal) == ("low", "medium", "high", "xhigh", "max")


def test_default_models_not_deprecated() -> None:
    assert "claude-opus-5-5" not in DEPRECATED_MODELS
    assert "claude-sonnet-5-5" not in DEPRECATED_MODELS


def test_usage_fields_used_by_adapter_exist() -> None:
    assert {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_creation",
        "service_tier",
        "speed",
        "inference_geo",
    } <= Usage.model_fields.keys()
    assert {
        "ephemeral_5m_input_tokens",
        "ephemeral_1h_input_tokens",
    } <= CacheCreation.model_fields.keys()


def test_status_error_exposes_request_id_and_type() -> None:
    with _sdk_client() as client:
        error = _status_error(client, 400, "invalid_request_error")

    assert error.status_code == 400
    assert error.request_id == "req_contract"
    assert error.type == "invalid_request_error"


def test_message_request_id_attribute() -> None:
    with _sdk_client(_handler()) as client:
        message = client.messages.create(
            model="claude-opus-5-5",
            max_tokens=10,
            system="safe system",
            messages=[{"role": "user", "content": "safe turn"}],
        )

    assert message._request_id == "req_contract"


def test_explicit_api_key_wins_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_headers: list[httpx2.Headers] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        observed_headers.append(request.headers)
        return httpx2.Response(
            200,
            headers={"request-id": "req_contract"},
            json=_message_payload(),
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    with _sdk_client(handle) as client:
        client.messages.create(
            model="claude-opus-5-5",
            max_tokens=10,
            system="safe system",
            messages=[{"role": "user", "content": "safe turn"}],
        )

    headers = observed_headers[0]
    credential = headers.get("x-api-key") or headers.get("authorization")
    assert credential in {_FAKE_KEY, f"Bearer {_FAKE_KEY}"}


def test_sdk_debug_log_contains_request_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    prompt_sentinel = "private prompt debug sentinel"
    payload = _message_payload()
    payload["content"] = [{"type": "text", "text": "private response debug sentinel"}]

    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"request-id": "req_contract"},
            json=payload,
        )

    caplog.set_level(logging.DEBUG)
    logging.getLogger("anthropic").setLevel(logging.DEBUG)
    with _sdk_client(handle) as client:
        client.messages.create(
            model="claude-opus-5-5",
            max_tokens=10,
            system=prompt_sentinel,
            messages=[{"role": "user", "content": prompt_sentinel}],
        )

    assert prompt_sentinel in caplog.text
    assert "private response debug sentinel" not in caplog.text
    assert _FAKE_KEY not in caplog.text
