"""Check-in rows: the three fixed answers and the athlete's free text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from training_season.database import transaction
from training_season.timeformat import utc_timestamp
from training_season.weekly_cycle import next_week_range

if TYPE_CHECKING:
    import sqlite3
    from datetime import date, datetime

_INSERT_CHECKIN = """
INSERT INTO checkin
    (week_start, fatigue, has_pain, pain_location, pain_level, created_at)
VALUES (?, ?, ?, ?, ?, ?)
RETURNING id
"""
_APPEND_FREE_TEXT = """
UPDATE checkin
SET free_text = coalesce(free_text || char(10), '') || ?
WHERE id = ?
"""


class CheckinStoreError(ValueError):
    """The check-in write does not fit the stored rows; the message says why."""


@dataclass(frozen=True, slots=True)
class CheckinAnswers:
    """The answers to the three fixed questions; no pain means both pain fields None."""

    fatigue: int
    pain_location: str | None
    pain_level: int | None


def checkin_week_of(today: date) -> date:
    """
    The Monday of the local week of `today`, so the next Sunday plan reads it.

    Used as `checkin_week_of(date(2026, 11, 18))` -> `date(2026, 11, 16)`
    """
    return next_week_range(today)[0] - timedelta(days=7)


def store_checkin_answers(
    connection: sqlite3.Connection,
    answers: CheckinAnswers,
    *,
    week_start: date,
    now: datetime,
) -> int:
    """
    Store one check-in with no free text yet and return its id.

    Used as `store_checkin_answers(connection, answers, week_start=monday, now=now)`
    """
    has_pain = 0 if answers.pain_location is None else 1
    values = (
        week_start.isoformat(),
        answers.fatigue,
        has_pain,
        answers.pain_location,
        answers.pain_level,
        utc_timestamp(now),
    )
    with transaction(connection):
        row = connection.execute(_INSERT_CHECKIN, values).fetchone()
    return int(row[0])


def append_checkin_message(
    connection: sqlite3.Connection, checkin_id: int, text: str
) -> None:
    """
    Add one athlete message to the check-in free text, on a new line.

    Used as `append_checkin_message(connection, checkin_id, "dormi mal")`
    """
    if not text.strip():
        raise CheckinStoreError(
            f"check-in message must be non-blank text, got {text!r}"
        )
    with transaction(connection):
        cursor = connection.execute(_APPEND_FREE_TEXT, (text, checkin_id))
        if cursor.rowcount != 1:
            raise CheckinStoreError(
                f"check-in {checkin_id} does not exist, expected one stored check-in"
            )
