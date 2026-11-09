"""Tests for the strict input model of one pushed week."""

from __future__ import annotations

import json
from datetime import date

import pytest

from garmin_push_fakes import WEEK_START, dated_workout, week_text
from training_season.push_week import PushWeekError, push_week_from_text


def _week_with(entries: list[dict[str, object]], **fields: object) -> str:
    return json.dumps({"week_start": WEEK_START, "workouts": entries, **fields})


def _entry(day: str, name: str) -> dict[str, object]:
    workout = dated_workout(day) | {"name": name}
    return {"date": day, "workout": workout}


def test_week_parses_and_orders_entries_by_date() -> None:
    week = push_week_from_text(week_text(["2027-01-08", "2027-01-04", "2027-01-10"]))

    assert week.week_start == date(2027, 1, 4)
    assert week.week_end == date(2027, 1, 10)
    assert [entry.date for entry in week.ordered_entries()] == [
        date(2027, 1, 4),
        date(2027, 1, 8),
        date(2027, 1, 10),
    ]
    assert week.ordered_entries()[0].workout.name == "04/01 Leve"


def test_empty_week_is_valid() -> None:
    assert push_week_from_text(week_text([])).ordered_entries() == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (week_text(["2027-01-11"]), "outside the week 2027-01-04..2027-01-10"),
        (week_text(["2027-01-03"]), "outside the week 2027-01-04..2027-01-10"),
        (week_text(["2027-01-05", "2027-01-05"]), "two workouts on 2027-01-05"),
        (week_text([], week_start="2027-1-4"), "expected a YYYY-MM-DD date"),
        (_week_with([], week_start=20270104), "expected a YYYY-MM-DD date"),
        (_week_with([_entry("2027-01-05", "06/01 Leve")]), "must start with '05/01 '"),
        (_week_with([_entry("2027-01-05", "05/01 Fácil")]), "expected '05/01 Facil'"),
        (_week_with([], extra="x"), "extra: Extra inputs are not permitted"),
        ("not json", "Invalid JSON"),
    ],
)
def test_invalid_week_names_the_offending_value(text: str, expected: str) -> None:
    with pytest.raises(PushWeekError) as raised:
        push_week_from_text(text)

    assert expected in str(raised.value)
