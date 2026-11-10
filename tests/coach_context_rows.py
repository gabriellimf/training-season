"""Full rows and a synthetic training block for the coach context tests."""

import json
import sqlite3
from datetime import date, timedelta

from schema_rows import _insert_macro, _insert_weekly_plan

CREATED_AT = "2026-10-26T00:00:00Z"
BLOCK_START = date(2026, 5, 4)
RACE_DATE = "2026-12-20"
RUNS_PER_WEEK = 5
LAPS_PER_RUN = 10
WELLNESS_DAYS_PER_WEEK = 7


def week_of_block(week_number: int) -> date:
    return BLOCK_START + timedelta(days=7 * (week_number - 1))


def insert_block_macro(connection: sqlite3.Connection) -> int:
    return _insert_macro(
        connection, start_date=BLOCK_START.isoformat(), race_date=RACE_DATE
    )


def run_laps_json(lap_count: int) -> str:
    lap = {"averageHR": 150.0, "averageSpeed": 3.3, "distance": 1000.0}
    laps = [
        {**lap, "duration": 300.0 + index, "lapIndex": index}
        for index in range(1, lap_count + 1)
    ]
    return json.dumps({"lapDTOs": laps})


def insert_full_activity(
    connection: sqlite3.Connection,
    *,
    activity_id: str,
    day: date,
    sport: str = "running",
    splits_json: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO activity (garmin_activity_id, sport, activity_type, start_time, "
        "activity_date, distance_m, duration_s, avg_hr, max_hr, avg_speed_mps, "
        "avg_cadence_spm, splits_json, raw_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, 10000.0, 3000.0, 150.0, 172.0, 3.3333, 170.0, ?, "
        "'{\"secretRawKey\": 1}', ?)",
        (
            activity_id,
            sport,
            "lap_swimming" if sport == "swimming" else "road_run",
            f"{day.isoformat()}T10:00:00Z",
            day.isoformat(),
            splits_json,
            CREATED_AT,
        ),
    )


def insert_full_wellness(connection: sqlite3.Connection, day: date) -> None:
    connection.execute(
        "INSERT INTO wellness (wellness_date, resting_hr, sleep_seconds, "
        "body_battery_high, body_battery_low, stress_avg, hrv_last_night_ms, vo2max, "
        "raw_json, created_at, updated_at) "
        "VALUES (?, 48.0, 27000.0, 90.0, 20.0, 30.0, 62.0, 52.0, "
        "'{\"secretRawKey\": 1}', ?, ?)",
        (day.isoformat(), CREATED_AT, CREATED_AT),
    )


def insert_week_checkin(
    connection: sqlite3.Connection,
    week_start: date,
    *,
    created_at: str = CREATED_AT,
    fatigue: int = 5,
) -> None:
    connection.execute(
        "INSERT INTO checkin (week_start, fatigue, has_pain, created_at) "
        "VALUES (?, ?, 0, ?)",
        (week_start.isoformat(), fatigue, created_at),
    )


def insert_summary(connection: sqlite3.Connection, summary: str) -> None:
    connection.execute(
        "INSERT INTO coach_summary (summary, created_at) VALUES (?, ?)",
        (summary, CREATED_AT),
    )


def insert_profile(connection: sqlite3.Connection, profile_json: str) -> None:
    connection.execute(
        "INSERT INTO profile (id, data_json, created_at, updated_at) "
        "VALUES (1, ?, ?, ?)",
        (profile_json, CREATED_AT, CREATED_AT),
    )


def _week_plan_json(week_start: date) -> str:
    workouts = [
        {
            "date": (week_start + timedelta(days=day)).isoformat(),
            "workout": {"name": "Easy", "steps": [{"kind": "run", "km": 8}]},
        }
        for day in range(RUNS_PER_WEEK)
    ]
    return json.dumps({"week_start": week_start.isoformat(), "workouts": workouts})


def _seed_week(connection: sqlite3.Connection, macro_id: int, week_number: int) -> None:
    week_start = week_of_block(week_number)
    _insert_weekly_plan(
        connection,
        macro_id,
        week_start=week_start.isoformat(),
        week_end=(week_start + timedelta(days=6)).isoformat(),
        raw_json=_week_plan_json(week_start),
    )
    _seed_week_activities(connection, week_number, week_start)
    for day in range(WELLNESS_DAYS_PER_WEEK):
        insert_full_wellness(connection, week_start + timedelta(days=day))


def _seed_week_activities(
    connection: sqlite3.Connection, week_number: int, week_start: date
) -> None:
    for day in range(RUNS_PER_WEEK):
        insert_full_activity(
            connection,
            activity_id=f"run-{week_number}-{day}",
            day=week_start + timedelta(days=day),
            splits_json=run_laps_json(LAPS_PER_RUN),
        )
    insert_full_activity(
        connection,
        activity_id=f"swim-{week_number}",
        day=week_start + timedelta(days=5),
        sport="swimming",
    )


def seed_training_block(connection: sqlite3.Connection, week_count: int) -> int:
    macro_id = insert_block_macro(connection)
    for week_number in range(1, week_count + 1):
        _seed_week(connection, macro_id, week_number)
    return macro_id
