"""Render the coach instruction with the stored athlete name and race date."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from pydantic import ValidationError

from training_season.athlete_profile import AthleteProfile
from training_season.coach_context_store import read_active_macro, read_profile_json
from training_season.workout_validation import WorkoutValidationError

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from training_season.coach_instruction import CoachInstruction


class StoredProfileError(WorkoutValidationError):
    """`profile.data_json` does not hold an athlete profile."""


def _stored_athlete_name(connection: sqlite3.Connection) -> str | None:
    profile_json = read_profile_json(connection)
    if profile_json is None:
        return None
    try:
        profile = AthleteProfile.model_validate_json(profile_json)
    except ValidationError as error:
        lines = WorkoutValidationError.from_pydantic(error).errors
        raise StoredProfileError(
            ("profile.data_json is not an athlete profile", *lines)
        ) from error
    return profile.athlete_name


def _active_race_date(connection: sqlite3.Connection) -> date | None:
    macro = read_active_macro(connection)
    return None if macro is None else date.fromisoformat(macro.race_date)


def render_coach_system(
    instruction: CoachInstruction,
    connection: sqlite3.Connection,
    *,
    now: datetime,
    zone: ZoneInfo,
) -> str:
    """
    Render the instruction with the profile name and the active macro race date.

    A missing profile or macro renders `-`.

    Used as `render_coach_system(instruction, connection, now=utc_moment, zone=zone)`
    """
    return instruction.render(
        now=now,
        zone=zone,
        athlete_name=_stored_athlete_name(connection),
        race_date=_active_race_date(connection),
    )
