"""Strict input for one pushed week: dated workouts inside a 7-day span."""

from datetime import date, timedelta
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    model_serializer,
    model_validator,
)

from training_season.timeformat import parse_iso_date
from training_season.workout_name import require_distinct_names, workout_name
from training_season.workout_schema import Workout
from training_season.workout_validation import WorkoutValidationError

WEEK_DAYS = 7
_SHAPE_ONLY = Workout.model_config


class PushWeekError(WorkoutValidationError):
    """The week file is not a valid week. `errors` holds one line per problem."""


def _strict_iso_date(value: object) -> date:
    if type(value) is date:
        return value
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


def _full_week_end(week_start: date) -> date:
    return week_start + timedelta(days=WEEK_DAYS - 1)


def _default_week_end(validated: dict[str, object]) -> date:
    week_start = validated.get("week_start")
    if not isinstance(week_start, date):
        # Pydantic 2.14 calls this for a missing week_start; its error is reported.
        return date.min
    return _full_week_end(week_start)


class PushWeek(BaseModel):
    """
    The full replacement for week_start..week_end: at most one workout per date.

    `week_end` is optional on the wire and defaults to `week_start + 6`. A
    shorter week (onboarding week 1) leaves the days after it untouched.
    """

    model_config = _SHAPE_ONLY

    week_start: IsoDate
    week_end: IsoDate = Field(default_factory=_default_week_end)
    workouts: list[PushEntry] = Field(max_length=WEEK_DAYS)

    def ordered_entries(self) -> tuple[PushEntry, ...]:
        """Entries in the date order the push uses: `week.ordered_entries()`."""
        return tuple(sorted(self.workouts, key=lambda entry: entry.date))

    @model_serializer(mode="wrap")
    def _omit_full_week_end(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        serialized: dict[str, object] = handler(self)
        if self.week_end == _full_week_end(self.week_start):
            serialized.pop("week_end", None)
        return serialized

    @model_validator(mode="after")
    def _require_week_end_in_seven_days(self) -> Self:
        full_end = _full_week_end(self.week_start)
        if not self.week_start <= self.week_end <= full_end:
            raise ValueError(
                f"week_end {self.week_end.isoformat()} is outside "
                f"{self.week_start.isoformat()}..{full_end.isoformat()}"
            )
        return self

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
        raise PushWeekError.from_pydantic(error) from error
