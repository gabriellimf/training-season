"""Anthropic mock transport and adapter helpers shared by the Claude tests."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import TYPE_CHECKING

import httpx2

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
from training_season.llm_base import ChatTurn

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    import anthropic
    import pytest

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
RequestHandler = Callable[[httpx2.Request], httpx2.Response]


def _clear_anthropic_variables(monkeypatch: pytest.MonkeyPatch) -> None:
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
