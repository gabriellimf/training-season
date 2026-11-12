"""
Read-only SQL for the coach context.

Every query is bounded by a date range on an indexed column, reads one row from
the end of an index or the primary key, or uses the single-active macro plan index,
so its cost does not grow per day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from datetime import date

_ACTIVITIES_BETWEEN = """
SELECT activity_date, sport, activity_type, distance_m, duration_s, avg_hr, max_hr,
       avg_speed_mps, avg_cadence_spm, splits_json
FROM activity
WHERE activity_date BETWEEN ? AND ?
ORDER BY activity_date, start_time, garmin_activity_id
"""

_RUNNING_METRES_BETWEEN = """
SELECT COALESCE(SUM(distance_m), 0)
FROM activity
WHERE activity_date BETWEEN ? AND ? AND sport = 'running'
"""

_WELLNESS_BETWEEN = """
SELECT wellness_date, resting_hr, sleep_seconds, body_battery_high,
       body_battery_low, stress_avg, hrv_last_night_ms, vo2max
FROM wellness
WHERE wellness_date BETWEEN ? AND ?
ORDER BY wellness_date
"""

_WEEK_TOTALS_BETWEEN = """
SELECT (CAST(julianday(?) - julianday(activity_date) AS INTEGER) + 6) / 7
           AS weeks_back,
       sport, COUNT(*), COALESCE(SUM(distance_m), 0), COALESCE(SUM(duration_s), 0)
