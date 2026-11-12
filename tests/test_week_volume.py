"""Tests for the planned metres of a week and its easy/hard split."""

from __future__ import annotations

import json

import pytest

from training_season.push_week import PushWeek, push_week_from_text
from training_season.week_volume import week_volume


def _step(
    kind: str, duration: dict[str, int], pace: tuple[float, float] = (3.0, 3.0)
) -> dict[str, object]:
    return {
        "type": kind,
        "duration": duration,
        "pace_mps": {"min": pace[0], "max": pace[1]},
    }


def _repeat(count: int, *steps: dict[str, object]) -> dict[str, object]:
    return {"type": "repeat", "count": count, "steps": list(steps)}


_EASY = {
    "name": "17/11 Leve",
    "steps": [
        _step("warmup", {"meters": 2000}),
        _step("cooldown", {"seconds": 600}, (2.5, 3.5)),
    ],
}
_EASY_M = 2000 + 600 * 3.0
_HARD = {
    "name": "19/11 Tiros",
    "steps": [
        _step("warmup", {"meters": 1500}),
        _repeat(
            4,
            _step("interval", {"meters": 400}, (4.0, 4.4)),
            _step("recovery", {"seconds": 90}, (2.0, 2.4)),
        ),
    ],
}
_HARD_M = 1500 + 4 * (400 + 90 * 2.2)


def _week(*entries: tuple[str, object]) -> PushWeek:
    workouts = [{"date": day, "workout": workout} for day, workout in entries]
    return push_week_from_text(
        json.dumps({"week_start": "2026-11-16", "workouts": workouts})
    )


def test_planned_metres_count_meters_seconds_and_repeats() -> None:
    volume = week_volume(_week(("2026-11-17", _EASY), ("2026-11-19", _HARD)))

    assert volume.easy_m == pytest.approx(_EASY_M)
    assert volume.hard_m == pytest.approx(_HARD_M)


def test_interval_inside_a_repeat_makes_the_workout_hard() -> None:
    volume = week_volume(_week(("2026-11-19", _HARD)))

    assert volume.easy_m == 0
    assert volume.hard_m == pytest.approx(_HARD_M)


def test_empty_week_has_no_volume() -> None:
    volume = week_volume(_week())

    assert (volume.easy_m, volume.hard_m) == (0, 0)
