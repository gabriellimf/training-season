"""Portuguese text of the workout steps, one line per step, for the plan card."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.coach_context_lines import clock_text
from training_season.week_volume import is_hard_step
from training_season.weekly_cycle_text import metres_km_text
from training_season.workout_schema import RepeatGroup

if TYPE_CHECKING:
    from collections.abc import Sequence

    from training_season.workout_schema import (
        CadenceRange,
        PaceRange,
        StepDuration,
        TimedStep,
        WorkoutStep,
    )

_STEP_TYPE_TEXT = {
    "warmup": "Aquecimento",
    "recovery": "Recuperação",
    "cooldown": "Desaquecimento",
}
_HARD_INTERVAL_TEXT = "Tiro"
_EASY_INTERVAL_TEXT = "Corrida"
_PART_SEPARATOR = " · "
_METRES_PER_KM = 1000
_SECONDS_PER_MINUTE = 60


@dataclass(frozen=True, slots=True)
class StepText:
    """One step as text. A repeat has child steps; a timed step has `steps=None`."""

    text: str
    notes: str | None
    steps: tuple[StepText, ...] | None


def _distance_text(metres: int) -> str:
    if metres < _METRES_PER_KM:
        return f"{metres} m"
    return f"{metres_km_text(metres)} km"


def _time_text(seconds: int) -> str:
    minutes, rest = divmod(seconds, _SECONDS_PER_MINUTE)
    if minutes == 0:
        return f"{rest} s"
    if rest == 0:
        return f"{minutes} min"
    return f"{minutes} min {rest} s"


def _duration_text(duration: StepDuration) -> str:
    if duration.meters is not None:
        return _distance_text(duration.meters)
    if duration.seconds is not None:
        return _time_text(duration.seconds)
    raise ValueError(f"duration is {duration!r}, expected meters or seconds")


def _seconds_per_km_text(speed_mps: float) -> str:
    return clock_text(_METRES_PER_KM / speed_mps)


def _pace_text(pace: PaceRange) -> str:
    faster, slower = _seconds_per_km_text(pace.max), _seconds_per_km_text(pace.min)
    if faster == slower:
        return f"ritmo {faster} /km"
    return f"ritmo {faster}\N{EN DASH}{slower} /km"


def _cadence_text(cadence: CadenceRange) -> str:
    if cadence.min == cadence.max:
        return f"cadência {cadence.min} spm"
    return f"cadência {cadence.min}-{cadence.max} spm"


def _step_label(step: TimedStep, *, in_repeat: bool) -> str:
    if step.type != "interval":
        return _STEP_TYPE_TEXT[step.type]
    if is_hard_step(step, in_repeat=in_repeat):
        return _HARD_INTERVAL_TEXT
    return _EASY_INTERVAL_TEXT


def _timed_step_text(step: TimedStep, *, in_repeat: bool) -> StepText:
    parts = [
        _step_label(step, in_repeat=in_repeat),
        _duration_text(step.duration),
        _pace_text(step.pace_mps),
    ]
    if step.heart_rate_zone is not None:
        parts.append(f"zona {step.heart_rate_zone}")
    if step.cadence_spm is not None:
        parts.append(_cadence_text(step.cadence_spm))
    return StepText(_PART_SEPARATOR.join(parts), step.notes, None)


def _step_text(step: WorkoutStep) -> StepText:
    if isinstance(step, RepeatGroup):
        children = tuple(
            _timed_step_text(child, in_repeat=True) for child in step.steps
        )
        return StepText(f"Repita {step.count}x:", None, children)
    return _timed_step_text(step, in_repeat=False)


def workout_step_texts(steps: Sequence[WorkoutStep]) -> tuple[StepText, ...]:
    """
    Return each step as Portuguese text, in workout order. Notes stay apart.

    Used as `workout_step_texts(entry.workout.steps)[0].text`
    """
    return tuple(map(_step_text, steps))
