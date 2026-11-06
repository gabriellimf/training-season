"""Tests for the provider-neutral LLM base."""

from __future__ import annotations

import logging
import re

import pytest

from training_season.llm_base import (
    ChatTurn,
    LazyLLMClient,
    LLMConfigurationError,
    LLMRefusalError,
    LLMResponseError,
    check_request,
    config_value_detail,
    model_id,
    raise_http_logger_levels,
)


@pytest.mark.parametrize(
    ("system", "turns", "message"),
    [
        ("", [ChatTurn("user", "turn")], "LLM system text is required"),
        ("  ", [ChatTurn("user", "turn")], "LLM system text is required"),
        ("system", [], "At least one LLM turn is required"),
        ("system", [ChatTurn("user", "  ")], "LLM turn text is required"),
        (
            "system",
            [ChatTurn("assistant", "turn")],
            "The last LLM turn must be from the user",
        ),
    ],
)
def test_check_request_rejects_invalid_input(
    system: str,
    turns: list[ChatTurn],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        check_request(system, turns)


def test_check_request_accepts_valid_input() -> None:
    check_request(
        "system",
        [ChatTurn("user", "a"), ChatTurn("assistant", "b"), ChatTurn("user", "c")],
    )


def test_config_value_detail_echoes_a_safe_value() -> None:
    assert config_value_detail("bad", "key-one", "key-two") == "Got: 'bad'."


def test_config_value_detail_hides_a_value_that_holds_any_secret() -> None:
    assert (
        config_value_detail("has key-two inside", "key-one", "key-two")
        == "Got a value of 18 characters."
    )


def test_config_value_detail_hides_a_long_value() -> None:
    assert config_value_detail("x" * 65, "key") == "Got a value of 65 characters."


def test_config_value_detail_ignores_an_empty_secret() -> None:
    assert config_value_detail("bad", "") == "Got: 'bad'."


def test_response_error_adds_category_and_request_id() -> None:
    error = LLMResponseError("Failed.", category="cyber", request_id="req_1")

    assert str(error) == "Failed (category cyber, request_id req_1)."
    assert error.request_id == "req_1"


def test_response_error_drops_an_invalid_category() -> None:
    error = LLMResponseError("Failed.", category="Not Valid", request_id=None)

    assert str(error) == "Failed."


def test_refusal_error_uses_the_given_message() -> None:
    error = LLMRefusalError("Refused.", category="cyber", request_id="req_1")

    assert str(error) == "Refused (category cyber, request_id req_1)."
    assert error.stop_reason == "refusal"
    assert error.category == "cyber"


def test_model_id_accepts_a_custom_pattern() -> None:
    pattern = re.compile(r"[a-z/]+\Z", re.ASCII)

    assert model_id({"M": "a/b"}, "M", "default", "key", pattern=pattern) == "a/b"
    assert model_id({}, "M", "default", "key", pattern=pattern) == "default"
    with pytest.raises(LLMConfigurationError, match="M must be a model id"):
        model_id({"M": "a:b"}, "M", "default", "key", pattern=pattern)


def test_model_id_hides_a_value_that_holds_the_key() -> None:
    pattern = re.compile(r"[a-z/-]+\Z", re.ASCII)

    with pytest.raises(LLMConfigurationError) as raised:
        model_id({"M": "a-secret-b"}, "M", "default", "secret", pattern=pattern)

    assert "secret" not in str(raised.value)
    assert "Got a value of 10 characters." in str(raised.value)


def test_raise_http_logger_levels_only_raises() -> None:
    quiet = logging.getLogger("llm_base_test_quiet")
    loud = logging.getLogger("llm_base_test_loud")
    quiet.setLevel(logging.DEBUG)
    loud.setLevel(logging.ERROR)

    raise_http_logger_levels("llm_base_test_quiet", "llm_base_test_loud")

    assert quiet.level == logging.INFO
    assert loud.level == logging.ERROR


class FakeClosableClient:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def test_lazy_client_creates_once_and_closes_once() -> None:
    created: list[FakeClosableClient] = []

    def factory(config: str) -> FakeClosableClient:
        created.append(FakeClosableClient())
        return created[-1]

    lazy = LazyLLMClient("config", factory)

    first = lazy.get_or_create()
    assert lazy.get_or_create() is first
    assert len(created) == 1

    lazy.close()
    lazy.close()
    assert first.close_calls == 1

    assert lazy.get_or_create() is not first
    assert len(created) == 2


def test_lazy_client_close_without_client_creates_nothing() -> None:
    created: list[FakeClosableClient] = []

    def factory(config: str) -> FakeClosableClient:
        created.append(FakeClosableClient())
        return created[-1]

    LazyLLMClient("config", factory).close()

    assert created == []
