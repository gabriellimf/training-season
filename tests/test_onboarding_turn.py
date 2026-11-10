"""Tests for one onboarding turn: interview, completion, retries and failures."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from llm_fakes import QueuedReplyAdapter, UnavailableAdapter
from onboarding_rows import REASON, TODAY, macro_text, result_text
from training_season.coach_context import assemble_coach_context
from training_season.coach_context_store import read_active_macro, read_profile_json
from training_season.llm import LLMUnavailableError
from training_season.macro_store import read_macro_history
from training_season.onboarding_contract import (
    ANSWER_RETRY_REQUEST,
    ONBOARDING_RETRY_REQUEST,
    onboarding_system_text,
)
from training_season.onboarding_store import (
    append_exchange,
    current_open_session,
    open_session,
    session_messages,
)
from training_season.onboarding_turn import (
    MAX_ATHLETE_MESSAGE_LENGTH,
    MAX_SESSION_HISTORY_LENGTH,
    OnboardingTurnResult,
    onboarding_turn,
)

if TYPE_CHECKING:
    import sqlite3

    from training_season.llm import LLMAdapter

_RESULT_FAILED = (
    "Não consegui registrar os dados do onboarding. Responda para tentar de novo."
)


def tagged(visible: str, body: str = "") -> str:
    return f"{visible}\n<onboarding_result>{body or result_text()}</onboarding_result>"


def run_turn(
    connection: sqlite3.Connection, adapter: LLMAdapter, user_text: str = "Oi"
) -> OnboardingTurnResult:
    session_id = current_open_session(connection)
    assert session_id is not None
    return onboarding_turn(
        connection,
        adapter,
        session_id=session_id,
        instruction_text="Instrução",
        user_text=user_text,
        today=TODAY,
    )


def stored(connection: sqlite3.Connection, session_id: int) -> list[tuple[str, str]]:
    return [(m.role, m.content) for m in session_messages(connection, session_id)]


def nothing_written(connection: sqlite3.Connection) -> bool:
    return (
        read_profile_json(connection) is None and read_macro_history(connection) == ()
    )


@pytest.fixture
def session_id(migrated_connection: sqlite3.Connection) -> int:
    return open_session(migrated_connection)


def test_reply_without_the_tag_is_a_stored_interview_turn(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    adapter = QueuedReplyAdapter(["Qual é a sua prova?"])

    outcome = run_turn(migrated_connection, adapter)

    assert outcome == OnboardingTurnResult("asking", "Qual é a sua prova?", None)
    assert stored(migrated_connection, session_id) == [
        ("user", "Oi"),
        ("assistant", "Qual é a sua prova?"),
    ]
    assert current_open_session(migrated_connection) == session_id
    request = adapter.requests[0]
    assert request.job.value == "chat"
    assert request.system == onboarding_system_text("Instrução")


def test_next_turn_sends_kickoff_history_and_new_text(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    adapter = QueuedReplyAdapter(["Qual é a sua prova?", "Quando é?"])
    run_turn(migrated_connection, adapter, "Oi")

    run_turn(migrated_connection, adapter, "Uma maratona")

    turns = adapter.requests[1].turns
    assert [turn.role for turn in turns] == ["user", "assistant", "user"]
    context_text = assemble_coach_context(migrated_connection, TODAY).text
    assert context_text in turns[0].text
    assert "primeiro onboarding" in turns[0].text
    assert turns[0].text.endswith("Mensagem do atleta:\nOi")
    assert turns[-1].text == "Uma maratona"


def test_valid_result_completes_the_session_in_one_write(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    adapter = QueuedReplyAdapter([tagged("Montando seu plano."), macro_text()])

    outcome = run_turn(migrated_connection, adapter, "Pode montar")

    assert [request.job.value for request in adapter.requests] == ["chat", "plan"]
    assert outcome.status == "completed"
    assert outcome.chat_message == "Montando seu plano."
    history = read_macro_history(migrated_connection)
    assert [(e.macro_plan_id, e.status) for e in history] == [(1, "active")]
    assert history[0].revisions[0].reason == REASON
    assert outcome.macro_plan_id == 1
    assert read_profile_json(migrated_connection) is not None
    assert current_open_session(migrated_connection) is None
    assert stored(migrated_connection, session_id) == [
        ("user", "Pode montar"),
        ("assistant", "Montando seu plano."),
    ]


def test_result_with_no_visible_text_stores_the_closing_text(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    adapter = QueuedReplyAdapter([tagged(""), macro_text()])

    outcome = run_turn(migrated_connection, adapter)

    assert outcome.chat_message == "Onboarding concluído. Plano salvo."
    assert stored(migrated_connection, session_id)[-1] == (
        "assistant",
        "Onboarding concluído. Plano salvo.",
    )


def test_invalid_result_is_retried_with_the_block_request(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    invalid = tagged("Montando.", result_text(reason=""))
    adapter = QueuedReplyAdapter([invalid, tagged(""), macro_text()])

    outcome = run_turn(migrated_connection, adapter)

    assert outcome.status == "completed"
    assert adapter.requests[1].turns[-1].text.endswith(ONBOARDING_RETRY_REQUEST)
    assert stored(migrated_connection, session_id)[-1] == ("assistant", "Montando.")


def test_invalid_result_twice_keeps_the_session_open(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    invalid = tagged("Montando.", result_text(reason=""))
    adapter = QueuedReplyAdapter([invalid, invalid])

    outcome = run_turn(migrated_connection, adapter)

    assert outcome == OnboardingTurnResult(
        "result_rejected", f"Montando.\n\n{_RESULT_FAILED}", None
    )
    assert nothing_written(migrated_connection)
    assert current_open_session(migrated_connection) == session_id
    assert stored(migrated_connection, session_id)[-1] == (
        "assistant",
        f"Montando.\n\n{_RESULT_FAILED}",
    )


def test_invalid_macro_twice_writes_nothing(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    adapter = QueuedReplyAdapter([tagged("Montando."), "{}", "{}"])

    outcome = run_turn(migrated_connection, adapter)

    assert outcome.status == "macro_rejected"
    assert outcome.chat_message.startswith("O treinador devolveu um plano macro")
    assert nothing_written(migrated_connection)
    assert stored(migrated_connection, session_id) == []
    assert current_open_session(migrated_connection) == session_id


def test_reopen_kickoff_holds_the_current_macro_and_archives_it(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    run_turn(migrated_connection, QueuedReplyAdapter([tagged(""), macro_text()]))
    reopened = open_session(migrated_connection)
    adapter = QueuedReplyAdapter([tagged("Plano novo."), macro_text()])

    run_turn(migrated_connection, adapter, "Mudei de prova")

    kickoff = adapter.requests[0].turns[0].text
    active = read_active_macro(migrated_connection)
    assert "revisão do objetivo" in kickoff
    assert '"phases":' in kickoff
    history = read_macro_history(migrated_connection)
    assert [e.status for e in history] == ["archived", "active"]
    assert active is not None
    assert active.macro_plan_id == 2
    assert stored(migrated_connection, reopened)[-1] == ("assistant", "Plano novo.")


def test_empty_reply_is_retried_with_a_request_to_answer_the_athlete(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    adapter = QueuedReplyAdapter(["  ", "Qual é a sua prova?"])

    outcome = run_turn(migrated_connection, adapter)

    assert outcome.status == "asking"
    retry_text = adapter.requests[1].turns[-1].text
    assert retry_text.endswith(ANSWER_RETRY_REQUEST)
    assert ONBOARDING_RETRY_REQUEST not in retry_text


def test_line_at_the_cap_is_sent(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    adapter = QueuedReplyAdapter(["Anotado."])

    outcome = run_turn(migrated_connection, adapter, "x" * MAX_ATHLETE_MESSAGE_LENGTH)

    assert outcome.status == "asking"


def test_line_over_the_cap_stores_nothing_and_calls_no_model(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    adapter = QueuedReplyAdapter([])
    too_long = "x" * (MAX_ATHLETE_MESSAGE_LENGTH + 1)

    outcome = run_turn(migrated_connection, adapter, too_long)

    assert outcome.status == "message_too_long"
    assert f"{MAX_ATHLETE_MESSAGE_LENGTH} caracteres" in outcome.chat_message
    assert adapter.requests == []
    assert stored(migrated_connection, session_id) == []


def test_history_over_the_cap_stops_the_model_call(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    long_text = "x" * MAX_ATHLETE_MESSAGE_LENGTH
    while sum(len(text) for _, text in stored(migrated_connection, session_id)) < (
        MAX_SESSION_HISTORY_LENGTH
    ):
        append_exchange(migrated_connection, session_id, long_text, long_text)
    stored_before = stored(migrated_connection, session_id)
    adapter = QueuedReplyAdapter([])

    outcome = run_turn(migrated_connection, adapter, "Oi")

    assert outcome.status == "session_too_long"
    assert "training-season-onboarding discard" in outcome.chat_message
    assert adapter.requests == []
    assert stored(migrated_connection, session_id) == stored_before


def test_provider_error_stores_nothing(
    migrated_connection: sqlite3.Connection, session_id: int
) -> None:
    with pytest.raises(LLMUnavailableError):
        run_turn(migrated_connection, UnavailableAdapter())

    assert stored(migrated_connection, session_id) == []


def test_logs_hold_no_profile_or_message_text(
    migrated_connection: sqlite3.Connection,
    session_id: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = QueuedReplyAdapter([tagged("Montando seu plano."), macro_text()])
    caplog.set_level(logging.DEBUG)

    run_turn(migrated_connection, adapter, "Segredo do atleta")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for private in ("Segredo do atleta", "Montando", "Gabi", "Canelite"):
        assert private not in logged
    assert f"session={session_id}" in logged
