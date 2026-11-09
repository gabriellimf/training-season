from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import httpx2
import pytest

from training_season.llm import (
    ChatTurn,
    LLMAuthenticationError,
    LLMJob,
    llm_config_from_environment,
    new_llm_adapter,
)
from training_season.workout_cli import main
from training_season.workout_request import (
    WORKOUT_OUTPUT_CONTRACT,
    WorkoutAccepted,
    WorkoutRejected,
    request_workout,
    workout_system_text,
)
from workout_fixtures import example_workout_text

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from training_season.llm import LLMAdapter, LLMReply

_ENVIRONMENT = {
    "OPENAI_COMPATIBLE_API_KEY": "sk-or-test-not-a-real-key",
    "TZ": "America/Sao_Paulo",
}
_BAD_REPLY = '{"name": "", "steps": []}'
_SYSTEM = "Instrução de teste"
_TURNS = [ChatTurn("user", "Monte um 10x400")]


class ScriptedTransport:
    def __init__(self, replies: Sequence[str]) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, list[dict[str, str]]]] = []

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._answer)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        content = self.replies[len(self.bodies) - 1]
        return httpx2.Response(
            200,
            json={
                "id": "gen-test",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0},
            },
        )


def run_request(replies: Sequence[str]) -> tuple[object, ScriptedTransport]:
    script = ScriptedTransport(replies)
    config = llm_config_from_environment(_ENVIRONMENT)
    adapter = new_llm_adapter(config, transport=script.transport())
    try:
        result = request_workout(adapter, LLMJob.PLAN, system=_SYSTEM, turns=_TURNS)
    finally:
        adapter.close()
    return result, script


def roles(body: dict[str, list[dict[str, str]]]) -> list[str]:
    return [message["role"] for message in body["messages"]]


def test_valid_first_reply_is_accepted_after_one_attempt() -> None:
    result, script = run_request([example_workout_text()])

    assert isinstance(result, WorkoutAccepted)
    assert (result.attempts, len(result.replies)) == (1, 1)
    assert len(script.bodies) == 1


def test_invalid_then_valid_is_accepted_after_two_attempts() -> None:
    result, _ = run_request([_BAD_REPLY, example_workout_text()])

    assert isinstance(result, WorkoutAccepted)
    assert result.attempts == 2
    assert result.workout.name == "26/10 10x400"


def test_retry_request_carries_the_assistant_turn_and_the_errors() -> None:
    _, script = run_request([_BAD_REPLY, example_workout_text()])

    retry = script.bodies[1]
    assert roles(retry) == ["system", "user", "assistant", "user"]
    assert retry["messages"][2]["content"] == _BAD_REPLY
    assert "name: " in retry["messages"][3]["content"]
    assert "apenas com o JSON corrigido" in retry["messages"][3]["content"]


def test_retry_request_keeps_the_same_system_text() -> None:
    _, script = run_request([_BAD_REPLY, example_workout_text()])

    first, retry = script.bodies
    assert retry["messages"][0] == first["messages"][0]


def test_invalid_twice_is_rejected_with_a_portuguese_message() -> None:
    result, script = run_request([_BAD_REPLY, "not json"])

    assert isinstance(result, WorkoutRejected)
    assert result.attempts == 2
    assert len(script.bodies) == 2
    assert result.chat_message.startswith(
        "O treinador devolveu um treino inválido duas vezes. "
        "Nada foi enviado ao Garmin."
    )


def test_rejection_lists_the_errors_of_the_last_attempt() -> None:
    result, _ = run_request([_BAD_REPLY, "not json"])

    assert isinstance(result, WorkoutRejected)
    assert result.errors[0].startswith("$: ")
    assert result.chat_message.endswith(result.errors[0])


def test_system_text_ends_with_the_contract() -> None:
    text = workout_system_text("Instrução")

    assert text.startswith("Instrução\n\n")
    assert text.endswith(WORKOUT_OUTPUT_CONTRACT)


class BlankFirstReplyAdapter:
    """Wraps a real adapter and blanks the text of its first reply."""

    def __init__(self, inner: LLMAdapter) -> None:
        self.inner = inner
        self.calls = 0

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        reply = self.inner.complete(job, system=system, turns=turns)
        self.calls += 1
        return replace(reply, text="") if self.calls == 1 else reply

    def close(self) -> None:
        self.inner.close()


