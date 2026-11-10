"""The athlete profile and the onboarding result the coach sends at the end."""

from datetime import date
from typing import Annotated, Literal, Self

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

MAX_RESULT_TEXT_LENGTH = 20_000
MAX_INJURIES = 20
_SHAPE_ONLY = Workout.model_config

RevisionReason = Annotated[str, trimmed(500), MULTI_LINE]

Weekday = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]


class OnboardingResultError(WorkoutValidationError):
    """The onboarding result is not valid. `errors` holds one line per problem."""


class RaceGoal(BaseModel):
    """The target race and what success means for the athlete."""

    model_config = _SHAPE_ONLY

    race_name: Annotated[str, trimmed(120), SINGLE_LINE]
    race_date: IsoDate
    distance_km: float = Field(gt=0, le=500)
    goal: Annotated[str, trimmed(500), MULTI_LINE]


class Injury(BaseModel):
    """One injury the coach must respect."""

    model_config = _SHAPE_ONLY

    description: Annotated[str, trimmed(500), MULTI_LINE]
    status: Literal["active", "recovered"]


class Availability(BaseModel):
    """When the athlete can run."""

    model_config = _SHAPE_ONLY

    days_per_week: int = Field(ge=1, le=7)
    preferred_days: list[Weekday] = Field(max_length=7)
    notes: Annotated[str, trimmed(1000), MULTI_LINE] | None = None

    @model_validator(mode="after")
    def _require_distinct_days_within_count(self) -> Self:
        for day in self.preferred_days:
            if self.preferred_days.count(day) > 1:
                raise ValueError(f"{day} is listed twice, expected distinct days")
        if len(self.preferred_days) > self.days_per_week:
            raise ValueError(
                f"{len(self.preferred_days)} preferred days for "
                f"{self.days_per_week} days per week, expected at most "
                f"{self.days_per_week}"
            )
        return self


class AthleteProfile(BaseModel):
    """The shape of `profile.data_json`."""

    model_config = _SHAPE_ONLY

    athlete_name: Annotated[str, trimmed(40), SINGLE_LINE] | None
    goal: RaceGoal
    running_history: Annotated[str, trimmed(2000), MULTI_LINE]
    injuries: list[Injury] = Field(max_length=MAX_INJURIES)
    availability: Availability
    notes: Annotated[str, trimmed(2000), MULTI_LINE] | None = None


class OnboardingResult(BaseModel):
    """The profile and the reason for the macro, sent when the interview ends."""

    model_config = _SHAPE_ONLY

    profile: AthleteProfile
    reason: RevisionReason


def onboarding_result_from_text(text: str, today: date) -> OnboardingResult:
    """
    Parse the result JSON and require a race after today.

    Used as `onboarding_result_from_text(result_json, date(2026, 11, 10))`
    """
    if errors := text_length_errors("result text", text, MAX_RESULT_TEXT_LENGTH):
        raise OnboardingResultError(errors)
    try:
        result = OnboardingResult.model_validate_json(text)
    except ValidationError as error:
        raise OnboardingResultError.from_pydantic(error) from error
    race_date = result.profile.goal.race_date
    if race_date <= today:
        line = (
            f"profile.goal.race_date: race date {race_date.isoformat()} is not "
            f"after today {today.isoformat()}, expected a future race"
        )
        raise OnboardingResultError((line,))
    return result
