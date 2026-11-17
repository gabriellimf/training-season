"""Tests for the plan cards: which stored weeks show and what each card holds."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

from schema_rows import _insert_macro
from training_season.plan_view import PlanWeek, PlanWorkout, plan_weeks
from training_season.push_store import (
    insert_pushed_workout,
    store_schedule_id,
    upsert_weekly_plan,
)
from training_season.push_week import PushWeek, push_week_from_text
from training_season.workout_text import StepText

if TYPE_CHECKING:
    import sqlite3

_PACE = {"min": 2.941, "max": 3.125}
_PACE_TEXT = "ritmo 5:20\N{EN DASH}5:40 /km"


def _easy(day: str, description: str) -> dict[str, object]:
    step = {"type": "warmup", "duration": {"meters": 2000}, "pace_mps": _PACE}
    name = f"{day[8:]}/{day[5:7]} Leve"
    workout = {"name": name, "description": description, "steps": [step]}
    return {"date": day, "workout": workout}


def _intervals(day: str) -> dict[str, object]:
    interval = {"type": "interval", "duration": {"meters": 400}, "pace_mps": _PACE}
    repeat = {"type": "repeat", "count": 2, "steps": [interval]}
    name = f"{day[8:]}/{day[5:7]} Tiros"
    return {"date": day, "workout": {"name": name, "steps": [repeat]}}


def _store_week(
    connection: sqlite3.Connection,
    macro_id: int,
    week_start: str,
    *workouts: dict[str, object],
    week_end: str | None = None,
) -> PushWeek:
    end = {} if week_end is None else {"week_end": week_end}
    text = json.dumps({"week_start": week_start, **end, "workouts": list(workouts)})
    week = push_week_from_text(text)
    upsert_weekly_plan(connection, macro_id, week)
    return week


def _macro(connection: sqlite3.Connection) -> int:
    return _insert_macro(connection, start_date="2026-11-09", race_date="2027-06-01")


def test_no_active_macro_shows_no_week(migrated_connection: sqlite3.Connection) -> None:
    assert plan_weeks(migrated_connection, date(2026, 11, 18)) == ()


def test_shows_the_week_not_over_and_the_next_one(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_id = _macro(migrated_connection)
    for week_start in ("2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"):
        _store_week(migrated_connection, macro_id, week_start)

    weeks = plan_weeks(migrated_connection, date(2026, 11, 18))

    assert [week.week_start for week in weeks] == [
        date(2026, 11, 16),
        date(2026, 11, 23),
    ]


def test_short_week_one_shows_until_its_last_day(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_id = _macro(migrated_connection)
    _store_week(migrated_connection, macro_id, "2026-11-18", week_end="2026-11-22")

    on_sunday = plan_weeks(migrated_connection, date(2026, 11, 22))
    on_monday = plan_weeks(migrated_connection, date(2026, 11, 23))

    assert [week.week_end for week in on_sunday] == [date(2026, 11, 22)]
    assert on_monday == ()


def test_card_holds_volume_split_and_readable_workouts(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_id = _macro(migrated_connection)
    easy = _easy("2026-11-17", "Rodagem leve.")
    _store_week(
        migrated_connection, macro_id, "2026-11-16", easy, _intervals("2026-11-19")
    )
    plan_id = migrated_connection.execute("SELECT id FROM weekly_plan").fetchone()[0]
    scheduled_row = insert_pushed_workout(
        migrated_connection, plan_id, date(2026, 11, 17), "17/11 Leve", "11"
    )
    store_schedule_id(migrated_connection, scheduled_row, "12")
    insert_pushed_workout(
        migrated_connection, plan_id, date(2026, 11, 19), "19/11 Tiros", "13"
    )

    weeks = plan_weeks(migrated_connection, date(2026, 11, 18))

    assert weeks == (
        PlanWeek(
            week_start=date(2026, 11, 16),
            week_end=date(2026, 11, 22),
            heading="Treinos da semana de 16/11 a 22/11:",
            volume_line=(
                "Volume planejado: 2,8 km (+2,8 km contra 0,0 km corridos "
                "na semana passada)."
            ),
            split_line="Leve: 2,0 km · Forte: 0,8 km.",
            workouts=(
                PlanWorkout(
                    date=date(2026, 11, 17),
                    weekday="ter",
                    name="17/11 Leve",
                    intensity="easy",
                    description="Rodagem leve.",
                    scheduled=True,
                    steps=(
                        StepText(f"Aquecimento · 2,0 km · {_PACE_TEXT}", None, None),
                    ),
                ),
                PlanWorkout(
                    date=date(2026, 11, 19),
                    weekday="qui",
                    name="19/11 Tiros",
                    intensity="hard",
                    description=None,
                    scheduled=False,
                    steps=(
                        StepText(
                            "Repita 2x:",
                            None,
                            (StepText(f"Tiro · 400 m · {_PACE_TEXT}", None, None),),
                        ),
                    ),
                ),
            ),
        ),
    )


def _scheduled_row(
    connection: sqlite3.Connection, plan_id: int, day: date, name: str
) -> None:
    row_id = insert_pushed_workout(connection, plan_id, day, name, f"w{day.day}")
    store_schedule_id(connection, row_id, f"s{day.day}")


def test_an_old_row_on_a_planned_date_is_not_scheduled(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_id = _macro(migrated_connection)
    _store_week(migrated_connection, macro_id, "2026-11-09")
    _store_week(
        migrated_connection,
        macro_id,
        "2026-11-16",
        _easy("2026-11-17", "Rodagem leve."),
        _intervals("2026-11-19"),
    )
    old_plan_id, plan_id = (
        row[0]
        for row in migrated_connection.execute(
            "SELECT id FROM weekly_plan ORDER BY week_start"
        )
    )
    _scheduled_row(migrated_connection, plan_id, date(2026, 11, 17), "17/11 Antigo")
    _scheduled_row(migrated_connection, old_plan_id, date(2026, 11, 19), "19/11 Tiros")

    weeks = plan_weeks(migrated_connection, date(2026, 11, 18))

    assert [workout.scheduled for workout in weeks[0].workouts] == [False, False]


def test_a_row_older_than_the_last_upsert_is_not_scheduled(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_id = _macro(migrated_connection)
    _store_week(
        migrated_connection, macro_id, "2026-11-16", _easy("2026-11-17", "Leve.")
    )
    plan_id = migrated_connection.execute("SELECT id FROM weekly_plan").fetchone()[0]
    _scheduled_row(migrated_connection, plan_id, date(2026, 11, 17), "17/11 Leve")
    migrated_connection.execute(
        "UPDATE workout SET created_at = '2026-11-01T12:00:00Z'"
    )

    weeks = plan_weeks(migrated_connection, date(2026, 11, 18))

    assert [workout.scheduled for workout in weeks[0].workouts] == [False]
