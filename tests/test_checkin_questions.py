"""Tests for the three fixed check-in questions on the console."""

import sqlite3
from datetime import UTC, date, datetime
from io import StringIO

import pytest

from console_fakes import ScriptedConsole
from training_season.checkin_questions import (
    CheckinAnswered,
    CheckinAnswerKind,
    CheckinAnswerRejected,
    CheckinNextQuestion,
    ask_checkin_answers,
    checkin_step,
    record_checkin,
)
from training_season.checkin_store import CheckinAnswers

_FATIGUE = "Treinador: Como está sua fadiga hoje, de 1 a 10?"
_PAIN = "Treinador: Algo está doendo? (s/n)"
_LOCATION = "Treinador: Onde dói?"
_LEVEL = "Treinador: De 1 a 10, quanto dói?"
_SCALE_HINT = "Treinador: Responda com um número inteiro de 1 a 10."
_YES_NO_HINT = "Treinador: Responda s ou n."
_LOCATION_HINT = "Treinador: Diga onde dói, em até 200 caracteres."


def _asked(lines: list[str]) -> tuple[CheckinAnswers | None, list[str]]:
    out = StringIO()
    answers = ask_checkin_answers(ScriptedConsole(lines).read_line, out)
    return answers, out.getvalue().splitlines()


def _checkin_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT count(*) FROM checkin").fetchone()[0])


def test_no_pain_asks_two_questions() -> None:
    answers, shown = _asked(["7", "n"])

    assert answers == CheckinAnswers(7, None, None)
    assert shown == [_FATIGUE, _PAIN]


def test_pain_asks_where_and_how_much() -> None:
    answers, shown = _asked(["4", " S ", "joelho esquerdo", "6"])

    assert answers == CheckinAnswers(4, "joelho esquerdo", 6)
    assert shown == [_FATIGUE, _PAIN, _LOCATION, _LEVEL]


@pytest.mark.parametrize("bad_fatigue", ["0", "11", "abc", "", "1_0", "-3"])
def test_bad_fatigue_shows_the_hint_and_asks_again(bad_fatigue: str) -> None:
    answers, shown = _asked([bad_fatigue, "10", "n"])

    assert answers == CheckinAnswers(10, None, None)
    assert shown == [_FATIGUE, _SCALE_HINT, _FATIGUE, _PAIN]


def test_bad_pain_answer_shows_the_hint_and_asks_again() -> None:
    answers, shown = _asked(["5", "talvez", "N"])

    assert answers == CheckinAnswers(5, None, None)
    assert shown == [_FATIGUE, _PAIN, _YES_NO_HINT, _PAIN]


@pytest.mark.parametrize("bad_location", ["   ", "\x1b", "x" * 201])
def test_bad_location_shows_the_hint_and_asks_again(bad_location: str) -> None:
    answers, shown = _asked(["5", "s", bad_location, "tornozelo", "2"])

    assert answers == CheckinAnswers(5, "tornozelo", 2)
    assert shown == [_FATIGUE, _PAIN, _LOCATION, _LOCATION_HINT, _LOCATION, _LEVEL]


def test_location_drops_control_characters_and_keeps_one_line() -> None:
    answers, _ = _asked(["5", "s", " joel\x1bho\ndireito ", "2"])

    assert answers == CheckinAnswers(5, "joelho direito", 2)


def test_location_of_200_characters_is_kept() -> None:
    answers, _ = _asked(["5", "s", "x" * 200, "2"])

    assert answers == CheckinAnswers(5, "x" * 200, 2)


def test_bad_pain_level_shows_the_hint_and_asks_again() -> None:
    answers, shown = _asked(["5", "s", "joelho", "15", "1"])

    assert answers == CheckinAnswers(5, "joelho", 1)
    assert shown == [_FATIGUE, _PAIN, _LOCATION, _LEVEL, _SCALE_HINT, _LEVEL]


@pytest.mark.parametrize(
    ("lines", "question", "accepted", "kind"),
    [
        ([], "Como está sua fadiga hoje, de 1 a 10?", 0, "scale"),
        (["7"], "Algo está doendo? (s/n)", 1, "yes_no"),
        (["7", "s"], "Onde dói?", 2, "text"),
        (["7", "s", "joelho"], "De 1 a 10, quanto dói?", 3, "scale"),
    ],
)
def test_checkin_step_names_the_next_question_and_its_answer_kind(
    lines: list[str], question: str, accepted: int, kind: CheckinAnswerKind
) -> None:
    assert checkin_step(lines) == CheckinNextQuestion(question, accepted, kind)


def test_checkin_step_rejects_the_first_bad_answer_with_its_hint() -> None:
    step = checkin_step(["7", "x", "joelho"])

    assert step == CheckinAnswerRejected(
        "Algo está doendo? (s/n)", "Responda s ou n.", 1, "yes_no"
    )


def test_checkin_step_cleans_raw_answers_like_the_console() -> None:
    step = checkin_step([" 4 ", " S\x1b ", " joel\x1bho\ndireito ", "6\n"])

    assert step == CheckinAnswered(CheckinAnswers(4, "joelho direito", 6))


def test_checkin_step_ends_after_no_pain() -> None:
    assert checkin_step(["3", "n"]) == CheckinAnswered(CheckinAnswers(3, None, None))


@pytest.mark.parametrize(
    "lines",
    [[], ["5"], ["5", "s"], ["5", "s", "joelho"], ["5", "/sair"]],
    ids=["at-fatigue", "at-pain", "at-location", "at-level", "leave-command"],
)
def test_leaving_before_the_last_answer_stores_nothing(
    migrated_connection: sqlite3.Connection, lines: list[str]
) -> None:
    checkin_id = record_checkin(
        migrated_connection,
        ScriptedConsole(lines).read_line,
        StringIO(),
        today=date(2026, 11, 18),
        now=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
    )

    assert checkin_id is None
    assert _checkin_count(migrated_connection) == 0


def test_valid_answers_store_one_checkin_for_the_local_week(
    migrated_connection: sqlite3.Connection,
) -> None:
    out = StringIO()

    checkin_id = record_checkin(
        migrated_connection,
        ScriptedConsole(["6", "n"]).read_line,
        out,
        today=date(2026, 11, 22),
        now=datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
    )

    row = migrated_connection.execute(
        "SELECT week_start, fatigue, has_pain, created_at FROM checkin WHERE id = ?",
        (checkin_id,),
    ).fetchone()
    assert tuple(row) == ("2026-11-16", 6, 0, "2026-11-22T12:00:00Z")
    assert out.getvalue().splitlines()[-1] == "Treinador: Check-in salvo."
