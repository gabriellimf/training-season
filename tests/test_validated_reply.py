"""Tests for the shared one-retry request: caller parser and caller headline."""

from __future__ import annotations

from llm_fakes import QueuedReplyAdapter
from training_season.llm import ChatTurn, LLMJob
from training_season.validated_reply import (
    Accepted,
    Rejected,
    request_with_one_retry,
)
from training_season.workout_validation import WorkoutValidationError

_HEADLINE = "Resposta inválida duas vezes."
_TURNS = (ChatTurn("user", "Diga um número par"),)


def parse_even_number(text: str) -> int:
    if not text.strip().isdigit() or int(text) % 2:
        raise WorkoutValidationError((f"got {text!r}, expected an even number",))
    return int(text)


def run_request(replies: list[str]) -> tuple[Accepted[int] | Rejected, list[str]]:
    adapter = QueuedReplyAdapter(replies)
    outcome = request_with_one_retry(
        adapter,
        LLMJob.CHAT,
        system="Sistema",
        turns=_TURNS,
        parse=parse_even_number,
        rejection_headline=_HEADLINE,
    )
    return outcome, [request.job.value for request in adapter.requests]


def test_accepted_carries_the_parsed_value_of_the_retry() -> None:
    outcome, jobs = run_request(["3", "4"])

    assert isinstance(outcome, Accepted)
    assert (outcome.value, outcome.attempts, len(outcome.replies)) == (4, 2, 2)
    assert jobs == ["chat", "chat"]


def test_rejected_starts_with_the_caller_headline() -> None:
    outcome, _ = run_request(["3", "5"])

    assert isinstance(outcome, Rejected)
    assert outcome.chat_message == f"{_HEADLINE}\ngot '5', expected an even number"
    assert outcome.errors == ("got '5', expected an even number",)


def test_retry_ends_with_the_caller_retry_request() -> None:
    adapter = QueuedReplyAdapter(["3", "4"])

    request_with_one_retry(
        adapter,
        LLMJob.CHAT,
        system="Sistema",
        turns=_TURNS,
        parse=parse_even_number,
        rejection_headline=_HEADLINE,
        retry_request="Responda só com o número corrigido.",
    )

    retry_turn = adapter.requests[1].turns[-1]
    assert retry_turn.role == "user"
    assert retry_turn.text.endswith("\nResponda só com o número corrigido.")


def retry_request_naming_the_first_reply(first_text: str) -> str:
    return f"Troque o {first_text} por um número par."


def test_retry_request_can_depend_on_the_first_reply() -> None:
    adapter = QueuedReplyAdapter(["3", "4"])

    request_with_one_retry(
        adapter,
        LLMJob.CHAT,
        system="Sistema",
        turns=_TURNS,
        parse=parse_even_number,
        rejection_headline=_HEADLINE,
        retry_request=retry_request_naming_the_first_reply,
    )

    retry_text = adapter.requests[1].turns[-1].text
    assert retry_text.endswith("\nTroque o 3 por um número par.")
