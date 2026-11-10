"""Tests for the onboarding contract: system text, turns and the result block."""

import pytest

from onboarding_rows import TODAY, result_text
from training_season.athlete_profile import OnboardingResultError
from training_season.llm import ChatTurn
from training_season.onboarding_contract import (
    ANSWER_RETRY_REQUEST,
    ONBOARDING_CONTRACT,
    ONBOARDING_RETRY_REQUEST,
    conversation_turns,
    interview_reply_from_text,
    interview_retry_request,
    kickoff_text,
    onboarding_system_text,
    split_result,
)
from training_season.onboarding_store import SessionMessage


def tagged(visible: str, body: str) -> str:
    return f"{visible}\n<onboarding_result>{body}</onboarding_result>"


def test_system_text_appends_the_contract_to_the_instruction() -> None:
    assert onboarding_system_text("Instrução") == f"Instrução\n\n{ONBOARDING_CONTRACT}"


def test_contract_names_the_result_block_and_every_field() -> None:
    for name in (
        "<onboarding_result>",
        "</onboarding_result>",
        "athlete_name",
        "race_date",
        "running_history",
        "injuries",
        "preferred_days",
        "reason",
    ):
        assert name in ONBOARDING_CONTRACT


def test_kickoff_holds_the_context_and_the_first_run_flag() -> None:
    text = kickoff_text("## Plano macro\nSem plano", reopening=False)

    assert "## Plano macro\nSem plano" in text
    assert "primeiro onboarding" in text
    assert "revisão" not in text


def test_kickoff_on_reopen_says_the_goal_changes() -> None:
    assert "revisão do objetivo" in kickoff_text("Contexto", reopening=True)


def test_first_turn_joins_the_kickoff_and_the_athlete_text() -> None:
    turns = conversation_turns("Início", (), "Oi")

    assert turns == [ChatTurn("user", "Início\n\nMensagem do atleta:\nOi")]


def test_turns_replay_the_history_and_end_with_the_new_user_text() -> None:
    history = (
        SessionMessage("user", "Oi"),
        SessionMessage("assistant", "Qual é a sua prova?"),
    )

    turns = conversation_turns("Início", history, "Uma maratona")

    assert [turn.role for turn in turns] == ["user", "assistant", "user"]
    assert turns[0].text == "Início\n\nMensagem do atleta:\nOi"
    assert turns[1:] == [
        ChatTurn("assistant", "Qual é a sua prova?"),
        ChatTurn("user", "Uma maratona"),
    ]


def test_reply_without_the_tag_is_all_visible() -> None:
    split = split_result("  Qual é a sua prova?  ")

    assert (split.visible, split.result_json) == ("Qual é a sua prova?", None)


def test_tag_is_cut_from_the_visible_text() -> None:
    split = split_result("Antes\n<onboarding_result>{}</onboarding_result>\nDepois")

    assert (split.visible, split.result_json) == ("Antes\n\nDepois", "{}")


def test_tag_without_its_end_takes_the_rest_as_the_result() -> None:
    split = split_result('Pronto.\n<onboarding_result>{"profile": ')

    assert (split.visible, split.result_json) == ("Pronto.", '{"profile":')


def test_interview_reply_without_the_tag_has_no_result() -> None:
    reply = interview_reply_from_text("Quando é a prova?", TODAY)

    assert (reply.visible, reply.result) == ("Quando é a prova?", None)


def test_interview_reply_with_a_fenced_valid_result_parses_it() -> None:
    body = f"```json\n{result_text()}\n```"

    reply = interview_reply_from_text(tagged("Plano a caminho.", body), TODAY)

    assert reply.visible == "Plano a caminho."
    assert reply.result is not None
    assert reply.result.profile.athlete_name == "Gabi"


def test_interview_reply_with_an_invalid_result_raises_its_lines() -> None:
    with pytest.raises(OnboardingResultError) as raised:
        interview_reply_from_text(tagged("Pronto.", result_text(reason="")), TODAY)

    assert any(line.startswith("reason") for line in raised.value.errors)


def test_empty_reply_without_the_tag_is_rejected() -> None:
    with pytest.raises(OnboardingResultError, match="reply has no text"):
        interview_reply_from_text("   ", TODAY)


def test_visible_text_loses_terminal_control_sequences() -> None:
    reply = "Olá\x1b]52;c;abc\x07 atleta\x1b[2J\r\nTchau\u200b"

    split = split_result(tagged(reply, "{}"))

    assert split.visible == "Olá]52;c;abc atleta[2J\nTchau"
    assert split.result_json == "{}"


def test_reply_of_control_characters_only_is_rejected() -> None:
    with pytest.raises(OnboardingResultError, match="reply has no text"):
        interview_reply_from_text("\x1b\x07", TODAY)


def test_retry_after_an_invalid_block_asks_for_the_corrected_block() -> None:
    first = tagged("Pronto.", result_text(reason=""))

    assert interview_retry_request(first) == ONBOARDING_RETRY_REQUEST


def test_retry_after_an_empty_reply_asks_for_a_reply_to_the_athlete() -> None:
    assert interview_retry_request("  ") == ANSWER_RETRY_REQUEST
    assert "onboarding_result" not in ANSWER_RETRY_REQUEST
