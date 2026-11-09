from __future__ import annotations

import json
from typing import Any

from training_season.garmin_workout_payload import running_workout
from training_season.workout_request import WORKOUT_OUTPUT_CONTRACT
from training_season.workout_validation import workout_from_text
from workout_fixtures import JsonObject, workout_with_steps

_CONTRACT_EXAMPLE = WORKOUT_OUTPUT_CONTRACT.split("Exemplo:\n", 1)[1]
_PLAIN_INTERVAL: JsonObject = {
    "type": "interval",
    "duration": {"meters": 400},
    "pace_mps": {"min": 4.0, "max": 4.3},
}


def _payload(text: str) -> dict[str, Any]:
    return running_workout(workout_from_text(text)).to_dict()


def _contract_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _payload(_CONTRACT_EXAMPLE)["workoutSegments"][0][
        "workoutSteps"
    ]
    return steps


def _single_step(**optional_fields: object) -> dict[str, Any]:
    step: JsonObject = {**_PLAIN_INTERVAL, **optional_fields}
    workout = _payload(json.dumps(workout_with_steps([step])))
    single: dict[str, Any] = workout["workoutSegments"][0]["workoutSteps"][0]
    return single


def _outline(step: dict[str, Any]) -> tuple[object, ...]:
    return (
        step["stepOrder"],
        step["stepType"]["stepTypeKey"],
        step["endCondition"]["conditionTypeKey"],
        step["endConditionValue"],
    )


def test_contract_example_keeps_name_description_and_running_segment() -> None:
    workout = _payload(_CONTRACT_EXAMPLE)
    segment = workout["workoutSegments"][0]
    assert (workout["workoutName"], workout["description"]) == (
        "26/10 10x400",
        "Tiros de 400 m com trote de recuperação.",
    )
    assert (segment["segmentOrder"], segment["sportType"]["sportTypeKey"]) == (
        1,
        "running",
    )


def test_contract_example_maps_types_orders_and_end_conditions() -> None:
    warmup, repeat, cooldown = _contract_steps()
    interval, recovery = repeat["workoutSteps"]
    assert [_outline(step) for step in (warmup, repeat, interval, recovery)] == [
        (1, "warmup", "distance", 2000.0),
        (2, "repeat", "iterations", 10.0),
        (3, "interval", "distance", 400.0),
        (4, "recovery", "time", 90.0),
    ]
    assert _outline(cooldown) == (5, "cooldown", "time", 600.0)
    assert repeat["numberOfIterations"] == 10


def test_pace_is_the_primary_target_with_the_slower_pace_first() -> None:
    step = _single_step()
    assert step["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert (step["targetValueOne"], step["targetValueTwo"]) == (4.0, 4.3)


def test_heart_rate_zone_alone_is_the_secondary_target() -> None:
    step = _single_step(heart_rate_zone=2)
    assert step["secondaryTargetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert step["secondaryZoneNumber"] == 2
    assert "description" not in step


def test_cadence_alone_is_the_secondary_target() -> None:
    step = _single_step(cadence_spm={"min": 175, "max": 185})
    assert step["secondaryTargetType"]["workoutTargetTypeKey"] == "cadence"
    assert (step["secondaryTargetValueOne"], step["secondaryTargetValueTwo"]) == (
        175.0,
        185.0,
    )
    assert "description" not in step


def test_zone_wins_over_cadence_and_cadence_moves_to_the_description() -> None:
    step = _single_step(
        heart_rate_zone=4, cadence_spm={"min": 175, "max": 185}, notes="Forte"
    )
    assert step["secondaryTargetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert "secondaryTargetValueOne" not in step
    assert step["description"] == "Forte\nCadência 175-185 spm"


def test_estimated_duration_uses_the_mean_pace_for_distance_steps() -> None:
    warmup = 2000 / ((2.6 + 2.9) / 2)
    repeat = 10 * (400 / ((4.0 + 4.3) / 2) + 90)
    expected = round(warmup + repeat + 600)
    assert _payload(_CONTRACT_EXAMPLE)["estimatedDurationInSecs"] == expected == 3191


def test_workout_without_optional_fields_has_no_secondary_target_or_description() -> (
    None
):
    workout = _payload(json.dumps(workout_with_steps([_PLAIN_INTERVAL])))
    step = workout["workoutSegments"][0]["workoutSteps"][0]
    assert {key for key in step if key.startswith("secondary")} == set()
    assert "description" not in step
    assert "description" not in workout