FROM activity
WHERE activity_date BETWEEN ? AND ?
GROUP BY weeks_back, sport
ORDER BY weeks_back DESC, sport
"""

_LATEST_CHECKIN_BEFORE = """
SELECT week_start, fatigue, has_pain, pain_location, pain_level, free_text
FROM checkin
WHERE week_start < ?
ORDER BY week_start DESC, id DESC
LIMIT 1
"""

_WEEKLY_PLANS_BETWEEN = """
SELECT week_start, raw_json
FROM weekly_plan
WHERE macro_plan_id = ? AND week_start BETWEEN ? AND ?
ORDER BY week_start
"""


@dataclass(frozen=True, slots=True)
class ActiveMacroRow:
    """The active macro plan: its dates and the plan JSON as stored."""

    macro_plan_id: int
    start_date: str
    race_date: str
    plan_json: str


@dataclass(frozen=True, slots=True)
class ContextPlanRow:
    """One weekly plan of the active macro, its JSON as stored."""

    week_start: str
    raw_json: str


@dataclass(frozen=True, slots=True)
class ContextActivityRow:
    """The activity columns the context shows; never the raw Garmin JSON."""

    activity_date: str
    sport: str
    activity_type: str
    distance_m: float | None
    duration_s: float | None
    avg_hr: float | None
    max_hr: float | None
    avg_speed_mps: float | None
    avg_cadence_spm: float | None
    splits_json: str | None


@dataclass(frozen=True, slots=True)
class ContextWellnessRow:
    """The wellness columns the context shows; never the raw Garmin JSON."""

    wellness_date: str
    resting_hr: float | None
    sleep_seconds: float | None
    body_battery_high: float | None
    body_battery_low: float | None
    stress_avg: float | None
    hrv_last_night_ms: float | None
    vo2max: float | None


@dataclass(frozen=True, slots=True)
class ContextCheckinRow:
    """One check-in as the athlete answered it."""

    week_start: str
    fatigue: int
    has_pain: int
    pain_location: str | None
    pain_level: int | None
    free_text: str | None


@dataclass(frozen=True, slots=True)
class WeekSportTotals:
    """Count, metres and seconds of one sport in the window `weeks_back` back."""

    weeks_back: int
    sport: str
    activity_count: int
    distance_m: float
    duration_s: float


def read_active_macro(connection: sqlite3.Connection) -> ActiveMacroRow | None:
    """
    Return the active macro plan, or None before onboarding made one.

    Used as `macro = read_active_macro(connection)`
    """
    row = connection.execute(
        "SELECT id, start_date, race_date, plan_json FROM macro_plan "
        "WHERE status = 'active'"
    ).fetchone()
    return None if row is None else ActiveMacroRow(*row)


def read_profile_json(connection: sqlite3.Connection) -> str | None:
    """
    Return the profile JSON object as stored, or None when it is not filled.

    Used as `profile_json = read_profile_json(connection)`
    """
    row = connection.execute("SELECT data_json FROM profile WHERE id = 1").fetchone()
    return None if row is None else str(row[0])


def read_latest_athlete_summary(connection: sqlite3.Connection) -> str | None:
    """
    Return the newest coach summary (highest id), or None before the first one.

    Used as `summary = read_latest_athlete_summary(connection)`
    """
    row = connection.execute(
        "SELECT summary FROM coach_summary ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row[0])


def read_weekly_plans(
    connection: sqlite3.Connection, macro_plan_id: int, first: date, last: date
) -> tuple[ContextPlanRow, ...]:
    """
    Return the macro plan's weekly plans that start first..last, in week order.

    Used as `read_weekly_plans(connection, 3, date(2026, 10, 5), date(2026, 11, 1))`
    """
    rows = connection.execute(
        _WEEKLY_PLANS_BETWEEN, (macro_plan_id, first.isoformat(), last.isoformat())
    ).fetchall()
    return tuple(ContextPlanRow(*row) for row in rows)


def read_activities_between(
    connection: sqlite3.Connection, first: date, last: date
) -> tuple[ContextActivityRow, ...]:
    """
    Return the activities of the training dates first..last, in date order.

    Used as `read_activities_between(connection, date(2026, 10, 5), date(2026, 11, 1))`
    """
    rows = connection.execute(
        _ACTIVITIES_BETWEEN, (first.isoformat(), last.isoformat())
    ).fetchall()
    return tuple(ContextActivityRow(*row) for row in rows)


def read_running_metres_between(
    connection: sqlite3.Connection, first: date, last: date
) -> float:
    """
    Return the run distance of the training dates first..last; no run gives 0.

    Used as `read_running_metres_between(connection, last_monday, last_sunday)`
    """
    row = connection.execute(
        _RUNNING_METRES_BETWEEN, (first.isoformat(), last.isoformat())
    ).fetchone()
    return float(row[0])


def read_wellness_between(
    connection: sqlite3.Connection, first: date, last: date
) -> tuple[ContextWellnessRow, ...]:
    """
    Return the wellness days first..last, in date order.

    Used as `read_wellness_between(connection, date(2026, 10, 5), date(2026, 11, 1))`
    """
    rows = connection.execute(
        _WELLNESS_BETWEEN, (first.isoformat(), last.isoformat())
    ).fetchall()
    return tuple(ContextWellnessRow(*row) for row in rows)


def read_week_totals(
    connection: sqlite3.Connection, target_week_start: date, first: date, last: date
) -> tuple[WeekSportTotals, ...]:
    """
    Sum activities per 7-day window before the target week, in one query.

    Window k holds the dates target-7k .. target-7k+6.
    Used as `read_week_totals(connection, date(2026, 11, 2), first, last)`
    """
    rows = connection.execute(
        _WEEK_TOTALS_BETWEEN,
        (target_week_start.isoformat(), first.isoformat(), last.isoformat()),
    ).fetchall()
    return tuple(WeekSportTotals(*row) for row in rows)


def read_latest_checkin_before(
    connection: sqlite3.Connection, target_week_start: date
) -> ContextCheckinRow | None:
    """
    Return the check-in with the latest week start before the target week.

    The highest id wins between two check-ins of one week. The query walks the
    `checkin_week_start` index from its end, with no sort.
    Used as `read_latest_checkin_before(connection, date(2026, 11, 2))`
    """
    row = connection.execute(
        _LATEST_CHECKIN_BEFORE, (target_week_start.isoformat(),)
    ).fetchone()
    return None if row is None else ContextCheckinRow(*row)
