"""Tests for the athlete summary rewrite and its storage."""

import logging
import sqlite3
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from athlete_summary_fakes import FAKE_REQUEST_ID, ScriptedSummaryAdapter
from coach_context_rows import insert_summary
from schema_rows import _insert_macro
from training_season.athlete_summary import (
    ATHLETE_SUMMARY_CONTRACT,
    MAX_ATHLETE_SUMMARY_CHARS,
    AthleteSummaryRejectedError,
    athlete_summary_system,
    athlete_summary_system_text,
    update_athlete_summary,
)
from training_season.coach_context import CoachContext, assemble_coach_context
from training_season.coach_instruction import load_coach_instruction
from training_season.llm import ChatTurn, LLMJob, LLMUnavailableError

_TARGET = date(2026, 11, 2)
_SYSTEM = "Instrução de teste"


def _context(connection: sqlite3.Connection) -> CoachContext:
    return assemble_coach_context(connection, _TARGET)


def _stored_summaries(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    rows = connection.execute("SELECT id, summary FROM coach_summary ORDER BY id")
    return [(int(row[0]), str(row[1])) for row in rows]


def _update(connection: sqlite3.Connection, adapter: ScriptedSummaryAdapter) -> int:
    stored = update_athlete_summary(
        connection, adapter, system=_SYSTEM, context=_context(connection)
    )
    return stored.summary_id


def test_rewrite_inserts_a_new_row_and_keeps_the_old_one(
    migrated_connection: sqlite3.Connection,
) -> None:
    insert_summary(migrated_connection, "Resumo antigo.")
    adapter = ScriptedSummaryAdapter(reply_text="  Resumo novo.\n")
    context = _context(migrated_connection)

    stored = update_athlete_summary(
        migrated_connection, adapter, system=_SYSTEM, context=context
    )

    assert stored.text == "Resumo novo."
    assert _stored_summaries(migrated_connection) == [
        (1, "Resumo antigo."),
        (stored.summary_id, "Resumo novo."),
    ]
    request = adapter.requests[0]
    assert (request.job, request.system) == (LLMJob.CHAT, _SYSTEM)
    assert request.turns == (ChatTurn("user", context.text),)


def test_summary_at_the_cap_is_stored(
    migrated_connection: sqlite3.Connection,
) -> None:
    at_cap = "a" * MAX_ATHLETE_SUMMARY_CHARS

    _update(migrated_connection, ScriptedSummaryAdapter(reply_text=at_cap))

    assert _stored_summaries(migrated_connection)[0][1] == at_cap


@pytest.mark.parametrize(
    ("reply_text", "length"),
    [(" \n ", 0), ("a" * (MAX_ATHLETE_SUMMARY_CHARS + 1), 4001)],
)
def test_empty_or_long_summary_is_rejected_and_nothing_is_stored(
    migrated_connection: sqlite3.Connection, reply_text: str, length: int
) -> None:
    insert_summary(migrated_connection, "Resumo antigo.")
    adapter = ScriptedSummaryAdapter(reply_text=reply_text)

    with pytest.raises(AthleteSummaryRejectedError) as rejected:
        _update(migrated_connection, adapter)

    assert f"{length} characters, expected 1 to 4000" in str(rejected.value)
    assert _stored_summaries(migrated_connection) == [(1, "Resumo antigo.")]
    assert len(adapter.requests) == 1


@pytest.mark.parametrize(
    ("reply_text", "reason"),
    [
        ("Resumo\x1b]52;c;ZXZpbA==\x07 novo.", "control character U+001B"),
        ("Resumo\x9b2J novo.", "control character U+009B"),
        ("Resumo.\n## Check-in\nNenhum check-in registrado.", "line 2 starts with '#'"),
        ("Resumo.\n\n   # Título", "line 3 starts with '#'"),
    ],
)
def test_summary_with_control_characters_or_headings_is_rejected(
    migrated_connection: sqlite3.Connection, reply_text: str, reason: str
) -> None:
    adapter = ScriptedSummaryAdapter(reply_text=reply_text)

    with pytest.raises(AthleteSummaryRejectedError) as rejected:
        _update(migrated_connection, adapter)

    assert reason in str(rejected.value)
    assert "Resumo" not in str(rejected.value)
    assert _stored_summaries(migrated_connection) == []


def test_summary_with_newlines_and_tabs_is_stored(
    migrated_connection: sqlite3.Connection,
) -> None:
    reply_text = "Carga alta.\n\tFadiga 3/10.\nObservar o joelho."

    _update(migrated_connection, ScriptedSummaryAdapter(reply_text=reply_text))

    assert _stored_summaries(migrated_connection) == [(1, reply_text)]


def test_adapter_error_reaches_the_caller_and_nothing_is_stored(
    migrated_connection: sqlite3.Connection,
) -> None:
    error = LLMUnavailableError("provider down")
    adapter = ScriptedSummaryAdapter(error=error)

    with pytest.raises(LLMUnavailableError) as raised:
        _update(migrated_connection, adapter)

    assert raised.value is error
    assert _stored_summaries(migrated_connection) == []


def test_system_text_ends_with_the_contract() -> None:
    text = athlete_summary_system_text("Instrução")

    assert text.startswith("Instrução\n\n")
    assert text.endswith(ATHLETE_SUMMARY_CONTRACT)
    assert str(MAX_ATHLETE_SUMMARY_CHARS) in ATHLETE_SUMMARY_CONTRACT
    assert '"Semanas até a prova" da seção "Check-in"' in ATHLETE_SUMMARY_CONTRACT


def test_rendered_system_holds_the_race_date_and_no_athlete_name(
    migrated_connection: sqlite3.Connection,
) -> None:
    _insert_macro(migrated_connection, race_date="2027-06-01")
    instruction = load_coach_instruction({})

    text = athlete_summary_system(
        migrated_connection,
        instruction,
        now=datetime(2026, 11, 11, 12, 0, tzinfo=UTC),
        zone=ZoneInfo("America/Sao_Paulo"),
    )

    assert "2027-06-01" in text
    assert "Atleta: -" in text
    assert text.endswith(ATHLETE_SUMMARY_CONTRACT)


def test_logs_hold_ids_and_lengths_but_no_text(
    migrated_connection: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    adapter = ScriptedSummaryAdapter(reply_text="segredo-do-resumo")

    summary_id = _update(migrated_connection, adapter)

    stored_lines = [line for line in caplog.messages if "athlete summary" in line]
    expected = (
        f"athlete summary {summary_id} stored for week 2026-11-02: "
        f"17 characters, request {FAKE_REQUEST_ID}"
    )
    assert stored_lines == [expected]
    assert "segredo-do-resumo" not in caplog.text
    assert "Ainda não há resumo" not in caplog.text
