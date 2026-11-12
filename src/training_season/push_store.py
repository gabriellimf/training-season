"""SQLite rows for pushed weeks: one commit per state change of a workout."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from training_season.database import transaction
from training_season.garmin_pull_base import canonical_json
from training_season.timeformat import utc_now

if TYPE_CHECKING:
    import sqlite3

    from training_season.push_week import PushWeek

_UPSERT_WEEKLY_PLAN = """
INSERT INTO weekly_plan (
    macro_plan_id, week_start, week_end, raw_json, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (macro_plan_id, week_start) DO UPDATE SET
    week_end = excluded.week_end,
    raw_json = excluded.raw_json,
    updated_at = excluded.updated_at
RETURNING id
"""


class NoActiveMacroPlanError(Exception):
    """No macro plan is active, so a pushed week has no plan to belong to."""


@dataclass(frozen=True, slots=True)
class StoredWorkout:
    """One workout row this app pushed, with its Garmin ids as decimal text."""

    row_id: int
    schedule_date: date
    garmin_workout_id: str
    garmin_schedule_id: str | None


def active_macro_plan_id(connection: sqlite3.Connection) -> int:
    """
    Return the id of the one active macro plan, or fail before any Garmin call.

    Used as `active_macro_plan_id(connection)`
    """
    row = connection.execute(
        "SELECT id FROM macro_plan WHERE status = 'active'"
    ).fetchone()
    if row is None:
        raise NoActiveMacroPlanError(
            "no active macro plan, expected one macro_plan row with status 'active'"
        )
    return int(row[0])


def upsert_weekly_plan(
    connection: sqlite3.Connection, macro_plan_id: int, week: PushWeek
) -> int:
    """
    Store the canonical week JSON on (macro plan, week start) and return its id.

    Used as `upsert_weekly_plan(connection, active_macro_plan_id(connection), week)`
    """
    now = utc_now()
    with transaction(connection):
        row = connection.execute(
            _UPSERT_WEEKLY_PLAN,
            (
                macro_plan_id,
                week.week_start.isoformat(),
                week.week_end.isoformat(),
                canonical_json(week.model_dump(mode="json")),
                now,
                now,
            ),
        ).fetchone()
    return int(row[0])


def active_weekly_plan_id(
    connection: sqlite3.Connection, week_start: date
) -> int | None:
    """
    Return the id of the active macro plan's week that starts on week_start, or None.

    Used as `active_weekly_plan_id(connection, date(2027, 1, 4))`
    """
    row = connection.execute(
        """
        SELECT weekly_plan.id FROM weekly_plan
        JOIN macro_plan ON macro_plan.id = weekly_plan.macro_plan_id
        WHERE macro_plan.status = 'active' AND weekly_plan.week_start = ?
        """,
        (week_start.isoformat(),),
    ).fetchone()
    return None if row is None else int(row[0])


def stored_workouts(
    connection: sqlite3.Connection, first_day: date, last_day: date
) -> tuple[StoredWorkout, ...]:
    """
    Return the workouts this app pushed on these days, in date order.

    Any weekly plan or macro plan may hold them.

    Used as `stored_workouts(connection, week.week_start, week.week_end)`
    """
    rows = connection.execute(
        """
        SELECT id, schedule_date, garmin_workout_id, garmin_schedule_id
        FROM workout WHERE schedule_date BETWEEN ? AND ?
        ORDER BY schedule_date, id
        """,
        (first_day.isoformat(), last_day.isoformat()),
    ).fetchall()
    return tuple(
        StoredWorkout(row[0], date.fromisoformat(row[1]), row[2], row[3])
        for row in rows
    )


def delete_stored_workout(connection: sqlite3.Connection, row_id: int) -> None:
    """
    Delete one workout row after Garmin no longer holds it.

    Used as `delete_stored_workout(connection, stored.row_id)`
    """
    with transaction(connection):
        connection.execute("DELETE FROM workout WHERE id = ?", (row_id,))


def insert_pushed_workout(
    connection: sqlite3.Connection,
    weekly_plan_id: int,
    schedule_date: date,
    name: str,
    garmin_workout_id: str,
) -> int:
    """
    Store an uploaded workout before it is scheduled, with no schedule id yet.

    Used as `insert_pushed_workout(connection, 7, date(2027, 1, 4), "04/01 Leve", "12")`
    """
    with transaction(connection):
        row = connection.execute(
            """
            INSERT INTO workout (
                weekly_plan_id, schedule_date, name, garmin_workout_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                weekly_plan_id,
                schedule_date.isoformat(),
                name,
                garmin_workout_id,
                utc_now(),
            ),
        ).fetchone()
    return int(row[0])


def store_schedule_id(
    connection: sqlite3.Connection, row_id: int, garmin_schedule_id: str
) -> None:
    """
    Store the Garmin calendar entry id of a scheduled workout row.

    Used as `store_schedule_id(connection, row_id, "34")`
    """
    with transaction(connection):
        connection.execute(
            "UPDATE workout SET garmin_schedule_id = ? WHERE id = ?",
            (garmin_schedule_id, row_id),
        )
