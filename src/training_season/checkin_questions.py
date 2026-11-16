"""The three fixed check-in questions on a console, then the stored check-in."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.checkin_store import (
    CheckinAnswers,
    checkin_week_of,
    store_checkin_answers,
)
from training_season.coach_context_lines import MAX_PAIN_LOCATION_CHARS
from training_season.onboarding_chat import LEAVE_COMMAND, read_stripped_line, say

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import date, datetime
    from typing import TextIO

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
    "Como está sua fadiga hoje, de 1 a 10?", _SCALE_HINT, _scale_value
)
_PAIN = _CheckinQuestion("Algo está doendo? (s/n)", "Responda s ou n.", _has_pain)
_LOCATION = _CheckinQuestion(
    "Onde dói?",
    f"Diga onde dói, em até {MAX_PAIN_LOCATION_CHARS} caracteres.",
    _pain_location,
)
_PAIN_LEVEL = _CheckinQuestion("De 1 a 10, quanto dói?", _SCALE_HINT, _scale_value)


def _ask[T](
    question: _CheckinQuestion[T], read_line: Callable[[str], str], out: TextIO
) -> T | None:
    while True:
        say(out, question.text)
        line = read_stripped_line(read_line, out)
        if line is None or line == LEAVE_COMMAND:
            return None
        answer = question.parse(line)
        if answer is not None:
            return answer
        say(out, question.hint)


def _pain_answers(
    read_line: Callable[[str], str], out: TextIO
) -> tuple[str, int] | None:
    location = _ask(_LOCATION, read_line, out)
    if location is None:
        return None
    level = _ask(_PAIN_LEVEL, read_line, out)
    return None if level is None else (location, level)


def ask_checkin_answers(
    read_line: Callable[[str], str], out: TextIO
) -> CheckinAnswers | None:
    """
    Ask fatigue, pain, and where and how much it hurts; None on EOF or `/sair`.

    A bad answer prints a hint and asks the same question again.
    Used as `answers = ask_checkin_answers(input, sys.stdout)`
    """
    fatigue = _ask(_FATIGUE, read_line, out)
    if fatigue is None:
        return None
    has_pain = _ask(_PAIN, read_line, out)
    if has_pain is None:
        return None
    if not has_pain:
        return CheckinAnswers(fatigue, None, None)
    pain = _pain_answers(read_line, out)
    return None if pain is None else CheckinAnswers(fatigue, *pain)


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
