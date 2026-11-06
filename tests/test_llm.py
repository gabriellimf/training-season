"""Tests for the provider-neutral LLM interface."""

from __future__ import annotations

import json

import httpx2
import pytest

from training_season import llm
from training_season.llm import (
    LLM_PROVIDER_ENV,
    ChatTurn,
    LLMConfigurationError,
    LLMJob,
    LLMProvider,
    llm_config_from_environment,
    new_llm_adapter,
)

_OPENAI_KEY = "sk-or-test-not-a-real-key"
_ANTHROPIC_KEY = "sk-ant-test-not-a-real-key"
_DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
_ENVIRONMENT_NAMES = (
    LLM_PROVIDER_ENV,
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
    "OPENAI_COMPATIBLE_PLAN_MODEL",
    "OPENAI_COMPATIBLE_CHAT_MODEL",
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
_OPENAI_MISSING_KEY = (
    "OPENAI_COMPATIBLE_API_KEY is not set and LLM_PROVIDER is openai_compatible "
    "(the default). Set the key, or set LLM_PROVIDER=anthropic, in the environment "
    "or the Compose env file before starting."
)
_ANTHROPIC_MISSING_KEY = (
    "ANTHROPIC_API_KEY is not set. Set it in the environment or the Compose "
    "env file before starting."
)


@pytest.fixture(autouse=True)
def clear_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in _ENVIRONMENT_NAMES:
        monkeypatch.delenv(variable, raising=False)


def _handler(requests: list[httpx2.Request]) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        model = json.loads(request.content)["model"]
        if request.url.path == "/v1/messages":
            return httpx2.Response(
                200,
                json={
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": "safe response"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "gen-test",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "safe response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0},
            },
        )

    return httpx2.MockTransport(handle)


@pytest.mark.parametrize("provider", [None, "", "  "])
def test_default_provider_is_openai_compatible(provider: str | None) -> None:
    environment = {"OPENAI_COMPATIBLE_API_KEY": _OPENAI_KEY}
    if provider is not None:
        environment[LLM_PROVIDER_ENV] = provider

    config = llm_config_from_environment(environment)

    assert config.for_job(LLMJob.PLAN).model == _DEFAULT_MODEL
    assert config.for_job(LLMJob.CHAT).model == _DEFAULT_MODEL


def test_anthropic_provider_reads_claude_settings() -> None:
    config = llm_config_from_environment(
        {LLM_PROVIDER_ENV: "anthropic", "ANTHROPIC_API_KEY": _ANTHROPIC_KEY}
    )

    assert type(config).__name__ == "ClaudeConfig"
    assert config.for_job(LLMJob.PLAN).model == "claude-opus-5-5"
    assert config.for_job(LLMJob.CHAT).model == "claude-sonnet-5-5"


def test_config_reads_os_environ_when_environ_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_PROVIDER_ENV, "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ANTHROPIC_KEY)

    assert llm_config_from_environment().for_job(LLMJob.CHAT).model == (
        "claude-sonnet-5-5"
    )


@pytest.mark.parametrize("value", ["foo", "Anthropic", "openrouter"])
def test_invalid_provider_fails_with_exact_message(value: str) -> None:
    with pytest.raises(LLMConfigurationError) as raised:
        llm_config_from_environment(
            {LLM_PROVIDER_ENV: value, "OPENAI_COMPATIBLE_API_KEY": _OPENAI_KEY}
        )

    assert str(raised.value) == (
        f"LLM_PROVIDER must be openai_compatible or anthropic. Got: {value!r}."
    )


