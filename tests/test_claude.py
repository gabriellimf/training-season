"""Tests for the Anthropic adapter."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

import anthropic
import httpx2
import pytest
from anthropic.resources.messages import Messages

from conftest import NetworkBlockedError
from training_season.claude import (
    ANTHROPIC_API_KEY_ENV,
    CLAUDE_CHAT_EFFORT_ENV,
    CLAUDE_CHAT_MODEL_ENV,
    CLAUDE_PLAN_EFFORT_ENV,
    CLAUDE_PLAN_MODEL_ENV,
    ClaudeAdapter,
    ClaudeClientFactory,
    ClaudeConfig,
    claude_config_from_environment,
    new_anthropic_client,
)
from training_season.llm_base import (
    ChatTurn,
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMJob,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMRequestRejectedError,
    LLMResponseError,
    LLMUnavailableError,
)

_FAKE_KEY = "sk-ant-test-not-a-real-key"
_ENVIRONMENT_NAMES = (
    ANTHROPIC_API_KEY_ENV,
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_LOG",
    "ANTHROPIC_PROFILE",
    CLAUDE_PLAN_MODEL_ENV,
    CLAUDE_PLAN_EFFORT_ENV,
    CLAUDE_CHAT_MODEL_ENV,
    CLAUDE_CHAT_EFFORT_ENV,
)
_SERVER_MESSAGE = "private-server-message-sentinel"
_MALFORMED_BODY_SENTINEL = "private malformed response body sentinel"
RequestHandler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture(autouse=True)
def clear_anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in _ENVIRONMENT_NAMES:
        monkeypatch.delenv(variable, raising=False)


def _config(**overrides: str) -> ClaudeConfig:
    environment = {ANTHROPIC_API_KEY_ENV: _FAKE_KEY}
    environment.update(overrides)
    return claude_config_from_environment(environment)


def _message_payload(
    *,
    model: str = "claude-opus-5-5",
    content: Sequence[Mapping[str, object]] | None = None,
    stop_reason: str = "end_turn",
    stop_details: Mapping[str, object] | None = None,
    usage: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": (
            [{"type": "text", "text": "safe response"}]
            if content is None
            else list(content)
        ),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": (
            {"input_tokens": 100, "output_tokens": 50} if usage is None else usage
        ),
    }
    if stop_details is not None:
        payload["stop_details"] = dict(stop_details)
    return payload


def _error_payload(
    error_type: str, message: str = _SERVER_MESSAGE
) -> dict[str, object]:
    return {
        "type": "error",
        "error": {"type": error_type, "message": message},
    }


def _json_handler(
    payload: Mapping[str, object],
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    requests: list[httpx2.Request] | None = None,
) -> RequestHandler:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(request)
        response_headers = {"request-id": "req_test"}
        if headers is not None:
            response_headers.update(headers)
        return httpx2.Response(status_code, headers=response_headers, json=payload)

    return handle


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


def _model_echo_handler(requests: list[httpx2.Request]) -> RequestHandler:
    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        request_body = json.loads(request.content)
        return httpx2.Response(
            200,
            headers={"request-id": "req_test"},
            json=_message_payload(model=str(request_body["model"])),
        )

    return handle


def _mock_factory(handler: RequestHandler) -> ClaudeClientFactory:
    def factory(config: ClaudeConfig) -> anthropic.Anthropic:
        client = httpx2.Client(transport=httpx2.MockTransport(handler))
        return new_anthropic_client(config, http_client=client)

    return factory


@contextmanager
def _adapter(
    handler: RequestHandler,
    config: ClaudeConfig | None = None,
) -> Iterator[ClaudeAdapter]:
    adapter = ClaudeAdapter(
        _config() if config is None else config,
        factory=_mock_factory(handler),
    )
    try:
        yield adapter
    finally:
        adapter.close()


def _turns() -> list[ChatTurn]:
    return [ChatTurn("user", "safe user turn")]


def test_config_defaults_with_only_key() -> None:
    config = _config()

    assert config.plan.model == "claude-opus-5-5"
    assert config.plan.effort == "medium"
    assert config.plan.max_tokens == 32_000
    assert config.plan.timeout.read == 900.0
    assert config.plan.timeout.connect == 5.0
    assert config.chat.model == "claude-sonnet-5-5"
    assert config.chat.effort == "medium"
    assert config.chat.max_tokens == 16_000
    assert config.chat.timeout.read == 600.0
    assert config.chat.timeout.connect == 5.0


def test_config_reads_os_environ_when_environ_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, _FAKE_KEY)
    monkeypatch.setenv(CLAUDE_CHAT_MODEL_ENV, "claude-test-chat")

    config = claude_config_from_environment()

    assert config.chat.model == "claude-test-chat"


def test_missing_api_key_fails_with_clear_message() -> None:
    with pytest.raises(LLMConfigurationError) as raised:
        claude_config_from_environment({})

    assert str(raised.value) == (
        "ANTHROPIC_API_KEY is not set. Set it in the environment or the Compose "
        "env file before starting."
    )


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_api_key_fails(value: str) -> None:
    with pytest.raises(LLMConfigurationError, match="ANTHROPIC_API_KEY"):
        claude_config_from_environment({ANTHROPIC_API_KEY_ENV: value})


def test_api_key_is_stripped_and_hidden_from_repr() -> None:
    config = claude_config_from_environment({ANTHROPIC_API_KEY_ENV: f" {_FAKE_KEY} "})

    assert config.api_key == _FAKE_KEY
    assert _FAKE_KEY not in repr(config)


def test_empty_model_and_effort_take_defaults() -> None:
    config = _config(
        **{
            CLAUDE_PLAN_MODEL_ENV: "",
            CLAUDE_PLAN_EFFORT_ENV: " ",
            CLAUDE_CHAT_MODEL_ENV: "",
            CLAUDE_CHAT_EFFORT_ENV: "",
        }
    )

    assert config.plan.model == "claude-opus-5-5"
    assert config.plan.effort == "medium"
    assert config.chat.model == "claude-sonnet-5-5"
    assert config.chat.effort == "medium"


@pytest.mark.parametrize(
    "value",
    ["claude test", "cláude-test", "x" * 129],
)
def test_invalid_model_fails(value: str) -> None:
    with pytest.raises(LLMConfigurationError) as raised:
        _config(**{CLAUDE_PLAN_MODEL_ENV: value})

    assert CLAUDE_PLAN_MODEL_ENV in str(raised.value)
    if len(value) <= 64:
        assert f"Got: {value!r}." in str(raised.value)
    else:
        assert f"Got a value of {len(value)} characters." in str(raised.value)
        assert value not in str(raised.value)


@pytest.mark.parametrize("value", ["MEDIUM", "extreme"])
def test_invalid_effort_fails_and_lists_allowed_values(value: str) -> None:
    with pytest.raises(LLMConfigurationError) as raised:
        _config(**{CLAUDE_CHAT_EFFORT_ENV: value})

    assert "low, medium, high, xhigh, max, none" in str(raised.value)
    assert f"Got: {value!r}." in str(raised.value)


@pytest.mark.parametrize(
    "variable",
    [CLAUDE_PLAN_MODEL_ENV, CLAUDE_CHAT_EFFORT_ENV],
)
def test_invalid_config_does_not_echo_api_key(
    variable: str,
) -> None:
    value = f"invalid-{_FAKE_KEY}-config"

    with pytest.raises(LLMConfigurationError) as raised:
        _config(**{variable: value})

    assert f"Got a value of {len(value)} characters." in str(raised.value)
    assert value not in str(raised.value)
    assert _FAKE_KEY not in str(raised.value)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (CLAUDE_PLAN_MODEL_ENV, "x" * 129),
        (CLAUDE_CHAT_EFFORT_ENV, "x" * 65),
    ],
)
def test_invalid_long_config_value_is_not_echoed(
    variable: str,
    value: str,
) -> None:
    with pytest.raises(LLMConfigurationError) as raised:
        _config(**{variable: value})

    assert f"Got a value of {len(value)} characters." in str(raised.value)
    assert value not in str(raised.value)


def test_model_swap_is_one_variable() -> None:
    requests: list[httpx2.Request] = []
    config = _config(**{CLAUDE_PLAN_MODEL_ENV: "claude-test-model"})

    with _adapter(_model_echo_handler(requests), config) as adapter:
        plan = adapter.complete(LLMJob.PLAN, system="system", turns=_turns())
        chat = adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    plan_body = json.loads(requests[0].content)
    chat_body = json.loads(requests[1].content)
    assert plan.model == "claude-test-model"
    assert plan_body["model"] == "claude-test-model"
    assert chat.model == "claude-sonnet-5-5"
    assert chat_body["model"] == "claude-sonnet-5-5"


def test_plan_request_body() -> None:
    requests: list[httpx2.Request] = []
    handler = _json_handler(
        _message_payload(),
        requests=requests,
    )

    with _adapter(handler) as adapter:
        adapter.complete(LLMJob.PLAN, system="safe system", turns=_turns())

    body = json.loads(requests[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 32_000
    assert body["system"] == "safe system"
    assert body["messages"] == [{"role": "user", "content": "safe user turn"}]
    assert body["output_config"] == {"effort": "medium"}
    assert "thinking" not in body
    assert "stream" not in body
    assert "tools" not in body


def test_chat_request_sends_history_in_order() -> None:
    requests: list[httpx2.Request] = []
    handler = _json_handler(
        _message_payload(model="claude-sonnet-5-5"),
        requests=requests,
    )

    with _adapter(handler) as adapter:
        adapter.complete(
            LLMJob.CHAT,
            system="safe system",
            turns=[
                ChatTurn("user", "first user turn"),
                ChatTurn("assistant", "first assistant turn"),
                ChatTurn("user", "second user turn"),
            ],
        )

    body = json.loads(requests[0].content)
    assert body["messages"] == [
        {"role": "user", "content": "first user turn"},
        {"role": "assistant", "content": "first assistant turn"},
        {"role": "user", "content": "second user turn"},
    ]


def test_effort_none_omits_output_config() -> None:
    requests: list[httpx2.Request] = []
    config = _config(**{CLAUDE_PLAN_EFFORT_ENV: "none"})

    with _adapter(
        _json_handler(_message_payload(), requests=requests), config
    ) as adapter:
        adapter.complete(LLMJob.PLAN, system="safe system", turns=_turns())

    assert "output_config" not in json.loads(requests[0].content)


def test_per_call_timeout_is_sent() -> None:
    requests: list[httpx2.Request] = []
    handler = _model_echo_handler(requests)

    with _adapter(handler) as adapter:
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert requests[0].headers["x-stainless-timeout"] == "900.0"
    assert requests[1].headers["x-stainless-timeout"] == "600.0"


def test_plan_max_tokens_does_not_trigger_streaming_guard() -> None:
    requests: list[httpx2.Request] = []
    handler = _json_handler(_message_payload(), requests=requests)

    with _adapter(handler) as adapter:
        reply = adapter.complete(
            LLMJob.PLAN,
            system="safe system",
            turns=_turns(),
        )

    assert reply.text == "safe response"
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("system", "turns"),
    [
        ("", [ChatTurn("user", "valid turn")]),
        ("valid system", []),
        ("valid system", [ChatTurn("user", "  ")]),
        ("valid system", [ChatTurn("assistant", "last turn is not user")]),
    ],
)
def test_input_validation(
    system: str,
    turns: list[ChatTurn],
) -> None:
    requests: list[httpx2.Request] = []

    with (
        _adapter(_json_handler(_message_payload(), requests=requests)) as adapter,
        pytest.raises(ValueError, match=r"LLM|last LLM"),
    ):
        adapter.complete(LLMJob.PLAN, system=system, turns=turns)

    assert requests == []


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


def test_reply_has_request_id_stop_reason_model_usage() -> None:
    payload = _message_payload(
        model="claude-sonnet-5-5",
        usage={
            "input_tokens": 101,
            "output_tokens": 52,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 11,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 6,
                "ephemeral_1h_input_tokens": 5,
            },
        },
    )

    with _adapter(_json_handler(payload)) as adapter:
        reply = adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert reply.job is LLMJob.CHAT
    assert reply.model == "claude-sonnet-5-5"
    assert reply.stop_reason == "end_turn"
    assert reply.request_id == "req_test"
    assert reply.usage.input_tokens == 101
    assert reply.usage.output_tokens == 52
    assert reply.usage.cache_read_input_tokens == 7
    assert reply.usage.cache_write_input_tokens == 11
    assert isinstance(reply.duration_seconds, float)


def test_max_tokens_stop_raises_response_error_and_logs_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _message_payload(
        content=[{"type": "text", "text": "partial response sentinel"}],
        stop_reason="max_tokens",
    )

    with (
        caplog.at_level(logging.INFO),
        _adapter(_json_handler(payload)) as adapter,
        pytest.raises(LLMResponseError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert raised.value.stop_reason == "max_tokens"
    assert str(raised.value) == (
        "Claude stopped at the max_tokens limit (request_id req_test)."
    )
    assert "partial response sentinel" not in str(raised.value)
    assert "input_tokens=100" in caplog.text
    assert "cost_usd=0.001400" in caplog.text


def test_context_window_stop_raises_response_error() -> None:
    payload = _message_payload(stop_reason="model_context_window_exceeded")

    with (
        _adapter(_json_handler(payload)) as adapter,
        pytest.raises(LLMResponseError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert raised.value.stop_reason == "model_context_window_exceeded"
    assert raised.value.request_id == "req_test"
    assert str(raised.value) == (
        "Claude exceeded the model context window (request_id req_test)."
    )


def test_refusal_raises_with_category_and_no_explanation() -> None:
    payload = _message_payload(
        stop_reason="refusal",
        stop_details={
            "type": "refusal",
            "category": "cyber",
            "explanation": "private refusal explanation sentinel",
        },
    )

    with (
        _adapter(_json_handler(payload)) as adapter,
        pytest.raises(LLMRefusalError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert raised.value.category == "cyber"
    assert str(raised.value) == (
        "Claude refused the request (category cyber, request_id req_test)."
    )
    assert "private refusal explanation sentinel" not in str(raised.value)
    assert raised.value.request_id == "req_test"


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


def test_end_turn_without_text_raises_response_error() -> None:
    payload = _message_payload(content=[])

    with (
        _adapter(_json_handler(payload)) as adapter,
        pytest.raises(LLMResponseError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert raised.value.stop_reason == "end_turn"
    assert raised.value.request_id == "req_test"
    assert str(raised.value) == "Claude returned no text (request_id req_test)."


def test_unknown_stop_reason_raises_response_error() -> None:
    payload = _message_payload(stop_reason="pause_turn")

    with (
        _adapter(_json_handler(payload)) as adapter,
        pytest.raises(LLMResponseError) as raised,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert raised.value.stop_reason == "pause_turn"
    assert raised.value.request_id == "req_test"
    assert str(raised.value) == (
        "Claude returned an unsupported stop reason (request_id req_test)."
    )


def test_stop_sequence_without_text_returns_empty_reply() -> None:
    payload = _message_payload(content=[], stop_reason="stop_sequence")

    with _adapter(_json_handler(payload)) as adapter:
        reply = adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert reply.text == ""
    assert reply.stop_reason == "stop_sequence"


def test_client_built_with_max_retries_2_and_otel_off() -> None:
    with patch("training_season.claude.anthropic.Anthropic") as sdk_client:
        new_anthropic_client(_config())

    sdk_client.assert_called_once_with(
        api_key=_FAKE_KEY,
        max_retries=2,
        open_telemetry=False,
    )


@pytest.mark.parametrize(
    ("current_level", "expected_level"),
    [
        (logging.WARNING, logging.WARNING),
        (logging.DEBUG, logging.INFO),
        (logging.NOTSET, logging.INFO),
    ],
)
def test_client_logger_levels_are_only_raised(
    current_level: int,
    expected_level: int,
) -> None:
    logger_names = ("anthropic", "httpx2", "httpcore2")
    for logger_name in logger_names:
        logging.getLogger(logger_name).setLevel(current_level)

    with patch("training_season.claude.anthropic.Anthropic"):
        new_anthropic_client(_config())

    assert all(
        logging.getLogger(logger_name).level == expected_level
        for logger_name in logger_names
    )


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


def test_success_log_line_has_expected_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.INFO),
        _adapter(_json_handler(_message_payload())) as adapter,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert (
        "Claude call: job=plan model=claude-opus-5-5 effort=medium "
        "stop_reason=end_turn input_tokens=100 output_tokens=50 "
        "cache_read_tokens=0 cache_write_tokens=0 cost_usd=0.001400 duration_ms="
    ) in caplog.text
    assert "request_id=req_test" in caplog.text


def test_failure_log_line_has_class_status_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = _json_handler(
        _error_payload("overloaded_error"),
        status_code=529,
        headers={"retry-after-ms": "1"},
    )

    with (
        caplog.at_level(logging.WARNING),
        _adapter(handler) as adapter,
        pytest.raises(LLMUnavailableError),
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert (
        "Claude call failed: job=plan model=claude-opus-5-5 "
        "error=LLMUnavailableError status=529 "
        "error_type=overloaded_error request_id=req_test duration_ms="
    ) in caplog.text
    assert _SERVER_MESSAGE not in caplog.text


def test_logs_never_contain_prompt_response_or_key(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = "private system sentinel"
    turn = "private turn sentinel"
    response_text = "private response sentinel"
    error_text = "private error response sentinel"
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    for logger_name in ("anthropic", "httpx2", "httpcore2"):
        logging.getLogger(logger_name).setLevel(logging.DEBUG)

    calls = 0

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx2.Response(
                200,
                headers={"request-id": "req_success"},
                json=_message_payload(
                    content=[{"type": "text", "text": response_text}]
                ),
            )
        return httpx2.Response(
            400,
            headers={"request-id": "req_failure"},
            json=_error_payload("invalid_request_error", error_text),
        )

    caplog.set_level(logging.DEBUG)
    with _adapter(handle) as adapter:
        adapter.complete(LLMJob.PLAN, system=system, turns=[ChatTurn("user", turn)])
        with pytest.raises(LLMRequestRejectedError):
            adapter.complete(
                LLMJob.PLAN,
                system=system,
                turns=[ChatTurn("user", turn)],
            )

    for private_text in (system, turn, response_text, error_text, _FAKE_KEY):
        assert private_text not in caplog.text


def test_default_client_is_network_blocked() -> None:
    adapter = ClaudeAdapter(_config())
    try:
        with pytest.raises(NetworkBlockedError):
            adapter.complete(LLMJob.PLAN, system="system", turns=_turns())
    finally:
        adapter.close()
