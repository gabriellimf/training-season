"""Tests for the volume and split lines of the weekly cycle chat message."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import pytest

from coach_context_rows import insert_full_activity
from training_season.push_week import PushWeek, push_week_from_text
from training_season.weekly_cycle_text import week_comparison_lines

if TYPE_CHECKING:
    import sqlite3


def _dated_workout(day: str, step_type: str, metres: int) -> dict[str, object]:
    step = {
        "type": step_type,
        "duration": {"meters": metres},
        "pace_mps": {"min": 3.0, "max": 3.0},
    }
    workout = {"name": f"{day[8:]}/{day[5:7]} Leve", "steps": [step]}
    return {"date": day, "workout": workout}


def _week_of_metres(easy_m: int, hard_m: int = 0) -> PushWeek:
    workouts = [_dated_workout("2026-11-17", "warmup", easy_m)]
    if hard_m:
        workouts.append(_dated_workout("2026-11-19", "interval", hard_m))
    return push_week_from_text(
        json.dumps({"week_start": "2026-11-16", "workouts": workouts})
    )


def _seed_last_week_run(connection: sqlite3.Connection, metres: int) -> None:
    insert_full_activity(connection, activity_id="last-week", day=date(2026, 11, 12))
    connection.execute("UPDATE activity SET distance_m = ?", (metres,))


@pytest.mark.parametrize(
    ("planned_m", "run_m", "expected"),
    [
        (10_060, 5_040, "10,1 km (+5,1 km contra 5,0 km"),
        (5_000, 5_040, "5,0 km (±0,0 km contra 5,0 km"),
    ],
)
def test_volume_delta_is_the_difference_of_the_shown_values(
    migrated_connection: sqlite3.Connection, planned_m: int, run_m: int, expected: str
) -> None:
    _seed_last_week_run(migrated_connection, run_m)

    lines = week_comparison_lines(migrated_connection, _week_of_metres(planned_m))

    assert lines[0] == (f"Volume planejado: {expected} corridos na semana passada).")


def test_planned_volume_is_the_sum_of_the_shown_split(
    migrated_connection: sqlite3.Connection,
) -> None:
    week = _week_of_metres(easy_m=1_040, hard_m=1_040)

    lines = week_comparison_lines(migrated_connection, week)

    assert lines[:2] == (
        "Volume planejado: 2,0 km (+2,0 km contra 0,0 km corridos na semana passada).",
        "Leve: 1,0 km · Forte: 1,0 km.",
    )


def test_volume_line_compares_with_last_week_running_only(
    migrated_connection: sqlite3.Connection,
) -> None:
    for activity_id, day, sport in (
        ("last-monday", date(2026, 11, 9), "running"),
        ("last-sunday", date(2026, 11, 15), "running"),
        ("last-swim", date(2026, 11, 12), "swimming"),
        ("two-weeks-back", date(2026, 11, 8), "running"),
    ):
        insert_full_activity(
            migrated_connection, activity_id=activity_id, day=day, sport=sport
        )

    lines = week_comparison_lines(migrated_connection, _week_of_metres(2_000))

    assert lines[0] == (
        "Volume planejado: 2,0 km (-18,0 km contra 20,0 km corridos na semana passada)."
    )
