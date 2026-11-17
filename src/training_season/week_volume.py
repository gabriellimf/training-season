"""Planned metres of a week, split into easy and hard workouts, from its steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.workout_schema import RepeatGroup, steps_as_run

if TYPE_CHECKING:
    from collections.abc import Iterator

    from training_season.push_week import PushWeek
    from training_season.workout_schema import TimedStep, Workout, WorkoutStep

_FIRST_HARD_ZONE = 3


@dataclass(frozen=True, slots=True)
class WeekVolume:
    """Planned metres of the easy workouts and of the hard ones."""

    easy_m: float
    hard_m: float


def _step_metres(step: TimedStep) -> float:
    if step.duration.seconds is None:
        return float(step.duration.meters or 0)
    return step.duration.seconds * step.pace_mps.middle_mps


def is_hard_step(step: TimedStep, *, in_repeat: bool) -> bool:
    """
    True from heart rate zone 3. A step with no zone is hard only inside a repeat.

    Used as `is_hard_step(child, in_repeat=True)`
    """
    if step.heart_rate_zone is None:
        return in_repeat
    return step.heart_rate_zone >= _FIRST_HARD_ZONE


def _steps_with_repeat_flag(
    steps: list[WorkoutStep],
) -> Iterator[tuple[TimedStep, bool]]:
    for step in steps:
        if isinstance(step, RepeatGroup):
            yield from ((child, True) for child in step.steps)
        else:
            yield step, False


def is_hard_workout(workout: Workout) -> bool:
    """
    True when any timed step is hard, as `is_hard_step` says, also inside a repeat.

    Used as `"hard" if is_hard_workout(entry.workout) else "easy"`
    """
    return any(
        is_hard_step(step, in_repeat=in_repeat)
        for step, in_repeat in _steps_with_repeat_flag(workout.steps)
    )


def week_volume(week: PushWeek) -> WeekVolume:
    """
    Sum the planned metres per workout, as hard or easy by `is_hard_workout`.

    A step in seconds counts at the middle of its pace range.
    Used as `week_volume(week).easy_m`
    """
    easy_m = hard_m = 0.0
    for entry in week.workouts:
        metres = sum(map(_step_metres, steps_as_run(entry.workout.steps)))
        if is_hard_workout(entry.workout):
            hard_m += metres
        else:
            easy_m += metres
    return WeekVolume(easy_m, hard_m)
