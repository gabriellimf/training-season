"""Tests for the Anthropic adapter."""

from __future__ import annotations

import json
import logging

import httpx2
import pytest

from claude_fakes import (
    _FAKE_KEY,
    _SERVER_MESSAGE,
    RequestHandler,
    _adapter,
    _clear_anthropic_variables,
    _config,
    _error_payload,
    _json_handler,
    _message_payload,
    _turns,
)
from training_season.claude import (
    CLAUDE_PLAN_EFFORT_ENV,
    CLAUDE_PLAN_MODEL_ENV,
)
from training_season.llm_base import (
    ChatTurn,
    LLMJob,
    LLMRefusalError,
    LLMRequestRejectedError,
    LLMResponseError,
    LLMUnavailableError,
)


@pytest.fixture(autouse=True)
def clear_anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_anthropic_variables(monkeypatch)


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
