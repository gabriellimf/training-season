"""Tests for the Portuguese text of the workout steps on the plan card."""

from __future__ import annotations

import json

import pytest

from training_season.coach_context_lines import clock_text
from training_season.workout_schema import Workout
from training_season.workout_text import StepText, workout_step_texts

_EASY_PACE = {"min": 2.941, "max": 3.125}
_EVEN_PACE = {"min": 4.1667, "max": 4.1667}
_EASY_PACE_TEXT = "ritmo 5:20\N{EN DASH}5:40 /km"


def _workout(*steps: dict[str, object]) -> Workout:
    return Workout.model_validate_json(
        json.dumps({"name": "17/11 Leve", "steps": list(steps)})
    )


def _timed(
    step_type: str, duration: dict[str, int], **extra: object
) -> dict[str, object]:
    return {"type": step_type, "duration": duration, "pace_mps": _EASY_PACE, **extra}


def test_timed_step_names_type_duration_and_pace_faster_first() -> None:
    workout = _workout(_timed("warmup", {"seconds": 600}))

    assert workout_step_texts(workout.steps) == (
        StepText(f"Aquecimento · 10 min · {_EASY_PACE_TEXT}", None, None),
    )


@pytest.mark.parametrize(
    ("step_type", "duration", "expected"),
    [
        ("interval", {"meters": 400}, "Corrida · 400 m"),
        ("recovery", {"meters": 999}, "Recuperação · 999 m"),
        ("cooldown", {"meters": 1000}, "Desaquecimento · 1,0 km"),
        ("warmup", {"meters": 1500}, "Aquecimento · 1,5 km"),
        ("warmup", {"seconds": 90}, "Aquecimento · 1 min 30 s"),
        ("warmup", {"seconds": 45}, "Aquecimento · 45 s"),
    ],
)
def test_duration_text_per_unit(
    step_type: str, duration: dict[str, int], expected: str
) -> None:
    workout = _workout(_timed(step_type, duration))

    text = workout_step_texts(workout.steps)[0].text

    assert text == f"{expected} · {_EASY_PACE_TEXT}"


def test_equal_pace_zone_and_cadence_show_one_value_each() -> None:
    step = _timed(
        "interval",
        {"meters": 400},
        heart_rate_zone=4,
        cadence_spm={"min": 170, "max": 180},
    )
    step["pace_mps"] = _EVEN_PACE

    text = workout_step_texts(_workout(step).steps)[0].text

    assert text == "Tiro · 400 m · ritmo 4:00 /km · zona 4 · cadência 170-180 spm"


def test_pace_rounding_carries_into_the_minute() -> None:
    step = _timed("warmup", {"seconds": 600})
    step["pace_mps"] = {"min": 1000 / 359.6, "max": 1000 / 359.6}

    text = workout_step_texts(_workout(step).steps)[0].text

    assert text == "Aquecimento · 10 min · ritmo 6:00 /km"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(359.6, "6:00"), (45, "0:45"), (3599.5, "1:00:00"), (3725, "1:02:05")],
)
def test_clock_text_rounds_to_whole_seconds(seconds: float, text: str) -> None:
    assert clock_text(seconds) == text


@pytest.mark.parametrize(
    ("zone", "label"),
    [(1, "Corrida"), (2, "Corrida"), (3, "Tiro"), (5, "Tiro"), (None, "Corrida")],
)
def test_top_level_interval_label_follows_its_zone(
    zone: int | None, label: str
) -> None:
    extra = {} if zone is None else {"heart_rate_zone": zone}
    workout = _workout(_timed("interval", {"meters": 5000}, **extra))

    text = workout_step_texts(workout.steps)[0].text

    assert text.startswith(f"{label} · 5,0 km · ")


@pytest.mark.parametrize(
    ("zone", "label"), [(2, "Corrida"), (4, "Tiro"), (None, "Tiro")]
)
def test_interval_label_in_a_repeat_follows_its_zone(
    zone: int | None, label: str
) -> None:
    extra = {} if zone is None else {"heart_rate_zone": zone}
    repeat = {
        "type": "repeat",
        "count": 3,
        "steps": [_timed("interval", {"meters": 400}, **extra)],
    }

    child = workout_step_texts(_workout(repeat).steps)[0].steps

    assert child is not None
    assert child[0].text.startswith(f"{label} · 400 m · ")


def test_zone_three_keeps_the_warmup_label() -> None:
    workout = _workout(_timed("warmup", {"meters": 1000}, heart_rate_zone=3))

    text = workout_step_texts(workout.steps)[0].text

    assert text.startswith("Aquecimento · 1,0 km · ")


def test_repeat_lists_its_child_steps_and_keeps_notes_apart() -> None:
    repeat = {
        "type": "repeat",
        "count": 6,
        "steps": [
            _timed("interval", {"meters": 400}, notes="Forte, mas solto."),
            _timed("recovery", {"seconds": 60}),
        ],
    }

    texts = workout_step_texts(_workout(repeat).steps)

    assert texts == (
        StepText(
            "Repita 6x:",
            None,
            (
                StepText(
                    f"Tiro · 400 m · {_EASY_PACE_TEXT}", "Forte, mas solto.", None
                ),
                StepText(f"Recuperação · 1 min · {_EASY_PACE_TEXT}", None, None),
            ),
        ),
    )