def test_empty_first_reply_is_echoed_as_a_placeholder() -> None:
    script = ScriptedTransport(["ignored", example_workout_text()])
    config = llm_config_from_environment(_ENVIRONMENT)
    adapter = BlankFirstReplyAdapter(
        new_llm_adapter(config, transport=script.transport())
    )
    try:
        result = request_workout(adapter, LLMJob.PLAN, system=_SYSTEM, turns=_TURNS)
    finally:
        adapter.close()

    assert isinstance(result, WorkoutAccepted)
    assert result.attempts == 2
    assert script.bodies[1]["messages"][2]["content"] == "(resposta vazia)"


def test_llm_error_on_the_second_call_reaches_the_caller() -> None:
    calls: list[int] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return ScriptedTransport([_BAD_REPLY])._answer(request)
        return httpx2.Response(401, json={})

    config = llm_config_from_environment(_ENVIRONMENT)
    adapter = new_llm_adapter(config, transport=httpx2.MockTransport(answer))
    try:
        with pytest.raises(LLMAuthenticationError):
            request_workout(adapter, LLMJob.PLAN, system=_SYSTEM, turns=_TURNS)
    finally:
        adapter.close()


def test_contract_names_the_step_types_and_the_limits() -> None:
    assert "warmup, interval, recovery, cooldown ou repeat" in WORKOUT_OUTPUT_CONTRACT
    for limit in ("50", "99", "15"):
        assert limit in WORKOUT_OUTPUT_CONTRACT


def run_command(
    tmp_path: Path, replies: Sequence[str], environ: dict[str, str] | None = None
) -> int:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Monte um 10x400", encoding="utf-8")
    script = ScriptedTransport(replies)
    return main(
        ["request", str(prompt)],
        environ=_ENVIRONMENT if environ is None else environ,
        transport=script.transport(),
    )


def test_request_command_prints_the_summary_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_command(tmp_path, [example_workout_text()]) == 0

    assert capsys.readouterr().out == "workout name='26/10 10x400' steps=5 attempts=1\n"


def test_request_command_prints_the_chat_message_and_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_command(tmp_path, ["OK", "OK"]) == 3

    captured = capsys.readouterr()
    assert "Nada foi enviado ao Garmin." in captured.out
    assert captured.err == ""


def test_request_command_reports_a_configuration_error_and_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_command(tmp_path, [], environ={"TZ": "America/Sao_Paulo"}) == 2

    assert "OPENAI_COMPATIBLE_API_KEY" in capsys.readouterr().err


def test_request_command_reports_a_bad_timezone_and_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = {**_ENVIRONMENT, "TZ": "Nowhere"}

    assert run_command(tmp_path, [], environ=environ) == 2

    assert "TZ must name an IANA timezone" in capsys.readouterr().err


def test_request_command_reports_a_missing_prompt_and_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.txt"

    code = main(["request", str(missing)], environ=_ENVIRONMENT)

    assert code == 2
    assert str(missing) in capsys.readouterr().err


def test_request_command_reports_a_provider_failure_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Monte um 10x400", encoding="utf-8")
    refuse = httpx2.MockTransport(lambda _: httpx2.Response(401, json={}))

    code = main(["request", str(prompt)], environ=_ENVIRONMENT, transport=refuse)

    assert code == 1
    assert "rejected the API key (HTTP 401)" in capsys.readouterr().err


@pytest.mark.parametrize("content", ["", "  \n\t"])
def test_request_command_rejects_a_blank_prompt_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(content, encoding="utf-8")
    script = ScriptedTransport([])

    code = main(
        ["request", str(prompt)], environ=_ENVIRONMENT, transport=script.transport()
    )

    assert code == 2
    assert script.bodies == []
    assert "empty or only whitespace" in capsys.readouterr().err


def test_request_command_rejects_a_prompt_file_above_one_megabyte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_bytes(b"a" * 1_048_577)
    script = ScriptedTransport([])

    code = main(
        ["request", str(prompt)], environ=_ENVIRONMENT, transport=script.transport()
    )

    assert code == 2
    assert script.bodies == []
    assert "1048577 bytes, above the limit of 1048576 bytes" in capsys.readouterr().err
