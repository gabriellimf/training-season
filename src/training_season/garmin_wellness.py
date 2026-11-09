"""Pull Garmin wellness days. Each date window commits on its own."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from training_season.database import transaction
from training_season.garmin_auth import GarminUnavailableError
from training_season.garmin_pull_base import (
    canonical_json,
    has_garmin_value,
    non_negative,
    none_if_not_found,
    resume_start_date,
)
from training_season.timeformat import parse_iso_date, utc_now

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence

    from garminconnect import Garmin

    from training_season.garmin_pull_base import GarminPullContext

WELLNESS_WINDOW_DAYS = 28
_LATEST_WELLNESS_DATE_SQL = "SELECT max(wellness_date) FROM wellness"
_EARLIEST_ACTIVITY_DATE_SQL = "SELECT min(activity_date) FROM activity"
_UPSERT_WELLNESS_SQL = """
INSERT INTO wellness (
    wellness_date,
    resting_hr,
    sleep_seconds,
    body_battery_high,
    body_battery_low,
    stress_avg,
    hrv_last_night_ms,
    vo2max,
    raw_json,
    created_at,
    updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (wellness_date) DO UPDATE SET
    resting_hr = excluded.resting_hr,
    sleep_seconds = excluded.sleep_seconds,
    body_battery_high = excluded.body_battery_high,
    body_battery_low = excluded.body_battery_low,
    stress_avg = excluded.stress_avg,
    hrv_last_night_ms = excluded.hrv_last_night_ms,
    vo2max = excluded.vo2max,
    raw_json = excluded.raw_json,
    updated_at = excluded.updated_at
"""


@dataclass(frozen=True, slots=True)
class _WellnessRow:
    wellness_date: str
    resting_hr: float | None
    sleep_seconds: float | None
    body_battery_high: float | None
    body_battery_low: float | None
    stress_avg: float | None
    hrv_last_night_ms: float | None
    vo2max: float | None
    raw_json: str


@dataclass(frozen=True, slots=True)
class WellnessPhase:
    """Date window and day count written by the wellness pull."""

    start: str
    end: str
    written: int


def _vo2max_from(max_metrics: object) -> float | None:
    # Public evidence: arpanghosh8453/garmin-grafana:garmin_fetch.py:get_vo2_max
    # reads this path. This shape is not verified against this account.
    if not isinstance(max_metrics, list) or not max_metrics:
        return None
    first = max_metrics[0]
    if not isinstance(first, dict):
        return None
    generic = first.get("generic")
    if not isinstance(generic, dict):
        return None
    return non_negative(generic.get("vo2MaxPreciseValue"))


def _hrv_last_night(hrv: object) -> object:
    summary = hrv.get("hrvSummary") if isinstance(hrv, dict) else None
    return summary.get("lastNightAvg") if isinstance(summary, dict) else None


def _wellness_raw_json(daily_summary: object, hrv: object, max_metrics: object) -> str:
    raw = {"daily_summary": daily_summary, "hrv": hrv, "max_metrics": max_metrics}
    try:
        return canonical_json(raw)
    except (TypeError, ValueError):
        raise GarminUnavailableError(
            "Garmin returned unexpected wellness data"
        ) from None


def _new_wellness_row(
    day_key: str,
    daily_summary: object,
    hrv: object,
    max_metrics: object,
    raw_json: str,
) -> _WellnessRow:
    values: dict[str, object] = daily_summary if isinstance(daily_summary, dict) else {}
    return _WellnessRow(
        wellness_date=day_key,
        resting_hr=non_negative(values.get("restingHeartRate")),
        sleep_seconds=non_negative(values.get("sleepingSeconds")),
        body_battery_high=non_negative(values.get("bodyBatteryHighestValue")),
        body_battery_low=non_negative(values.get("bodyBatteryLowestValue")),
        stress_avg=non_negative(values.get("averageStressLevel")),
        hrv_last_night_ms=non_negative(_hrv_last_night(hrv)),
        vo2max=_vo2max_from(max_metrics),
        raw_json=raw_json,
    )


def _wellness_day_row(
    day_key: str, daily_summary: object, hrv: object, max_metrics: object
) -> _WellnessRow | None:
    summary, hrv_value, metrics_value = (
        value if has_garmin_value(value) else None
        for value in (daily_summary, hrv, max_metrics)
    )
    if summary is None and hrv_value is None and metrics_value is None:
        return None
    raw_json = _wellness_raw_json(summary, hrv_value, metrics_value)
    return _new_wellness_row(day_key, summary, hrv_value, metrics_value, raw_json)


def _wellness_rows(
    days: Sequence[date],
    daily_summaries: Mapping[str, object],
    hrv_by_day: Mapping[str, object],
    max_metrics_by_day: Mapping[str, object],
) -> list[_WellnessRow]:
    rows: list[_WellnessRow] = []
    for day in days:
        key = day.isoformat()
        row = _wellness_day_row(
            key,
            daily_summaries.get(key),
            hrv_by_day.get(key),
            max_metrics_by_day.get(key),
        )
        if row is not None:
            rows.append(row)
    return rows


def _wellness_start(run: GarminPullContext) -> date:
    start = resume_start_date(run, _LATEST_WELLNESS_DATE_SQL)
    if start is not None:
        return start
    earliest = run.connection.execute(_EARLIEST_ACTIVITY_DATE_SQL).fetchone()
    if earliest is None or earliest[0] is None:
        return run.today
    return date.fromisoformat(earliest[0])


def _daily_summaries_by_day(
    garmin: Garmin, start: date, end: date
) -> dict[str, object]:
    response = garmin.get_daily_summaries(start.isoformat(), end.isoformat())
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise GarminUnavailableError("Garmin returned an unexpected daily summary list")
    summaries: dict[str, object] = {}
    for summary in response["data"]:
        if not isinstance(summary, dict):
            continue
        summary_date = parse_iso_date(summary.get("calendarDate"))
        if summary_date is not None and start <= summary_date <= end:
            summaries[summary_date.isoformat()] = summary
    return summaries


def _day_metrics(
    garmin: Garmin, days: Sequence[date]
) -> tuple[dict[str, object], dict[str, object]]:
    hrv_by_day: dict[str, object] = {}
    max_metrics_by_day: dict[str, object] = {}
    for day in days:
        key = day.isoformat()
        hrv_by_day[key] = none_if_not_found(garmin.get_hrv_data, key)
        max_metrics_by_day[key] = none_if_not_found(garmin.get_max_metrics, key)
    return hrv_by_day, max_metrics_by_day


def _upsert_wellness(
    connection: sqlite3.Connection, row: _WellnessRow, updated_at: str
) -> int:
    upserted = connection.execute(
        _UPSERT_WELLNESS_SQL,
        (
            row.wellness_date,
            row.resting_hr,
            row.sleep_seconds,
            row.body_battery_high,
            row.body_battery_low,
            row.stress_avg,
            row.hrv_last_night_ms,
            row.vo2max,
            row.raw_json,
            updated_at,
            updated_at,
        ),
    )
    return upserted.rowcount


def _store_wellness_rows(
    connection: sqlite3.Connection, rows: Sequence[_WellnessRow]
) -> int:
    if not rows:
        return 0
    updated_at = utc_now()
    with transaction(connection):
        return sum(_upsert_wellness(connection, row, updated_at) for row in rows)


def _pull_wellness_window(run: GarminPullContext, start: date, end: date) -> int:
    summaries = _daily_summaries_by_day(run.garmin, start, end)
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    hrv_by_day, max_metrics_by_day = _day_metrics(run.garmin, days)
    rows = _wellness_rows(days, summaries, hrv_by_day, max_metrics_by_day)
    return _store_wellness_rows(run.connection, rows)


def pull_wellness(run: GarminPullContext) -> WellnessPhase:
    """Pull wellness days in windows from the resume date to today."""
    start = _wellness_start(run)
    written = 0
    current = start
    while current <= run.today:
        end = min(current + timedelta(days=WELLNESS_WINDOW_DAYS - 1), run.today)
        written += _pull_wellness_window(run, current, end)
        current = end + timedelta(days=1)
    return WellnessPhase(start.isoformat(), run.today.isoformat(), written)
