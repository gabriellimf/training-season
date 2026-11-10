"""Tests for the macro plan request: plan job, fixed dates, one retry."""

from __future__ import annotations

from llm_fakes import QueuedReplyAdapter
from onboarding_rows import RACE_DAY, REASON, TODAY, macro_text, valid_result
from training_season.macro_generation import (
    MACRO_OUTPUT_CONTRACT,
    generate_macro,
    macro_system_text,
)
from training_season.validated_reply import Accepted, Rejected
from training_season.workout_request import JSON_ONLY_HEADER

_CONTEXT = "## Plano macro\nSem plano ativo."


def run_generation(replies: list[str]) -> tuple[object, QueuedReplyAdapter]:
    adapter = QueuedReplyAdapter(replies)
    outcome = generate_macro(
        adapter,
        instruction_text="Instrução",
        result=valid_result(),
        context_text=_CONTEXT,
        today=TODAY,
    )
    return outcome, adapter


def test_contract_starts_with_the_json_only_header() -> None:
    assert MACRO_OUTPUT_CONTRACT.startswith(JSON_ONLY_HEADER)
    assert macro_system_text("Instrução") == f"Instrução\n\n{MACRO_OUTPUT_CONTRACT}"


def test_valid_macro_uses_the_plan_job_and_the_fixed_dates() -> None:
    outcome, adapter = run_generation([macro_text()])

    assert isinstance(outcome, Accepted)
    assert (outcome.value.start_date, outcome.value.race_date) == (TODAY, RACE_DAY)
    request = adapter.requests[0]
    assert request.job.value == "plan"
    assert request.system == macro_system_text("Instrução")
    turn_text = request.turns[0].text
    for part in (_CONTEXT, '"athlete_name":"Gabi"', REASON, "2026-11-10", "2027-04-18"):
        assert part in turn_text


def test_fenced_macro_is_accepted() -> None:
    outcome, _ = run_generation([f"```json\n{macro_text()}\n```"])

    assert isinstance(outcome, Accepted)


def test_macro_on_a_start_the_model_chose_is_retried() -> None:
    wrong_start = TODAY.replace(day=11)
    outcome, adapter = run_generation(
        [macro_text(start_date=wrong_start), macro_text()]
    )

    assert isinstance(outcome, Accepted)
    assert outcome.attempts == 2
    assert "start_date: plan starts 2026-11-11" in adapter.requests[1].turns[-1].text


def test_two_invalid_macros_are_rejected_in_portuguese() -> None:
    outcome, _ = run_generation(["{}", "{}"])

    assert isinstance(outcome, Rejected)
    assert outcome.chat_message.startswith("O treinador devolveu um plano macro")
