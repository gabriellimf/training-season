"""Pydantic envelope for one running workout returned by the coach model."""

import unicodedata
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_WORKOUT_STEPS = 50
MAX_REPEAT_COUNT = 99
MAX_WORKOUT_NAME_LENGTH = 15
MAX_NOTES_LENGTH = 500
MAX_DESCRIPTION_LENGTH = 2000

_SHAPE_ONLY = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _reject_control_characters(text: str) -> str:
    for character in text:
        if unicodedata.category(character).startswith("C"):
            raise ValueError(
                f"control or format character U+{ord(character):04X} is not allowed"
            )
    return text


def _reject_control_characters_but_newline(text: str) -> str:
    _reject_control_characters(text.replace("\n", ""))
    return text


class StepDuration(BaseModel):
    """Length of a step as exactly one of meters or seconds."""

    model_config = _SHAPE_ONLY

    meters: int | None = Field(default=None, ge=1, le=1_000_000)
    seconds: int | None = Field(default=None, ge=1, le=86_400)

    @model_validator(mode="after")
    def _require_exactly_one_key(self) -> Self:
        if (self.meters is None) == (self.seconds is None):
            raise ValueError(
                f"duration needs exactly one of meters or seconds, "
                f"got meters={self.meters} seconds={self.seconds}"
            )
        return self


class PaceRange(BaseModel):
    """Pace target in metres per second. Faster pace is the larger number."""

    model_config = _SHAPE_ONLY

    min: float = Field(gt=0, le=100)
    max: float = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _require_ordered_range(self) -> Self:
        if self.min > self.max:
            raise ValueError(
                f"pace min {self.min} is above max {self.max}, expected min <= max"
            )
        return self


class CadenceRange(BaseModel):
    """Cadence target in steps per minute."""

    model_config = _SHAPE_ONLY

    min: int = Field(ge=1, le=400)
    max: int = Field(ge=1, le=400)

    @model_validator(mode="after")
    def _require_ordered_range(self) -> Self:
        if self.min > self.max:
            raise ValueError(
                f"cadence min {self.min} is above max {self.max}, expected min <= max"
            )
        return self


class _TimedStep(BaseModel):
    model_config = _SHAPE_ONLY

    duration: StepDuration
    pace_mps: PaceRange
    heart_rate_zone: int | None = Field(default=None, ge=1, le=5)
    cadence_spm: CadenceRange | None = None
    notes: (
        Annotated[
            str,
            Field(max_length=MAX_NOTES_LENGTH),
            AfterValidator(_reject_control_characters_but_newline),
        ]
        | None
    ) = None


class WarmupStep(_TimedStep):
    type: Literal["warmup"]


class IntervalStep(_TimedStep):
    type: Literal["interval"]


class RecoveryStep(_TimedStep):
    type: Literal["recovery"]


class CooldownStep(_TimedStep):
    type: Literal["cooldown"]


TimedStep = Annotated[
    WarmupStep | IntervalStep | RecoveryStep | CooldownStep,
    Field(discriminator="type"),
]


class RepeatGroup(BaseModel):
    """Steps run `count` times. Garmin does not support a group inside a group."""

    model_config = _SHAPE_ONLY

    type: Literal["repeat"]
    count: int = Field(ge=1, le=MAX_REPEAT_COUNT)
    steps: list[TimedStep] = Field(min_length=1)

    @field_validator("steps", mode="before")
    @classmethod
    def _reject_nested_repeat(cls, steps: object) -> object:
        if not isinstance(steps, list):
            return steps
        for index, step in enumerate(steps):
            if isinstance(step, dict) and step.get("type") == "repeat":
                raise ValueError(
                    "nested repeat groups are not supported, "
                    f"got a repeat with count={step.get('count')!r} at child {index}"
                )
        return steps


WorkoutStep = Annotated[
    WarmupStep | IntervalStep | RecoveryStep | CooldownStep | RepeatGroup,
    Field(discriminator="type"),
]


class Workout(BaseModel):
    """One workout. The step count includes repeat groups and their children."""

    model_config = _SHAPE_ONLY

    name: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=MAX_WORKOUT_NAME_LENGTH
        ),
        AfterValidator(_reject_control_characters),
    ]
    description: (
        Annotated[
            str,
            Field(max_length=MAX_DESCRIPTION_LENGTH),
            AfterValidator(_reject_control_characters_but_newline),
        ]
        | None
    ) = None
    steps: list[WorkoutStep] = Field(min_length=1)

    @property
    def step_count(self) -> int:
        """Step count the device limit applies to: `workout.step_count`."""
        return count_steps(self.steps)

    @model_validator(mode="after")
    def _limit_step_count(self) -> Self:
        if self.step_count > MAX_WORKOUT_STEPS:
            raise ValueError(
                f"workout has {self.step_count} steps, "
                f"the maximum is {MAX_WORKOUT_STEPS}"
            )
        return self


def count_steps(steps: list[WorkoutStep]) -> int:
    """
    Count every step once, with each repeat group and its children included.

    Used as `count_steps(workout.steps)`
    """
    return sum(
        1 + len(step.steps) if isinstance(step, RepeatGroup) else 1 for step in steps
    )
