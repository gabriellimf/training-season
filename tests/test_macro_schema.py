"""Tests for the macro plan shape: anchored dates and contiguous phases."""

import json
from datetime import date, timedelta

import pytest

from onboarding_rows import RACE_DAY, TODAY, default_phases, macro_text, phase_payload
from training_season.macro_schema import (
    MAX_MACRO_TEXT_LENGTH,
    MacroPlanError,
    macro_plan_from_text,
)


def _errors(text: str) -> tuple[str, ...]:
    with pytest.raises(MacroPlanError) as raised:
        macro_plan_from_text(text, TODAY, RACE_DAY)
    return raised.value.errors


def _phases_with(index: int, **changes: object) -> list[dict[str, object]]:
    phases = default_phases()
    phases[index] = {**phases[index], **changes}
    return phases


def test_valid_plan_parses() -> None:
    plan = macro_plan_from_text(macro_text(), TODAY, RACE_DAY)

    assert plan.start_date == TODAY
    assert plan.race_date == RACE_DAY
    assert [phase.name for phase in plan.phases] == ["Base", "Build", "Taper"]
    assert plan.phases[0].weekly_km.max == 30


def test_start_date_must_be_today() -> None:
    tomorrow = TODAY + timedelta(days=1)
    text = macro_text(start_date=tomorrow)

    assert _errors(text) == ("start_date: plan starts 2026-11-11, expected 2026-11-10",)


def test_race_date_must_match_the_profile() -> None:
    text = macro_text(race_date=date(2027, 5, 2))

    assert _errors(text) == (
        "race_date: plan race date 2027-05-02, expected 2027-04-18",
    )


def test_first_phase_must_start_on_the_plan_start() -> None:
    phases = _phases_with(0, start_date="2026-11-11")

    errors = _errors(macro_text(phases=phases))

    assert errors == ("$: Value error, phase 1 starts 2026-11-11, expected 2026-11-10",)


def test_gap_between_phases_is_rejected() -> None:
    phases = _phases_with(1, start_date="2026-12-02")

    errors = _errors(macro_text(phases=phases))

    assert errors == ("$: Value error, phase 2 starts 2026-12-02, expected 2026-12-01",)


def test_overlap_between_phases_is_rejected() -> None:
    phases = _phases_with(1, start_date="2026-11-30")

    errors = _errors(macro_text(phases=phases))

    assert errors == ("$: Value error, phase 2 starts 2026-11-30, expected 2026-12-01",)


def test_last_phase_must_end_on_the_race_date() -> None:
    phases = _phases_with(2, end_date="2027-04-17")

    errors = _errors(macro_text(phases=phases))

    assert errors == (
        "$: Value error, last phase ends 2027-04-17, expected the race date 2027-04-18",
    )


def test_phase_end_before_start_is_rejected() -> None:
    phase = phase_payload("Base", TODAY, TODAY - timedelta(days=1))

    errors = _errors(macro_text(phases=[phase]))

    assert any("phases.0" in line and "2026-11-09" in line for line in errors)


@pytest.mark.parametrize(
    "weekly_km",
    [
        {"min": float("nan"), "max": 30},
        {"min": 20, "max": float("inf")},
        {"min": -1, "max": 30},
        {"min": 20, "max": 301},
        {"min": 40, "max": 30},
    ],
)
def test_weekly_km_must_be_finite_ordered_and_bounded(
    weekly_km: dict[str, float],
) -> None:
    phase = phase_payload("Base", TODAY, RACE_DAY, weekly_km=weekly_km)

    errors = _errors(macro_text(phases=[phase]))

    assert any("phases.0.weekly_km" in line for line in errors)


def test_no_phases_is_rejected() -> None:
    assert any("phases" in line for line in _errors(macro_text(phases=[])))


def test_more_than_sixty_phases_is_rejected() -> None:
    phases = [
        phase_payload(
            f"F{day}", TODAY + timedelta(days=day), TODAY + timedelta(days=day)
        )
        for day in range(61)
    ]

    assert any("phases" in line for line in _errors(macro_text(phases=phases)))


def test_unknown_key_is_rejected() -> None:
    phases = _phases_with(0, intensity="alta")

    errors = _errors(macro_text(phases=phases))

    assert any("intensity" in line for line in errors)


@pytest.mark.parametrize(
    ("field", "length"), [("name", 61), ("focus", 1001), ("summary", 2001)]
)
def test_oversize_text_fields_are_rejected(field: str, length: int) -> None:
    if field == "summary":
        text = macro_text(summary="x" * length)
    else:
        text = macro_text(phases=_phases_with(0, **{field: "x" * length}))

    assert any(field in line for line in _errors(text))


def test_oversize_text_is_rejected_before_parsing() -> None:
    text = json.dumps({"padding": "x" * MAX_MACRO_TEXT_LENGTH})

    errors = _errors(text)

    assert errors == (
        f"macro plan text has {len(text)} characters, the maximum is 100000",
    )
