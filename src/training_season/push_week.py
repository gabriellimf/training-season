"""Strict input for one pushed week: dated workouts inside a 7-day span."""

from datetime import date, timedelta
from typing import Annotated, Self

from pydantic import BaseModel, BeforeValidator, Field, ValidationError, model_validator

from training_season.timeformat import parse_iso_date
from training_season.workout_name import require_distinct_names, workout_name
from training_season.workout_schema import Workout
from training_season.workout_validation import WorkoutValidationError

WEEK_DAYS = 7
_SHAPE_ONLY = Workout.model_config


class PushWeekError(ValueError):
    """The week file is not a valid week. `errors` holds one line per problem."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _strict_iso_date(value: object) -> date:
    parsed = parse_iso_date(value)
    if parsed is None:
        raise ValueError(f"got {value!r}, expected a YYYY-MM-DD date")
    return parsed


IsoDate = Annotated[date, BeforeValidator(_strict_iso_date)]


class PushEntry(BaseModel):
    """One workout and the calendar date it is scheduled on."""

    model_config = _SHAPE_ONLY

    date: IsoDate
    workout: Workout

    @model_validator(mode="after")
    def _require_dated_name(self) -> Self:
        name = self.workout.name
        prefix = f"{self.date:%d/%m} "
        if not name.startswith(prefix):
            raise ValueError(
                f"workout name {name!r} must start with {prefix!r}, "
                f"expected 'DD/MM <token>' for {self.date.isoformat()}"
            )
        expected = workout_name(self.date, name.removeprefix(prefix))
        if expected != name:
            raise ValueError(f"workout name is {name!r}, expected {expected!r}")
        return self


class PushWeek(BaseModel):
    """The full replacement for one week: at most one workout per date."""

    model_config = _SHAPE_ONLY

    week_start: IsoDate
    workouts: list[PushEntry] = Field(max_length=WEEK_DAYS)

    @property
    def week_end(self) -> date:
        """Last day of the week: `week.week_end`."""
        return self.week_start + timedelta(days=WEEK_DAYS - 1)

    def ordered_entries(self) -> tuple[PushEntry, ...]:
        """Entries in the date order the push uses: `week.ordered_entries()`."""
        return tuple(sorted(self.workouts, key=lambda entry: entry.date))

    @model_validator(mode="after")
    def _require_one_workout_per_day_in_week(self) -> Self:
        seen: set[date] = set()
        for entry in self.workouts:
            if not self.week_start <= entry.date <= self.week_end:
                raise ValueError(
                    f"workout date {entry.date.isoformat()} is outside the week "
                    f"{self.week_start.isoformat()}..{self.week_end.isoformat()}"
                )
            if entry.date in seen:
                raise ValueError(
                    f"two workouts on {entry.date.isoformat()}, "
                    "expected at most one per date"
                )
            seen.add(entry.date)
        require_distinct_names([entry.workout.name for entry in self.workouts])
        return self


def push_week_from_text(text: str) -> PushWeek:
    """
    Parse the week JSON file before any Garmin call, with bounded error lines.

    Used as `push_week_from_text('{"week_start": "2027-01-04", "workouts": []}')`
    """
    try:
        return PushWeek.model_validate_json(text)
    except ValidationError as error:
        raise PushWeekError(
            WorkoutValidationError.from_pydantic(error).errors
        ) from error
