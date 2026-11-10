"""Tests for the onboarding result and the athlete profile it carries."""

import json
from datetime import date, timedelta

import pytest

from onboarding_rows import (
    RACE_DAY,
    REASON,
    TODAY,
    availability_payload,
    goal_payload,
    profile_payload,
    result_text,
)
from training_season.athlete_profile import (
    MAX_RESULT_TEXT_LENGTH,
    OnboardingResultError,
    onboarding_result_from_text,
)


def _errors(text: str) -> tuple[str, ...]:
    with pytest.raises(OnboardingResultError) as raised:
        onboarding_result_from_text(text, TODAY)
    return raised.value.errors


def test_valid_result_parses_profile_and_reason() -> None:
    result = onboarding_result_from_text(result_text(), TODAY)

    assert result.reason == REASON
    assert result.profile.athlete_name == "Gabi"
    assert result.profile.goal.race_date == RACE_DAY
    assert result.profile.availability.preferred_days == [
        "tuesday",
        "thursday",
        "saturday",
        "sunday",
    ]
    assert result.profile.injuries[0].status == "recovered"


def test_athlete_name_may_be_null() -> None:
    text = result_text(profile=profile_payload(athlete_name=None))

    assert onboarding_result_from_text(text, TODAY).profile.athlete_name is None


@pytest.mark.parametrize("race_day", [TODAY, TODAY - timedelta(days=1)])
def test_race_date_not_after_today_is_rejected(race_day: date) -> None:
    profile = profile_payload(goal=goal_payload(race_date=race_day))

    errors = _errors(result_text(profile=profile))

    expected = (
        f"profile.goal.race_date: race date {race_day.isoformat()} is not after "
        "today 2026-11-10, expected a future race"
    )
    assert errors == (expected,)


def test_duplicate_preferred_days_are_rejected() -> None:
    availability = availability_payload(preferred_days=["monday", "monday"])

    errors = _errors(result_text(profile=profile_payload(availability=availability)))

    assert any("monday" in line and "twice" in line for line in errors)


def test_more_preferred_days_than_days_per_week_are_rejected() -> None:
    availability = availability_payload(
        days_per_week=2, preferred_days=["monday", "wednesday", "friday"]
    )

    errors = _errors(result_text(profile=profile_payload(availability=availability)))

    assert any("3 preferred days" in line and "2" in line for line in errors)


def test_unknown_weekday_is_rejected() -> None:
    availability = availability_payload(preferred_days=["segunda"])

    errors = _errors(result_text(profile=profile_payload(availability=availability)))

    assert any("preferred_days" in line for line in errors)


@pytest.mark.parametrize("bad_number", [float("nan"), float("inf"), 0, -5])
def test_distance_must_be_finite_and_positive(bad_number: float) -> None:
    profile = profile_payload(goal=goal_payload(distance_km=bad_number))

    errors = _errors(result_text(profile=profile))

    assert any("goal.distance_km" in line for line in errors)


def test_unknown_key_is_rejected() -> None:
    errors = _errors(result_text(profile=profile_payload(favorite_color="azul")))

    assert any("favorite_color" in line for line in errors)


def test_unknown_top_level_key_is_rejected() -> None:
    assert any("extra" in line for line in _errors(result_text(extra="x")))


def test_oversize_running_history_is_rejected() -> None:
    profile = profile_payload(running_history="x" * 2001)

    errors = _errors(result_text(profile=profile))

    assert any("running_history" in line for line in errors)


def test_more_than_twenty_injuries_are_rejected() -> None:
    injury = {"description": "Dor", "status": "active"}
    profile = profile_payload(injuries=[injury] * 21)

    errors = _errors(result_text(profile=profile))

    assert any("injuries" in line for line in errors)


@pytest.mark.parametrize("reason", ["", "   "])
def test_blank_reason_is_rejected(reason: str) -> None:
    assert any("reason" in line for line in _errors(result_text(reason=reason)))


def test_control_character_in_name_is_rejected() -> None:
    profile = profile_payload(athlete_name="Ga\u0007bi")

    errors = _errors(result_text(profile=profile))

    assert any("U+0007" in line for line in errors)


def test_oversize_text_is_rejected_before_parsing() -> None:
    text = json.dumps({"padding": "x" * MAX_RESULT_TEXT_LENGTH})

    errors = _errors(text)

    assert errors == (f"result text has {len(text)} characters, the maximum is 20000",)


def test_error_lines_are_bounded() -> None:
    profile = profile_payload(athlete_name="\n" * 500)

    errors = _errors(result_text(profile=profile))

    assert all(len(line) <= 200 and "\n" not in line for line in errors)
