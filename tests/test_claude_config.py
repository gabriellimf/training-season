"""Tests for the Anthropic adapter config reader and client construction."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from claude_fakes import _FAKE_KEY, _clear_anthropic_variables, _config, _turns
from conftest import NetworkBlockedError
from training_season.claude import (
    ANTHROPIC_API_KEY_ENV,
    CLAUDE_CHAT_EFFORT_ENV,
    CLAUDE_CHAT_MODEL_ENV,
    CLAUDE_PLAN_EFFORT_ENV,
    CLAUDE_PLAN_MODEL_ENV,
    ClaudeAdapter,
    claude_config_from_environment,
    new_anthropic_client,
)
from training_season.llm_base import LLMConfigurationError, LLMJob


@pytest.fixture(autouse=True)
def clear_anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_anthropic_variables(monkeypatch)


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


def test_default_client_is_network_blocked() -> None:
    adapter = ClaudeAdapter(_config())
    try:
        with pytest.raises(NetworkBlockedError):
            adapter.complete(LLMJob.PLAN, system="system", turns=_turns())
    finally:
        adapter.close()
