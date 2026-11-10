"""Portuguese text lines for one activity, its laps, one wellness day and a check-in."""

from __future__ import annotations

import json
import unicodedata
from typing import TYPE_CHECKING

from training_season.garmin_pull_base import non_negative

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from training_season.coach_context_store import (
        ContextActivityRow,
        ContextCheckinRow,
        ContextWellnessRow,
        WeekSportTotals,
    )

NO_WELLNESS = "sem dados"
MAX_CHECKIN_TEXT_CHARS = 2000
MAX_PAIN_LOCATION_CHARS = 200
CHECKIN_TEXT_CUT_SUFFIX = " […] (texto cortado)"
MAX_CONTEXT_LAPS = 20
_ALLOWED_CONTROLS = frozenset("\n\t")
SWIM_LABEL = "natação, só leitura, não planejar"
_SPORT_LABELS = {"running": "corrida", "swimming": SWIM_LABEL}

# Keys of one lap in Garmin's `get_activity_laps` response, stored as splits_json.
LAP_LIST_KEY = "lapDTOs"
LAP_DISTANCE_KEY = "distance"
LAP_DURATION_KEY = "duration"
LAP_SPEED_KEY = "averageSpeed"
LAP_HR_KEY = "averageHR"


def _joined(parts: Iterable[str | None]) -> str:
    return ", ".join(part for part in parts if part is not None)


def _optional(value: float | None, render: Callable[[float], str | None]) -> str | None:
    return None if value is None else render(value)


def _kilometres(distance_m: float) -> str:
    return f"{distance_m / 1000:.2f} km"


def _clock(seconds: float) -> str:
    minutes, rest_seconds = divmod(round(seconds), 60)
    hours, rest_minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{rest_minutes}:{rest_seconds:02d}"
    return f"{hours}:{rest_minutes:02d}:{rest_seconds:02d}"


def _pace(speed_mps: float) -> str | None:
    if speed_mps <= 0:
        return None
    return f"{_clock(1000 / speed_mps)} min/km"


def _sleep_hours(sleep_seconds: float) -> str:
    return f"sono {sleep_seconds / 3600:.1f} h"


def activity_line(activity: ContextActivityRow) -> str:
    """
    One activity with the measures it has; swims are marked read-only.

    Used as `activity_line(row)` -> `- 2026-10-27 corrida (road_run): 10.00 km, ...`
    """
    sport = _SPORT_LABELS.get(activity.sport, activity.sport)
    head = f"- {activity.activity_date} {sport} ({activity.activity_type})"
    measures = _joined(
        (
            _optional(activity.distance_m, _kilometres),
            _optional(activity.duration_s, _clock),
            _optional(activity.avg_speed_mps, _pace),
            _optional(activity.avg_hr, "FC média {:.0f}".format),
            _optional(activity.max_hr, "FC máx {:.0f}".format),
            _optional(activity.avg_cadence_spm, "cadência {:.0f} ppm".format),
        )
    )
    return f"{head}: {measures}" if measures else head


def _lap_objects(splits_json: str | None) -> list[dict[str, object]]:
    if splits_json is None:
        return []
    parsed: object = json.loads(splits_json)
    laps = parsed.get(LAP_LIST_KEY) if isinstance(parsed, dict) else None
    if not isinstance(laps, list):
        return []
    return [lap for lap in laps if isinstance(lap, dict)]


def _lap_text(lap: dict[str, object]) -> str:
    return _joined(
        (
            _optional(non_negative(lap.get(LAP_DISTANCE_KEY)), _kilometres),
            _optional(non_negative(lap.get(LAP_DURATION_KEY)), _clock),
            _optional(non_negative(lap.get(LAP_SPEED_KEY)), _pace),
            _optional(non_negative(lap.get(LAP_HR_KEY)), "FC {:.0f}".format),
        )
    )


