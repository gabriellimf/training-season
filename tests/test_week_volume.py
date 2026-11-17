"""Tests for the planned metres of a week and its easy/hard split."""

from __future__ import annotations

import json

import pytest

from training_season.push_week import PushWeek, push_week_from_text
from training_season.week_volume import is_hard_step, is_hard_workout, week_volume
from training_season.workout_schema import IntervalStep, Workout


def _step(
    kind: str,
    duration: dict[str, int],
    pace: tuple[float, float] = (3.0, 3.0),
    zone: int | None = None,
) -> dict[str, object]:
    step: dict[str, object] = {
        "type": kind,
        "duration": duration,
        "pace_mps": {"min": pace[0], "max": pace[1]},
    }
    if zone is not None:
        step["heart_rate_zone"] = zone
    return step


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


def _workout(*steps: dict[str, object]) -> Workout:
    return Workout.model_validate({"name": "17/11 Treino", "steps": list(steps)})


_RUN_5K = {"meters": 5000}


@pytest.mark.parametrize(
    ("zone", "in_repeat", "hard"),
    [
        (1, False, False),
        (2, True, False),
        (3, False, True),
        (5, True, True),
        (None, False, False),
        (None, True, True),
    ],
)
def test_step_is_hard_from_zone_three_or_with_no_zone_in_a_repeat(
    zone: int | None, in_repeat: bool, hard: bool
) -> None:
    step = IntervalStep.model_validate(_step("interval", _RUN_5K, zone=zone))

    assert is_hard_step(step, in_repeat=in_repeat) is hard


@pytest.mark.parametrize(
    ("steps", "hard"),
    [
        ([_step("interval", _RUN_5K, zone=2)], False),
        ([_step("interval", _RUN_5K, zone=3)], True),
        ([_step("interval", _RUN_5K)], False),
        ([_repeat(4, _step("interval", _RUN_5K))], True),
        ([_repeat(4, _step("interval", _RUN_5K, zone=2))], False),
        ([_step("warmup", _RUN_5K, zone=3), _step("interval", _RUN_5K)], True),
    ],
    ids=[
        "zone-2-run",
        "zone-3-run",
        "no-zone-run",
        "no-zone-repeat",
        "zone-2-repeat",
        "zone-3-warmup",
    ],
)
def test_workout_is_hard_when_any_step_is_hard_in_its_context(
    steps: list[dict[str, object]], hard: bool
) -> None:
    assert is_hard_workout(_workout(*steps)) is hard


def test_mixed_week_splits_easy_runs_from_hard_ones() -> None:
    easy_run = {"name": "17/11 Leve", "steps": [_step("interval", _RUN_5K, zone=2)]}
    plain_run = {"name": "18/11 Solto", "steps": [_step("interval", {"meters": 3000})]}
    tempo = {"name": "20/11 Ritmo", "steps": [_step("interval", _RUN_5K, zone=4)]}
    week = _week(
        ("2026-11-17", easy_run),
        ("2026-11-18", plain_run),
        ("2026-11-19", _HARD),
        ("2026-11-20", tempo),
    )

    volume = week_volume(week)

    assert volume.easy_m == pytest.approx(5000 + 3000)
    assert volume.hard_m == pytest.approx(_HARD_M + 5000)
