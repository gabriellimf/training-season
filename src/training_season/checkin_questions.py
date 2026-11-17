"""The three fixed check-in questions on a console, then the stored check-in."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from training_season.checkin_store import (
    CheckinAnswers,
    checkin_week_of,
    store_checkin_answers,
)
from training_season.coach_context_lines import MAX_PAIN_LOCATION_CHARS
from training_season.onboarding_chat import (
    LEAVE_COMMAND,
    cleaned_athlete_text,
    read_stripped_line,
    say,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence
    from datetime import date, datetime
    from typing import TextIO

type CheckinAnswerKind = Literal["scale", "yes_no", "text"]

CHECKIN_SAVED_TEXT = "Check-in salvo."
_SCALE = range(1, 11)
_SCALE_DIGITS = re.compile(r"[0-9]{1,2}")
_SCALE_HINT = "Responda com um número inteiro de 1 a 10."
_YES_NO = {"s": True, "n": False}


@dataclass(frozen=True, slots=True)
class _CheckinQuestion[T]:
    text: str
    hint: str
    parse: Callable[[str], T | None]
    kind: CheckinAnswerKind


def _scale_value(line: str) -> int | None:
    if _SCALE_DIGITS.fullmatch(line) is None:
        return None
    value = int(line)
    return value if value in _SCALE else None


def _has_pain(line: str) -> bool | None:
    return _YES_NO.get(line.lower())


def _pain_location(line: str) -> str | None:
    location = line.replace("\n", " ").strip()
    if not location or len(location) > MAX_PAIN_LOCATION_CHARS:
        return None
    return location


_FATIGUE = _CheckinQuestion(
    "Como está sua fadiga hoje, de 1 a 10?", _SCALE_HINT, _scale_value, "scale"
)
_PAIN = _CheckinQuestion(
    "Algo está doendo? (s/n)", "Responda s ou n.", _has_pain, "yes_no"
)
_LOCATION = _CheckinQuestion(
    "Onde dói?",
    f"Diga onde dói, em até {MAX_PAIN_LOCATION_CHARS} caracteres.",
    _pain_location,
    "text",
)
_PAIN_LEVEL = _CheckinQuestion(
    "De 1 a 10, quanto dói?", _SCALE_HINT, _scale_value, "scale"
)


@dataclass(frozen=True, slots=True)
class CheckinNextQuestion:
    """Every answer so far is valid; `question` is the one to ask next."""

    question: str
    accepted: int
    kind: CheckinAnswerKind


@dataclass(frozen=True, slots=True)
class CheckinAnswerRejected:
    """The answer at index `accepted` is invalid; show `hint`, then `question`."""

    question: str
    hint: str
    accepted: int
    kind: CheckinAnswerKind


@dataclass(frozen=True, slots=True)
class CheckinAnswered:
    """The answers cover every question the check-in needs."""

    answers: CheckinAnswers


type CheckinStep = CheckinNextQuestion | CheckinAnswerRejected | CheckinAnswered
type _Pending = CheckinNextQuestion | CheckinAnswerRejected


@dataclass(frozen=True, slots=True)
class _Parsed[T]:
    value: T


def _answer_at[T](
    question: _CheckinQuestion[T], lines: Sequence[str], index: int
) -> _Parsed[T] | _Pending:
    if index >= len(lines):
        return CheckinNextQuestion(question.text, index, question.kind)
    value = question.parse(cleaned_athlete_text(lines[index]))
    if value is None:
        return CheckinAnswerRejected(question.text, question.hint, index, question.kind)
    return _Parsed(value)


def _pain_step(fatigue: int, lines: Sequence[str]) -> CheckinStep:
    location = _answer_at(_LOCATION, lines, 2)
    if not isinstance(location, _Parsed):
        return location
    level = _answer_at(_PAIN_LEVEL, lines, 3)
    if not isinstance(level, _Parsed):
        return level
    return CheckinAnswered(CheckinAnswers(fatigue, location.value, level.value))


def checkin_step(lines: Sequence[str]) -> CheckinStep:
    """
    Parse the raw answers in question order and say what the check-in needs next.

    Answers after the first invalid one, or after the last question, are ignored.
    Used as `step = checkin_step(["7", "s"])`
    """
    fatigue = _answer_at(_FATIGUE, lines, 0)
    if not isinstance(fatigue, _Parsed):
        return fatigue
    has_pain = _answer_at(_PAIN, lines, 1)
    if not isinstance(has_pain, _Parsed):
        return has_pain
    if not has_pain.value:
        return CheckinAnswered(CheckinAnswers(fatigue.value, None, None))
    return _pain_step(fatigue.value, lines)


def _show_step(step: _Pending, out: TextIO) -> None:
    if isinstance(step, CheckinAnswerRejected):
        say(out, step.hint)
    say(out, step.question)


def ask_checkin_answers(
    read_line: Callable[[str], str], out: TextIO
) -> CheckinAnswers | None:
    """
    Ask fatigue, pain, and where and how much it hurts; None on EOF or `/sair`.

    A bad answer prints a hint and asks the same question again.
    Used as `answers = ask_checkin_answers(input, sys.stdout)`
    """
    lines: list[str] = []
    step = checkin_step(lines)
    while not isinstance(step, CheckinAnswered):
        _show_step(step, out)
        line = read_stripped_line(read_line, out)
        if line is None or line == LEAVE_COMMAND:
            return None
        lines = [*lines[: step.accepted], line]
        step = checkin_step(lines)
    return step.answers


def record_checkin(
    connection: sqlite3.Connection,
    read_line: Callable[[str], str],
    out: TextIO,
    *,
    today: date,
    now: datetime,
) -> int | None:
    """
    Ask the fixed questions and store the answers for the week of `today`.

    Returns the new check-in id, or None when the athlete left before the end.
    Used as `checkin_id = record_checkin(connection, input, out, today=d, now=now)`
    """
    answers = ask_checkin_answers(read_line, out)
    if answers is None:
        return None
    checkin_id = store_checkin_answers(
        connection, answers, week_start=checkin_week_of(today), now=now
    )
    say(out, CHECKIN_SAVED_TEXT)
    return checkin_id
