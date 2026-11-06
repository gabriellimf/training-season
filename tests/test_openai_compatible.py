"""Tests for the OpenAI-compatible adapter."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal

import httpx2
import pytest

from conftest import NetworkBlockedError
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
from training_season.openai_compatible import (
    OPENAI_COMPATIBLE_API_KEY_ENV,
    OPENAI_COMPATIBLE_BASE_URL_ENV,
    OPENAI_COMPATIBLE_CHAT_MODEL_ENV,
    OPENAI_COMPATIBLE_PLAN_MODEL_ENV,
    OpenAICompatibleAdapter,
    OpenAICompatibleConfig,
    new_openai_compatible_client,
    openai_compatible_config_from_environment,
)

_FAKE_KEY = "sk-or-test-not-a-real-key"
_DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
_ENVIRONMENT_NAMES = (
    OPENAI_COMPATIBLE_API_KEY_ENV,
    OPENAI_COMPATIBLE_BASE_URL_ENV,
    OPENAI_COMPATIBLE_PLAN_MODEL_ENV,
    OPENAI_COMPATIBLE_CHAT_MODEL_ENV,
)
_SERVER_MESSAGE = "private-server-message-sentinel"
_MALFORMED_BODY_SENTINEL = "private malformed response body sentinel"
_MISSING_KEY_MESSAGE = (
    "OPENAI_COMPATIBLE_API_KEY is not set and LLM_PROVIDER is openai_compatible "
    "(the default). Set the key, or set LLM_PROVIDER=anthropic, in the environment "
    "or the Compose env file before starting."
)
_BASE_URL_MESSAGE = (
    "OPENAI_COMPATIBLE_BASE_URL must be an https URL such as "
    "https://openrouter.ai/api/v1, with no user, password, query or fragment."
)
RequestHandler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture(autouse=True)
def clear_openai_compatible_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in _ENVIRONMENT_NAMES:
        monkeypatch.delenv(variable, raising=False)


def _config(**overrides: str) -> OpenAICompatibleConfig:
    environment = {OPENAI_COMPATIBLE_API_KEY_ENV: _FAKE_KEY}
    environment.update(overrides)
    return openai_compatible_config_from_environment(environment)


def _completion(
    *,
    content: object = "safe response",
    finish_reason: object = "stop",
    usage: object = None,
    request_id: object = "gen-test",
    model: object = _DEFAULT_MODEL,
) -> dict[str, object]:
    return {
        "id": request_id,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": (
            {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": 0,
            }
            if usage is None
            else usage
        ),
    }


def _error_payload(code: int, message: str = _SERVER_MESSAGE) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "metadata": {"error_type": "provider_error"},
        }
    }


def _choice_error_payload(
    code: object,
    *,
    content: str = "partial content",
    message: str = _SERVER_MESSAGE,
) -> dict[str, object]:
    error: dict[str, object] = {
        "message": message,
        "metadata": {"error_type": "provider_error"},
    }
    if code is not None:
        error["code"] = code
    return {
        "id": "gen-error",
        "model": _DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "error",
                "error": error,
            }
        ],
    }


def _json_handler(
    payload: object,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    requests: list[httpx2.Request] | None = None,
) -> RequestHandler:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(request)
        return httpx2.Response(status_code, headers=headers, json=payload)

    return handle


@contextmanager
def _adapter(
    handler: RequestHandler,
    config: OpenAICompatibleConfig | None = None,
    sleeps: list[float] | None = None,
) -> Iterator[OpenAICompatibleAdapter]:
    transport = httpx2.MockTransport(handler)
    adapter = OpenAICompatibleAdapter(
        _config() if config is None else config,
        factory=lambda c: new_openai_compatible_client(c, transport=transport),
        sleep=([] if sleeps is None else sleeps).append,
    )
    try:
        yield adapter
    finally:
        adapter.close()


def _turns() -> list[ChatTurn]:
    return [ChatTurn("user", "safe user turn")]


def test_config_defaults_with_only_key() -> None:
    config = _config()

    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.plan.model == _DEFAULT_MODEL
    assert config.plan.max_tokens == 32_000
    assert config.plan.timeout == httpx2.Timeout(900.0, connect=5.0)
    assert config.chat.model == _DEFAULT_MODEL
    assert config.chat.max_tokens == 16_000
    assert config.chat.timeout == httpx2.Timeout(600.0, connect=5.0)


def test_config_reads_os_environ_when_environ_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OPENAI_COMPATIBLE_API_KEY_ENV, _FAKE_KEY)

    assert openai_compatible_config_from_environment().api_key == _FAKE_KEY


def test_api_key_is_stripped_and_hidden_from_repr() -> None:
    config = _config(**{OPENAI_COMPATIBLE_API_KEY_ENV: f"  {_FAKE_KEY}\n"})

    assert config.api_key == _FAKE_KEY
    assert _FAKE_KEY not in repr(config)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_or_blank_key_fails(value: str | None) -> None:
    environment = {} if value is None else {OPENAI_COMPATIBLE_API_KEY_ENV: value}

    with pytest.raises(LLMConfigurationError) as error:
        openai_compatible_config_from_environment(environment)

    assert str(error.value) == _MISSING_KEY_MESSAGE


@pytest.mark.parametrize(
    "value", ["sk-or-test\N{RIGHT SINGLE QUOTATION MARK}key", "sk-or-test\x00key"]
)
def test_key_with_non_printable_or_non_ascii_character_fails_without_echo(
    value: str,
) -> None:
    with pytest.raises(LLMConfigurationError) as error:
        openai_compatible_config_from_environment(
            {OPENAI_COMPATIBLE_API_KEY_ENV: value}
        )

    assert str(error.value) == (
        "OPENAI_COMPATIBLE_API_KEY must hold only printable ASCII characters "
        "with no spaces."
    )
    assert value not in str(error.value)


def test_config_overrides() -> None:
    config = _config(
        **{
            OPENAI_COMPATIBLE_BASE_URL_ENV: "https://llm.example.com/v1/",
            OPENAI_COMPATIBLE_PLAN_MODEL_ENV: "vendor/plan-model:free",
            OPENAI_COMPATIBLE_CHAT_MODEL_ENV: "vendor/chat@2026-01",
        }
    )

    assert config.base_url == "https://llm.example.com/v1"
    assert config.plan.model == "vendor/plan-model:free"
    assert config.chat.model == "vendor/chat@2026-01"


@pytest.mark.parametrize(
    "value",
    [
        "http://openrouter.ai/api/v1",
        "https://user:secret@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?region=eu",
        "https://openrouter.ai/api/v1#top",
        "https://",
        "not a url",
    ],
)
def test_invalid_base_url_fails(value: str) -> None:
    with pytest.raises(LLMConfigurationError) as error:
        _config(**{OPENAI_COMPATIBLE_BASE_URL_ENV: value})

    assert str(error.value) == f"{_BASE_URL_MESSAGE} Got: {value!r}."


def test_base_url_with_key_fails_without_echo() -> None:
    value = f"https://openrouter.ai/{_FAKE_KEY}/v1"

    with pytest.raises(LLMConfigurationError) as error:
        _config(**{OPENAI_COMPATIBLE_BASE_URL_ENV: value})

    assert str(error.value) == (
        f"{_BASE_URL_MESSAGE} Got a value of {len(value)} characters."
    )


@pytest.mark.parametrize(
    "variable", [OPENAI_COMPATIBLE_PLAN_MODEL_ENV, OPENAI_COMPATIBLE_CHAT_MODEL_ENV]
)
@pytest.mark.parametrize(
    ("value", "detail"),
    [
        ("bad model", "Got: 'bad model'."),
        ("modèle", "Got: 'modèle'."),
        ("m" * 129, "Got a value of 129 characters."),
        (f"vendor/{_FAKE_KEY}", "Got a value of 32 characters."),
    ],
)
def test_invalid_model_fails(variable: str, value: str, detail: str) -> None:
    with pytest.raises(LLMConfigurationError) as error:
        _config(**{variable: value})

    assert str(error.value) == (
        f"{variable} must be a model id such as {_DEFAULT_MODEL}: ASCII, no "
        f"whitespace, at most 128 characters. {detail}"
    )


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


def _top_level_error_body(code: object, message: str = _SERVER_MESSAGE) -> bytes:
    error: dict[str, object] = {
        "message": message,
        "metadata": {"error_type": "timeout"},
    }
    if code is not None:
        error["code"] = code
    return b" " * 1100 + json.dumps({"id": "gen-1", "error": error}).encode()


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


def test_client_only_raises_http_logger_levels() -> None:
    logging.getLogger("httpx2").setLevel(logging.DEBUG)
    logging.getLogger("httpcore2").setLevel(logging.WARNING)

    new_openai_compatible_client(_config()).close()

    assert logging.getLogger("httpx2").level == logging.INFO
    assert logging.getLogger("httpcore2").level == logging.WARNING


def test_default_client_is_network_blocked() -> None:
    adapter = OpenAICompatibleAdapter(_config())
    try:
        with pytest.raises(NetworkBlockedError):
            adapter.complete(LLMJob.PLAN, system="system", turns=_turns())
    finally:
        adapter.close()