@pytest.mark.parametrize(
    "value",
    ["x" * 65, f"a{_OPENAI_KEY}b", f"a{_ANTHROPIC_KEY}b"],
)
def test_invalid_provider_hides_long_or_key_value(value: str) -> None:
    with pytest.raises(LLMConfigurationError) as raised:
        llm_config_from_environment(
            {
                LLM_PROVIDER_ENV: value,
                "OPENAI_COMPATIBLE_API_KEY": _OPENAI_KEY,
                "ANTHROPIC_API_KEY": _ANTHROPIC_KEY,
            }
        )

    assert str(raised.value) == (
        "LLM_PROVIDER must be openai_compatible or anthropic. "
        f"Got a value of {len(value)} characters."
    )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, _OPENAI_MISSING_KEY),
        ({LLM_PROVIDER_ENV: "openai_compatible"}, _OPENAI_MISSING_KEY),
        (
            {LLM_PROVIDER_ENV: "anthropic", "OPENAI_COMPATIBLE_API_KEY": _OPENAI_KEY},
            _ANTHROPIC_MISSING_KEY,
        ),
        ({"ANTHROPIC_API_KEY": _ANTHROPIC_KEY}, _OPENAI_MISSING_KEY),
    ],
)
def test_missing_key_for_selected_provider_fails(
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(LLMConfigurationError) as raised:
        llm_config_from_environment(environment)

    assert str(raised.value) == message


def test_unselected_provider_settings_are_not_validated() -> None:
    claude = llm_config_from_environment(
        {
            LLM_PROVIDER_ENV: "anthropic",
            "ANTHROPIC_API_KEY": _ANTHROPIC_KEY,
            "OPENAI_COMPATIBLE_BASE_URL": "http://insecure.example",
            "OPENAI_COMPATIBLE_PLAN_MODEL": "bad model",
        }
    )
    openai = llm_config_from_environment(
        {
            "OPENAI_COMPATIBLE_API_KEY": _OPENAI_KEY,
            "CLAUDE_PLAN_MODEL": "bad model",
            "CLAUDE_CHAT_EFFORT": "extreme",
        }
    )

    assert claude.for_job(LLMJob.PLAN).model == "claude-opus-5-5"
    assert openai.for_job(LLMJob.PLAN).model == _DEFAULT_MODEL


def test_new_llm_adapter_sends_openai_compatible_request() -> None:
    requests: list[httpx2.Request] = []
    config = llm_config_from_environment({"OPENAI_COMPATIBLE_API_KEY": _OPENAI_KEY})
    adapter = new_llm_adapter(config, transport=_handler(requests))
    try:
        reply = adapter.complete(
            LLMJob.CHAT, system="system", turns=[ChatTurn("user", "turn")]
        )
    finally:
        adapter.close()

    assert len(requests) == 1
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert json.loads(requests[0].content)["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "turn"},
    ]
    assert reply.text == "safe response"
    assert reply.model == _DEFAULT_MODEL


def test_new_llm_adapter_sends_anthropic_request() -> None:
    requests: list[httpx2.Request] = []
    config = llm_config_from_environment(
        {LLM_PROVIDER_ENV: "anthropic", "ANTHROPIC_API_KEY": _ANTHROPIC_KEY}
    )
    adapter = new_llm_adapter(config, transport=_handler(requests))
    try:
        reply = adapter.complete(
            LLMJob.CHAT, system="system", turns=[ChatTurn("user", "turn")]
        )
    finally:
        adapter.close()

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/messages"
    body = json.loads(requests[0].content)
    assert body["system"] == "system"
    assert body["messages"] == [{"role": "user", "content": "turn"}]
    assert reply.text == "safe response"
    assert reply.model == "claude-sonnet-5-5"


@pytest.mark.parametrize(
    ("provider", "message"),
    [(None, _OPENAI_MISSING_KEY), ("anthropic", _ANTHROPIC_MISSING_KEY)],
)
def test_check_exits_2_and_prints_message_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str | None,
    message: str,
) -> None:
    if provider is not None:
        monkeypatch.setenv(LLM_PROVIDER_ENV, provider)

    assert llm.main(["check"]) == 2
    output = capsys.readouterr()

    assert output.out == ""
    assert output.err == f"{message}\n"


def test_check_prints_openai_compatible_models_and_hides_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", _OPENAI_KEY)

    assert llm.main(["check"]) == 0
    output = capsys.readouterr()

    assert output.out == (
        f"plan provider=openai_compatible model={_DEFAULT_MODEL}\n"
        f"chat provider=openai_compatible model={_DEFAULT_MODEL}\n"
    )
    assert _OPENAI_KEY not in output.out + output.err


def test_check_prints_anthropic_models_and_hides_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(LLM_PROVIDER_ENV, "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ANTHROPIC_KEY)

    assert llm.main(["check"]) == 0
    output = capsys.readouterr()

    assert output.out == (
        "plan provider=anthropic model=claude-opus-5-5 effort=medium\n"
        "chat provider=anthropic model=claude-sonnet-5-5 effort=medium\n"
    )
    assert _ANTHROPIC_KEY not in output.out + output.err


def test_check_prints_none_effort(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(LLM_PROVIDER_ENV, "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ANTHROPIC_KEY)
    monkeypatch.setenv("CLAUDE_PLAN_EFFORT", "none")
    monkeypatch.setenv("CLAUDE_CHAT_EFFORT", "none")

    assert llm.main(["check"]) == 0

    assert capsys.readouterr().out == (
        "plan provider=anthropic model=claude-opus-5-5 effort=none\n"
        "chat provider=anthropic model=claude-sonnet-5-5 effort=none\n"
    )


@pytest.mark.parametrize("provider", list(LLMProvider))
def test_check_makes_no_network_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: LLMProvider,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("configuration check must not create a client")

    monkeypatch.setenv(LLM_PROVIDER_ENV, provider)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", _OPENAI_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _ANTHROPIC_KEY)
    monkeypatch.setattr(llm, "new_anthropic_client", fail)
    monkeypatch.setattr(llm, "new_openai_compatible_client", fail)

    assert llm.main(["check"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize(
    ("arguments", "code"),
    [(["--help"], 0), (["-h"], 0), ([], 2), (["run"], 2), (["check", "x"], 2)],
)
def test_usage(
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    code: int,
) -> None:
    assert llm.main(arguments) == code

    captured = capsys.readouterr()
    assert captured.err == "Usage: training-season-llm check\n"
    assert captured.out == ""
