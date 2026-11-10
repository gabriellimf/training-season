"""The macro plan shape: contiguous phases from the plan start to the race."""

from datetime import date, timedelta
from typing import Annotated, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

from training_season.bounded_text import (
    MULTI_LINE,
    SINGLE_LINE,
    text_length_errors,
    trimmed,
)
from training_season.push_week import IsoDate
from training_season.workout_schema import Workout
from training_season.workout_validation import WorkoutValidationError

MAX_MACRO_TEXT_LENGTH = 100_000
MAX_PHASES = 60
MAX_WEEKLY_KM = 300
_SHAPE_ONLY = Workout.model_config


class MacroPlanError(WorkoutValidationError):
    """The macro plan is not valid. `errors` holds one line per problem."""


class WeeklyKmRange(BaseModel):
    """The weekly distance band of one phase, in kilometers."""

    model_config = _SHAPE_ONLY

    min: float = Field(ge=0, le=MAX_WEEKLY_KM)
    max: float = Field(ge=0, le=MAX_WEEKLY_KM)

    @model_validator(mode="after")
    def _require_ordered_range(self) -> Self:
        if self.min > self.max:
            raise ValueError(
                f"weekly km min {self.min} is above max {self.max}, expected min <= max"
            )
        return self


class MacroPhase(BaseModel):
    """One training phase and its dates, both days included."""

    model_config = _SHAPE_ONLY

    name: Annotated[str, trimmed(60), SINGLE_LINE]
    start_date: IsoDate
    end_date: IsoDate
    focus: Annotated[str, trimmed(1000), MULTI_LINE]
    weekly_km: WeeklyKmRange

    @model_validator(mode="after")
    def _require_ordered_dates(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError(
                f"phase ends {self.end_date.isoformat()} before it starts "
                f"{self.start_date.isoformat()}, expected start <= end"
            )
        return self


class MacroPlan(BaseModel):
    """The shape of `macro_plan.plan_json`."""

    model_config = _SHAPE_ONLY

    start_date: IsoDate
    race_date: IsoDate
    summary: Annotated[str, trimmed(2000), MULTI_LINE]
    phases: list[MacroPhase] = Field(min_length=1, max_length=MAX_PHASES)

    @model_validator(mode="after")
    def _require_contiguous_phases(self) -> Self:
        expected_start = self.start_date
        for number, phase in enumerate(self.phases, start=1):
            if phase.start_date != expected_start:
                raise ValueError(
                    f"phase {number} starts {phase.start_date.isoformat()}, "
                    f"expected {expected_start.isoformat()}"
                )
            expected_start = phase.end_date + timedelta(days=1)
        last_end = self.phases[-1].end_date
        if last_end != self.race_date:
            raise ValueError(
                f"last phase ends {last_end.isoformat()}, "
                f"expected the race date {self.race_date.isoformat()}"
            )
        return self


def _anchor_errors(plan: MacroPlan, start_date: date, race_date: date) -> list[str]:
    errors: list[str] = []
    if plan.start_date != start_date:
        errors.append(
            f"start_date: plan starts {plan.start_date.isoformat()}, "
            f"expected {start_date.isoformat()}"
        )
    if plan.race_date != race_date:
        errors.append(
            f"race_date: plan race date {plan.race_date.isoformat()}, "
            f"expected {race_date.isoformat()}"
        )
    return errors


def macro_plan_from_text(text: str, start_date: date, race_date: date) -> MacroPlan:
    """
    Parse the macro JSON and require the start and race dates the code chose.

    Used as `macro_plan_from_text(plan_json, date(2026, 11, 10), date(2027, 4, 18))`
    """
    if errors := text_length_errors("macro plan text", text, MAX_MACRO_TEXT_LENGTH):
        raise MacroPlanError(errors)
    try:
        plan = MacroPlan.model_validate_json(text)
    except ValidationError as error:
        raise MacroPlanError.from_pydantic(error) from error
    if anchor_errors := _anchor_errors(plan, start_date, race_date):
        raise MacroPlanError(tuple(anchor_errors))
    return plan
