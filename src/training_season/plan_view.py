"""The plan cards of the chat page: the stored weeks of the active macro not over."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from training_season.coach_context_store import read_active_macro, read_weekly_plans
from training_season.push_store import active_weekly_plan_id, scheduled_workout_keys
from training_season.push_week import push_week_from_text
from training_season.week_volume import is_hard_workout
from training_season.weekly_cycle import next_week_range
from training_season.weekly_cycle_text import (
    planned_week_lines,
    week_comparison_lines,
    weekday_text,
)
from training_season.workout_text import StepText, workout_step_texts

if TYPE_CHECKING:
    import sqlite3
    from datetime import date

    from training_season.push_week import PushEntry, PushWeek

type Intensity = Literal["easy", "hard"]

_DAYS_BEFORE_TODAY = 6


@dataclass(frozen=True, slots=True)
class PlanWorkout:
    """One planned workout. `scheduled`: Garmin holds this week's row of it."""

    date: date
    weekday: str
    name: str
    intensity: Intensity
    description: str | None
    scheduled: bool
    steps: tuple[StepText, ...]


@dataclass(frozen=True, slots=True)
class PlanWeek:
    """One plan card: heading, km against last week, easy/hard split, workouts."""

    week_start: date
    week_end: date
    heading: str
    volume_line: str
    split_line: str
    workouts: tuple[PlanWorkout, ...]


def _scheduled_keys(
    connection: sqlite3.Connection, week: PushWeek
) -> frozenset[tuple[date, str]]:
    week_id = active_weekly_plan_id(connection, week.week_start)
    if week_id is None:
        return frozenset()
    return scheduled_workout_keys(connection, week_id, week.week_start, week.week_end)


def _plan_workout(
    entry: PushEntry, scheduled_keys: frozenset[tuple[date, str]]
) -> PlanWorkout:
    workout = entry.workout
    return PlanWorkout(
        date=entry.date,
        weekday=weekday_text(entry.date),
        name=workout.name,
        intensity="hard" if is_hard_workout(workout) else "easy",
        description=workout.description,
        scheduled=(entry.date, workout.name) in scheduled_keys,
        steps=workout_step_texts(workout.steps),
    )


def _plan_week(connection: sqlite3.Connection, week: PushWeek) -> PlanWeek:
    scheduled_keys = _scheduled_keys(connection, week)
    volume_line, split_line = week_comparison_lines(connection, week)
    return PlanWeek(
        week_start=week.week_start,
        week_end=week.week_end,
        heading=planned_week_lines(week)[0],
        volume_line=volume_line,
        split_line=split_line,
        workouts=tuple(
            _plan_workout(entry, scheduled_keys) for entry in week.ordered_entries()
        ),
    )


def _weeks_not_over(
    connection: sqlite3.Connection, today: date
) -> tuple[PushWeek, ...]:
    macro = read_active_macro(connection)
    if macro is None:
        return ()
    first = today - timedelta(days=_DAYS_BEFORE_TODAY)
    rows = read_weekly_plans(
        connection, macro.macro_plan_id, first, next_week_range(today)[0]
    )
    weeks = (push_week_from_text(row.raw_json) for row in rows)
    return tuple(week for week in weeks if week.week_end >= today)


def plan_weeks(connection: sqlite3.Connection, today: date) -> tuple[PlanWeek, ...]:
    """
    Return the cards of the current week and, once stored, the next week.

    A week shows until its last day. No active macro plan gives no card.
    Used as `plan_weeks(connection, training_date(clock(), zone))`
    """
    return tuple(
        _plan_week(connection, week) for week in _weeks_not_over(connection, today)
    )
