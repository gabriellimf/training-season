"""Run record of the weekly cycle command in the `cycle_run` table."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from training_season.database import transaction
from training_season.onboarding_store import write_cycle_message
from training_season.timeformat import utc_now

if TYPE_CHECKING:
    import sqlite3

CycleTrigger = Literal["scheduled", "manual"]
CycleRunOutcome = Literal["done", "failed", "crashed", "interrupted", "refused"]

_INSERT_OPEN_RUN = """
INSERT INTO cycle_run (trigger, started_at) VALUES (?, ?)
RETURNING id, started_at
"""
_INSERT_ENDED_RUN = """
INSERT INTO cycle_run
    (trigger, started_at, ended_at, outcome, exit_code, message_id)
VALUES (?, ?, ?, ?, ?, ?)
"""
_FINISH_RUN = """
UPDATE cycle_run SET ended_at = ?, outcome = ?, exit_code = ?, message_id = ?
WHERE id = ? AND ended_at IS NULL
"""
_OPEN_RUNS = "SELECT id, started_at FROM cycle_run WHERE ended_at IS NULL ORDER BY id"


class CycleRunStateError(Exception):
    """The cycle run row is not in the state the write needs."""


@dataclass(frozen=True, slots=True)
class OpenCycleRun:
    """A run row with no end yet: its id and its UTC start timestamp."""

    run_id: int
    started_at: str


@dataclass(frozen=True, slots=True)
class CycleRunEnd:
    """How a run ended, and the chat message it stored, if any."""

    outcome: CycleRunOutcome
    exit_code: int
    message_id: int | None


@dataclass(frozen=True, slots=True)
class CycleRunNotice:
    """How a run ended, with the chat message still to be stored for it."""

    outcome: CycleRunOutcome
    exit_code: int
    content: str = field(repr=False)
    weekly_plan_id: int | None = None


def start_cycle_run(
    connection: sqlite3.Connection, trigger: CycleTrigger
) -> OpenCycleRun:
    """
    Store a new open run row and return it.

    Used as `run = start_cycle_run(connection, "scheduled")`
    """
    with transaction(connection):
        row = connection.execute(_INSERT_OPEN_RUN, (trigger, utc_now())).fetchone()
    return OpenCycleRun(int(row[0]), str(row[1]))


def open_cycle_runs(connection: sqlite3.Connection) -> tuple[OpenCycleRun, ...]:
    """
    Return every run row with no end, oldest first.

    Used as `for run in open_cycle_runs(connection): ...`
    """
    rows = connection.execute(_OPEN_RUNS).fetchall()
    return tuple(OpenCycleRun(int(row[0]), str(row[1])) for row in rows)


def _write_end(connection: sqlite3.Connection, run_id: int, end: CycleRunEnd) -> None:
    cursor = connection.execute(
        _FINISH_RUN, (utc_now(), end.outcome, end.exit_code, end.message_id, run_id)
    )
    if cursor.rowcount != 1:
        raise CycleRunStateError(
            f"cycle run {run_id} is not open, expected one open run to finish"
        )


def finish_cycle_run(
    connection: sqlite3.Connection, run_id: int, end: CycleRunEnd
) -> None:
    """
    Close an open run row with its outcome.

    Used as `finish_cycle_run(connection, run.run_id, CycleRunEnd("done", 0, 12))`
    """
    with transaction(connection):
        _write_end(connection, run_id, end)


def finish_cycle_run_with_notice(
    connection: sqlite3.Connection, run_id: int, notice: CycleRunNotice
) -> None:
    """
    Store the notice as a chat message and close the open run row on it, together.

    Used as `finish_cycle_run_with_notice(connection, 3, CycleRunNotice(...))`
    """
    with transaction(connection):
        message_id = write_cycle_message(
            connection, notice.content, notice.weekly_plan_id
        )
        end = CycleRunEnd(notice.outcome, notice.exit_code, message_id)
        _write_end(connection, run_id, end)


def record_ended_cycle_run(
    connection: sqlite3.Connection, trigger: CycleTrigger, notice: CycleRunNotice
) -> None:
    """
    Store a run that ended as it started, with its notice as a chat message.

    Used as `record_ended_cycle_run(connection, "manual", CycleRunNotice(...))`
    """
    with transaction(connection):
        message_id = write_cycle_message(connection, notice.content, None)
        now = utc_now()
        connection.execute(
            _INSERT_ENDED_RUN,
            (trigger, now, now, notice.outcome, notice.exit_code, message_id),
        )