def laps_line(splits_json: str | None) -> str | None:
    """
    The laps of one activity on one line, or None when no lap has a known value.

    A missing or non-numeric lap key is skipped.
    Used as `laps_line('{"lapDTOs": [{"distance": 400.0}]}')` -> `  voltas: 0.40 km`
    """
    laps = [text for text in map(_lap_text, _lap_objects(splits_json)) if text]
    if not laps:
        return None
    shown = "; ".join(laps[:MAX_CONTEXT_LAPS])
    return f"  voltas: {shown}{_hidden_laps_text(len(laps) - MAX_CONTEXT_LAPS)}"


def _hidden_laps_text(hidden_count: int) -> str:
    if hidden_count <= 0:
        return ""
    noun = "volta" if hidden_count == 1 else "voltas"
    return f"; +{hidden_count} {noun}"


def wellness_line(day: ContextWellnessRow) -> str:
    """
    One wellness day with the values it has.

    Used as `wellness_line(row)` -> `- 2026-10-27: FC repouso 48, sono 7.5 h, ...`
    """
    measures = _joined(
        (
            _optional(day.resting_hr, "FC repouso {:.0f}".format),
            _optional(day.sleep_seconds, _sleep_hours),
            _optional(day.body_battery_high, "Body Battery máx {:.0f}".format),
            _optional(day.body_battery_low, "Body Battery mín {:.0f}".format),
            _optional(day.stress_avg, "estresse {:.0f}".format),
            _optional(day.hrv_last_night_ms, "VFC {:.0f} ms".format),
            _optional(day.vo2max, "VO2max {:g}".format),
        )
    )
    return f"- {day.wellness_date}: {measures or NO_WELLNESS}"


def _sport_sums(totals: WeekSportTotals | None) -> tuple[int, float, float]:
    if totals is None:
        return (0, 0.0, 0.0)
    return (totals.activity_count, totals.distance_m, totals.duration_s)


def week_totals_text(sport_totals: Sequence[WeekSportTotals]) -> str:
    """
    Run count, km and time, then swim count and km, for one older week.

    Used as `week_totals_text(rows)` -> `corrida 5x, 50.00 km, 4:10:00; natação 0x, ...`
    """
    by_sport = {row.sport: row for row in sport_totals}
    run_count, run_m, run_s = _sport_sums(by_sport.get("running"))
    swim_count, swim_m, _swim_s = _sport_sums(by_sport.get("swimming"))
    return (
        f"corrida {run_count}x, {_kilometres(run_m)}, {_clock(run_s)}; "
        f"natação {swim_count}x, {_kilometres(swim_m)}"
    )


def _is_unsafe_control(character: str) -> bool:
    return (
        unicodedata.category(character) == "Cc" and character not in _ALLOWED_CONTROLS
    )


def first_unsafe_control(text: str) -> str | None:
    r"""
    The first C0 or C1 control character other than newline and tab, or None.

    Used as `first_unsafe_control("a\x1b[2J")` -> `"\x1b"`
    """
    return next(
        (character for character in text if _is_unsafe_control(character)), None
    )


def _athlete_line(athlete_text: str, max_chars: int) -> str:
    one_line = " ".join(athlete_text.splitlines())
    shown = "".join(char for char in one_line if not _is_unsafe_control(char))
    if len(shown) <= max_chars:
        return shown
    return shown[:max_chars] + CHECKIN_TEXT_CUT_SUFFIX


def _pain_text(checkin: ContextCheckinRow) -> str:
    if not checkin.has_pain:
        return "dor: não"
    location = _athlete_line(checkin.pain_location or "", MAX_PAIN_LOCATION_CHARS)
    return f"dor: sim, local {location}, nível {checkin.pain_level}/10"


def checkin_text(checkin: ContextCheckinRow) -> str:
    """
    The athlete's answers to the three fixed questions, then the free text.

    Athlete text is one line each, without control characters, and capped.
    Used as `checkin_text(row)` -> `Último check-in: semana 2026-10-26, ...`
    """
    answers = f"semana {checkin.week_start}, fadiga {checkin.fatigue}/10"
    lines = [f"Último check-in: {answers}, {_pain_text(checkin)}."]
    if checkin.free_text is not None:
        free_text = _athlete_line(checkin.free_text, MAX_CHECKIN_TEXT_CHARS)
        lines.append(f"Texto livre: {free_text}")
    return "\n".join(lines)
