"""Tests for the OpenAI-compatible adapter."""

from __future__ import annotations

import json
import logging

import httpx2
import pytest

from openai_compatible_fakes import (
    _DEFAULT_MODEL,
    _FAKE_KEY,
    _SERVER_MESSAGE,
    _adapter,
    _choice_error_payload,
    _clear_openai_compatible_variables,
    _completion,
    _config,
    _error_payload,
    _json_handler,
    _top_level_error_body,
    _turns,
)
from training_season.llm_base import (
    ChatTurn,
    LLMError,
    LLMJob,
    LLMRateLimitedError,
    LLMRefusalError,
    LLMRequestRejectedError,
    LLMResponseError,
)


@pytest.fixture(autouse=True)
def clear_openai_compatible_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_openai_compatible_variables(monkeypatch)


@pytest.mark.parametrize(
    ("job", "max_tokens", "timeout"),
    [
        (LLMJob.PLAN, 32_000, httpx2.Timeout(900.0, connect=5.0)),
        (LLMJob.CHAT, 16_000, httpx2.Timeout(600.0, connect=5.0)),
    ],
)
def test_request_shape(job: LLMJob, max_tokens: int, timeout: httpx2.Timeout) -> None:
    requests: list[httpx2.Request] = []
    turns = [
        ChatTurn("user", "first"),
        ChatTurn("assistant", "second"),
        ChatTurn("user", "third"),
    ]

    with _adapter(_json_handler(_completion(), requests=requests)) as adapter:
        adapter.complete(job, system="system text", turns=turns)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {_FAKE_KEY}"
    assert request.extensions["timeout"] == timeout.as_dict()
    assert json.loads(request.content) == {
        "model": _DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": "system text"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ],
    }


@pytest.mark.parametrize(
    ("finish_reason", "content", "error_class", "message"),
    [
        (
            "length",
            "partial",
            LLMResponseError,
            "The model stopped at the max_tokens limit (request_id gen-test).",
        ),
        (
            "content_filter",
            "partial",
            LLMRefusalError,
            "The model refused the request (request_id gen-test).",
        ),
        (
            "tool_calls",
            None,
            LLMResponseError,
            "The model returned an unsupported finish reason (request_id gen-test).",
        ),
        (
            None,
            "text",
            LLMResponseError,
            "The model returned an unsupported finish reason (request_id gen-test).",
        ),
        (
            "stop",
            "",
            LLMResponseError,
            "The model returned no text (request_id gen-test).",
        ),
        (
            "stop",
            None,
            LLMResponseError,
            "The model returned no text (request_id gen-test).",
        ),
    ],
)
def test_finish_reason_errors(
    finish_reason: str | None,
    content: str | None,
    error_class: type[LLMResponseError],
    message: str,
) -> None:
    requests: list[httpx2.Request] = []
    payload = _completion(content=content, finish_reason=finish_reason)

    with (
        _adapter(_json_handler(payload, requests=requests)) as adapter,
        pytest.raises(error_class) as error,
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert type(error.value) is error_class
    assert str(error.value) == message
    assert error.value.stop_reason == (
        "refusal" if error_class is LLMRefusalError else finish_reason
    )
    assert len(requests) == 1


def test_success_log_line_has_expected_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.INFO),
        _adapter(_json_handler(_completion())) as adapter,
    ):
        adapter.complete(LLMJob.PLAN, system="system", turns=_turns())

    assert (
        f"OpenAI-compatible call: job=plan model={_DEFAULT_MODEL} "
        "finish_reason=stop input_tokens=100 output_tokens=50 "
        "cache_read_tokens=0 cache_write_tokens=0 cost_usd=0.000000 duration_ms="
    ) in caplog.text
    assert "request_id=gen-test" in caplog.text
    assert {
        record.name
        for record in caplog.records
        if record.getMessage().startswith("OpenAI-compatible call:")
    } == {"training_season.openai_compatible"}


def test_failure_log_line_has_class_status_and_error_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = _json_handler(_error_payload(400), status_code=400)

    with (
        caplog.at_level(logging.WARNING),
        _adapter(handler) as adapter,
        pytest.raises(LLMRequestRejectedError),
    ):
        adapter.complete(LLMJob.CHAT, system="system", turns=_turns())

    assert (
        f"OpenAI-compatible call failed: job=chat model={_DEFAULT_MODEL} "
        "error=LLMRequestRejectedError status=400 error_type=provider_error "
        "request_id=None duration_ms="
    ) in caplog.text
    assert _SERVER_MESSAGE not in caplog.text
    assert {
        record.name
        for record in caplog.records
        if record.getMessage().startswith("OpenAI-compatible call failed:")
    } == {"training_season.openai_compatible"}


def test_logs_never_contain_prompt_response_or_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    system = "private system sentinel"
    turn = "private turn sentinel"
    response_text = "private response sentinel"
    reasoning_text = "private reasoning sentinel"
    partial_text = "private partial content sentinel"
    error_texts = (
        "private rejected error sentinel",
        "private choice error sentinel",
        "private rate limit error sentinel",
        "private top-level error sentinel",
    )
    for logger_name in ("httpx2", "httpcore2"):
        logging.getLogger(logger_name).setLevel(logging.DEBUG)

    success = _completion(content=response_text)
    choices = success["choices"]
    assert isinstance(choices, list)
    choices[0]["message"]["reasoning"] = reasoning_text
    responses = [
        httpx2.Response(200, json=success),
        httpx2.Response(400, json=_error_payload(400, error_texts[0])),
        httpx2.Response(
            200,
            json=_choice_error_payload(
                400, content=partial_text, message=error_texts[1]
            ),
        ),
        httpx2.Response(
            200,
            headers={"content-type": "application/json"},
            content=_top_level_error_body(400, error_texts[3]),
        ),
        *(
            httpx2.Response(
                429,
                headers={"retry-after": "0"},
                json=_error_payload(429, error_texts[2]),
            )
            for _ in range(3)
        ),
    ]
    config = _config()
    errors: list[LLMError] = []

    caplog.set_level(logging.DEBUG)
    with _adapter(lambda _request: responses.pop(0), config) as adapter:
        adapter.complete(LLMJob.PLAN, system=system, turns=[ChatTurn("user", turn)])
        for _ in range(4):
            with pytest.raises(LLMError) as raised:
                adapter.complete(
                    LLMJob.PLAN, system=system, turns=[ChatTurn("user", turn)]
                )
            errors.append(raised.value)

    assert responses == []
    assert [type(error) for error in errors] == [
        LLMRequestRejectedError,
        LLMRequestRejectedError,
        LLMRequestRejectedError,
        LLMRateLimitedError,
    ]
    private_texts = (
        system,
        turn,
        response_text,
        reasoning_text,
        partial_text,
        *error_texts,
        _FAKE_KEY,
    )
    for private_text in private_texts:
        assert private_text not in caplog.text
        assert private_text not in repr(config)
        for error in errors:
            assert private_text not in str(error)
    for error in errors:
        assert error.__cause__ is None
        assert error.__context__ is None
