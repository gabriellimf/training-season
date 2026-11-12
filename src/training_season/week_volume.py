"""Planned metres of a week, split into easy and hard workouts, from its steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.workout_schema import IntervalStep, steps_as_run

if TYPE_CHECKING:
    from training_season.push_week import PushWeek
    from training_season.workout_schema import TimedStep, Workout


@dataclass(frozen=True, slots=True)
class WeekVolume:
    """Planned metres of the easy workouts and of the hard ones."""

    easy_m: float
    hard_m: float


def _step_metres(step: TimedStep) -> float:
    if step.duration.seconds is None:
        return float(step.duration.meters or 0)
    return step.duration.seconds * step.pace_mps.middle_mps


def _is_hard(workout: Workout) -> bool:
    return any(isinstance(step, IntervalStep) for step in steps_as_run(workout.steps))


def week_volume(week: PushWeek) -> WeekVolume:
    """
    Sum the planned metres per workout. A workout with an interval step is hard.

    A step in seconds counts at the middle of its pace range.
    Used as `week_volume(week).easy_m`
    """
    easy_m = hard_m = 0.0
    for entry in week.workouts:
        metres = sum(map(_step_metres, steps_as_run(entry.workout.steps)))
        if _is_hard(entry.workout):
            hard_m += metres
        else:
            easy_m += metres
    return WeekVolume(easy_m, hard_m)
