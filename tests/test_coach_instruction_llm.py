"""Tests that the rendered coaching instruction reaches the LLM system message."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import httpx2
import pytest

from coach_instruction_helpers import (
    _clear_coach_instruction_path,
    _load_path,
    _render,
    _write_instruction,
)
from training_season.llm import (
    ChatTurn,
    LLMAdapter,
    LLMJob,
    llm_config_from_environment,
    new_llm_adapter,
)

if TYPE_CHECKING:
    from pathlib import Path

RequestHandler = Callable[[httpx2.Request], httpx2.Response]
_FAKE_KEY = "sk-or-test-not-a-real-key"


@pytest.fixture(autouse=True)
def clear_coach_instruction_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_coach_instruction_path(monkeypatch)


def _completion_body(model: str) -> dict[str, object]:
    return {
        "id": "gen-test",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "safe response"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0},
    }


def _recording_handler(requests: list[httpx2.Request]) -> RequestHandler:
    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        request_body = json.loads(request.content)
        return httpx2.Response(200, json=_completion_body(str(request_body["model"])))

    return handle


@contextmanager
def _adapter(requests: list[httpx2.Request]) -> Iterator[LLMAdapter]:
    config = llm_config_from_environment({"OPENAI_COMPATIBLE_API_KEY": _FAKE_KEY})
    adapter = new_llm_adapter(
        config, transport=httpx2.MockTransport(_recording_handler(requests))
    )
    try:
        yield adapter
    finally:
        adapter.close()


def test_replacing_file_changes_system_text(tmp_path: Path) -> None:
    path = tmp_path / "coach.md"
    _write_instruction(path, "Instruction A: ${today}")
    instruction_a = _load_path(path)
    system_a = _render(instruction_a)

    _write_instruction(path, "Instruction B: ${today}")
    instruction_b = _load_path(path)
    system_b = _render(instruction_b)

    requests: list[httpx2.Request] = []
    with _adapter(requests) as adapter:
        adapter.complete(
            LLMJob.CHAT,
            system=system_a,
            turns=[ChatTurn("user", "safe user turn")],
        )
        adapter.complete(
            LLMJob.CHAT,
            system=system_b,
            turns=[ChatTurn("user", "safe user turn")],
        )

    systems = [json.loads(request.content)["messages"][0] for request in requests]
    assert systems[0] == {"role": "system", "content": system_a}
    assert systems[1] == {"role": "system", "content": system_b}
    assert system_a != system_b


def test_german_instruction_reaches_system_unchanged(tmp_path: Path) -> None:
    german_text = "Antworte auf Deutsch. ä ö ü ß."
    path = _write_instruction(tmp_path / "coach.md", german_text)
    instruction = _load_path(path)
    system_text = _render(instruction)
    requests: list[httpx2.Request] = []

    with _adapter(requests) as adapter:
        adapter.complete(
            LLMJob.CHAT,
            system=system_text,
            turns=[ChatTurn("user", "safe user turn")],
        )

    system = json.loads(requests[0].content)["messages"][0]
    assert system["role"] == "system"
    assert system["content"].encode("utf-8") == german_text.encode("utf-8")

    _write_instruction(path, "Antworte auf Deutsch. Heute: ${today}")
    instruction_with_date = _load_path(path)
    expected = _render(instruction_with_date)

    with _adapter(requests) as adapter:
        adapter.complete(
            LLMJob.CHAT,
            system=expected,
            turns=[ChatTurn("user", "safe user turn")],
        )

    second_system = json.loads(requests[1].content)["messages"][0]
    assert second_system == {"role": "system", "content": expected}
    assert expected == "Antworte auf Deutsch. Heute: 2026-03-01"
