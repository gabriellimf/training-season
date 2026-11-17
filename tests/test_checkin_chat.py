"""Tests for the check-in conversation steps that the console and the web share."""

from __future__ import annotations

from datetime import UTC, date, datetime
from io import StringIO
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from console_fakes import ScriptedConsole
from llm_fakes import QueuedReplyAdapter, UnavailableAdapter
from training_season.checkin_chat import (
    CHECKIN_INTRO,
    CheckinCoach,
    CheckinConversation,
    CheckinSetup,
    checkin_reply,
    open_checkin_conversation,
    run_checkin,
)
from training_season.checkin_store import CheckinAnswers, store_checkin_answers
from training_season.coach_instruction import load_coach_instruction
from training_season.onboarding_chat import LEAVE_COMMAND
from training_season.onboarding_store import SessionMessage
from training_season.onboarding_turn import (
    MAX_ATHLETE_MESSAGE_LENGTH,
    MAX_SESSION_HISTORY_LENGTH,
)

if TYPE_CHECKING:
    import sqlite3

_WEDNESDAY = date(2026, 11, 18)
_NOW = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)


def _stored_checkin(connection: sqlite3.Connection) -> int:
    return store_checkin_answers(
        connection,
        CheckinAnswers(6, None, None),
        week_start=date(2026, 11, 16),
        now=_NOW,
    )


def _conversation(
    connection: sqlite3.Connection, history: tuple[SessionMessage, ...] = ()
) -> CheckinConversation:
    return CheckinConversation(
        _stored_checkin(connection), "Sistema", "Início", history
    )


def _free_text(connection: sqlite3.Connection, checkin_id: int) -> str | None:
    row = connection.execute(
        "SELECT free_text FROM checkin WHERE id = ?", (checkin_id,)
    ).fetchone()
    return None if row[0] is None else str(row[0])


def test_open_checkin_conversation_carries_the_stored_answers(
    migrated_connection: sqlite3.Connection,
) -> None:
    checkin_id = _stored_checkin(migrated_connection)

    conversation = open_checkin_conversation(
        migrated_connection, checkin_id, "Sistema", _WEDNESDAY
    )

    assert (conversation.checkin_id, conversation.system) == (checkin_id, "Sistema")
    assert conversation.kickoff.startswith("Início do check-in")
    assert "fadiga 6/10" in conversation.kickoff
    assert conversation.history == ()


def test_checkin_reply_stores_the_message_and_grows_the_history(
    migrated_connection: sqlite3.Connection,
) -> None:
    conversation = _conversation(migrated_connection)
    adapter = QueuedReplyAdapter([" Bom treino!\x1b "])

    reply = checkin_reply(migrated_connection, adapter, conversation, "Dormi mal")

    assert (reply.status, reply.chat_message) == ("replied", "Bom treino!")
    assert reply.conversation.history == (
        SessionMessage("user", "Dormi mal"),
        SessionMessage("assistant", "Bom treino!"),
    )
    assert conversation.history == ()
    assert _free_text(migrated_connection, conversation.checkin_id) == "Dormi mal"


def test_checkin_reply_refuses_a_long_message_without_a_call(
    migrated_connection: sqlite3.Connection,
) -> None:
    conversation = _conversation(migrated_connection)
    adapter = QueuedReplyAdapter([])
    text = "x" * (MAX_ATHLETE_MESSAGE_LENGTH + 1)

    reply = checkin_reply(migrated_connection, adapter, conversation, text)

    assert (reply.status, reply.conversation) == ("message_too_long", conversation)
    assert adapter.requests == []
    assert _free_text(migrated_connection, conversation.checkin_id) is None


def test_checkin_reply_ends_a_conversation_that_is_too_long(
    migrated_connection: sqlite3.Connection,
) -> None:
    history = (SessionMessage("assistant", "x" * MAX_SESSION_HISTORY_LENGTH),)
    conversation = _conversation(migrated_connection, history)
    adapter = QueuedReplyAdapter([])

    reply = checkin_reply(migrated_connection, adapter, conversation, "Oi")

    assert reply.status == "conversation_too_long"
    assert adapter.requests == []
    assert _free_text(migrated_connection, conversation.checkin_id) is None


def test_checkin_reply_keeps_the_message_when_the_model_fails(
    migrated_connection: sqlite3.Connection,
) -> None:
    conversation = _conversation(migrated_connection)

    reply = checkin_reply(
        migrated_connection, UnavailableAdapter(), conversation, "Dormi mal"
    )

    assert (reply.status, reply.conversation) == ("model_failed", conversation)
    assert reply.chat_message.startswith("Não consegui falar com o treinador")
    assert _free_text(migrated_connection, conversation.checkin_id) == "Dormi mal"


def test_checkin_intro_carries_no_console_command() -> None:
    assert LEAVE_COMMAND not in CHECKIN_INTRO


def test_console_intro_ends_with_the_leave_hint(
    migrated_connection: sqlite3.Connection,
) -> None:
    out = StringIO()
    setup = CheckinSetup(
        load_coach_instruction({}), ZoneInfo("America/Sao_Paulo"), lambda: _NOW
    )
    coach = CheckinCoach(migrated_connection, QueuedReplyAdapter([]), setup, out)

    code = run_checkin(coach, ScriptedConsole([]).read_line)

    assert code == 0
    assert out.getvalue().startswith(
        f"Treinador: {CHECKIN_INTRO} Digite /sair para terminar.\n"
    )
