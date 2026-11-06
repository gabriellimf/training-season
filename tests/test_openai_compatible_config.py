"""Tests for the OpenAI-compatible config and client in openai_compatible.py."""

from __future__ import annotations

import logging

import httpx2
import pytest

from conftest import NetworkBlockedError
from openai_compatible_fakes import (
    _DEFAULT_MODEL,
    _FAKE_KEY,
    _clear_openai_compatible_variables,
    _config,
    _turns,
)
from training_season.llm_base import LLMConfigurationError, LLMJob
from training_season.openai_compatible import (
    OPENAI_COMPATIBLE_API_KEY_ENV,
    OPENAI_COMPATIBLE_BASE_URL_ENV,
    OPENAI_COMPATIBLE_CHAT_MODEL_ENV,
    OPENAI_COMPATIBLE_PLAN_MODEL_ENV,
    OpenAICompatibleAdapter,
    new_openai_compatible_client,
    openai_compatible_config_from_environment,
)

_MISSING_KEY_MESSAGE = (
    "OPENAI_COMPATIBLE_API_KEY is not set and LLM_PROVIDER is openai_compatible "
    "(the default). Set the key, or set LLM_PROVIDER=anthropic, in the environment "
    "or the Compose env file before starting."
)
_BASE_URL_MESSAGE = (
    "OPENAI_COMPATIBLE_BASE_URL must be an https URL such as "
    "https://openrouter.ai/api/v1, with no user, password, query or fragment."
)


@pytest.fixture(autouse=True)
def clear_openai_compatible_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_openai_compatible_variables(monkeypatch)


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
