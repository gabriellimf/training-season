"""Mock transport and adapter helpers shared by the OpenAI-compatible tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from typing import TYPE_CHECKING

import httpx2

from training_season.llm_base import ChatTurn
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

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    import pytest

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
RequestHandler = Callable[[httpx2.Request], httpx2.Response]


def _clear_openai_compatible_variables(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _top_level_error_body(code: object, message: str = _SERVER_MESSAGE) -> bytes:
    error: dict[str, object] = {
        "message": message,
        "metadata": {"error_type": "timeout"},
    }
    if code is not None:
        error["code"] = code
    return b" " * 1100 + json.dumps({"id": "gen-1", "error": error}).encode()
