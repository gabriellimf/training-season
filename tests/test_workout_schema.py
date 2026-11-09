from __future__ import annotations

import pytest
from pydantic import ValidationError

from training_season.workout_schema import Workout
from workout_fixtures import (
    JsonObject,
    easy_step,
    example_workout,
    repeat_group,
    workout_with_steps,
)


def rejection(payload: JsonObject) -> str:
    with pytest.raises(ValidationError) as caught:
        Workout.model_validate(payload)
    return str(caught.value)


def test_example_workout_passes() -> None:
    workout = Workout.model_validate(example_workout())

    assert workout.name == "26/10 10x400"
    assert [step.type for step in workout.steps] == ["warmup", "repeat", "cooldown"]


def test_fifty_steps_pass() -> None:
    steps = [easy_step() for _ in range(50)]

    assert len(Workout.model_validate(workout_with_steps(steps)).steps) == 50


def test_fifty_one_steps_are_rejected_naming_both_numbers() -> None:
    message = rejection(workout_with_steps([easy_step() for _ in range(51)]))

    assert "51 steps" in message
    assert "maximum is 50" in message


def test_repeat_children_and_group_count_toward_the_limit() -> None:
    steps = [easy_step() for _ in range(48)]
    steps.append(repeat_group(2, [easy_step("interval"), easy_step("recovery")]))

    assert "51 steps" in rejection(workout_with_steps(steps))


def test_repeat_count_is_not_multiplied_into_the_step_count() -> None:
    steps = [repeat_group(99, [easy_step("interval"), easy_step("recovery")])]

    assert Workout.model_validate(workout_with_steps(steps)).steps


def test_repeat_count_one_hundred_is_rejected() -> None:
    steps = [repeat_group(100, [easy_step("interval")])]

    assert "count" in rejection(workout_with_steps(steps))


def test_name_of_fifteen_characters_passes() -> None:
    payload = workout_with_steps([easy_step()], name="a" * 15)

    assert Workout.model_validate(payload).name == "a" * 15


def test_name_of_sixteen_characters_is_rejected() -> None:
    payload = workout_with_steps([easy_step()], name="a" * 16)

    assert "name" in rejection(payload)


def test_blank_name_is_rejected() -> None:
    assert "name" in rejection(workout_with_steps([easy_step()], name="   "))


def test_name_is_stripped() -> None:
    payload = workout_with_steps([easy_step()], name="  10x400  ")

    assert Workout.model_validate(payload).name == "10x400"


def test_pace_minimum_above_maximum_is_rejected() -> None:
    step = easy_step()
    step["pace_mps"] = {"min": 3.0, "max": 2.0}

    assert "pace min 3.0 is above max 2.0" in rejection(workout_with_steps([step]))


def test_duration_with_both_keys_is_rejected() -> None:
    step = easy_step()
    step["duration"] = {"meters": 400, "seconds": 90}

    assert "exactly one of meters or seconds" in rejection(workout_with_steps([step]))


def test_duration_without_a_key_is_rejected() -> None:
    step = easy_step()
    step["duration"] = {}

    assert "exactly one of meters or seconds" in rejection(workout_with_steps([step]))


def test_step_without_pace_is_rejected() -> None:
    step = easy_step()
    del step["pace_mps"]

    assert "pace_mps" in rejection(workout_with_steps([step]))


def test_unknown_step_type_is_rejected() -> None:
    assert "swim" in rejection(workout_with_steps([easy_step("swim")]))


def test_nested_repeat_group_is_rejected() -> None:
    inner = repeat_group(2, [easy_step("interval")])
    steps = [repeat_group(3, [inner])]

    assert "nested repeat groups are not supported" in rejection(
        workout_with_steps(steps)
    )


def test_empty_repeat_group_is_rejected() -> None:
    assert "steps" in rejection(workout_with_steps([repeat_group(2, [])]))


def test_empty_workout_is_rejected() -> None:
    assert "steps" in rejection(workout_with_steps([]))


def test_unknown_key_is_rejected() -> None:
    step = easy_step()
    step["speed"] = 3

    assert "speed" in rejection(workout_with_steps([step]))


def test_heart_rate_zone_six_is_rejected() -> None:
    step = easy_step()
    step["heart_rate_zone"] = 6

    assert "heart_rate_zone" in rejection(workout_with_steps([step]))


def test_nested_repeat_message_names_count_and_child_without_a_dump() -> None:
    inner = repeat_group(2, [easy_step("interval")])
    message = rejection(workout_with_steps([repeat_group(3, [inner])]))

    assert "got a repeat with count=2 at child 0" in message
    assert "pace_mps" not in message


def test_boolean_heart_rate_zone_is_rejected() -> None:
    step = easy_step()
    step["heart_rate_zone"] = True

    assert "heart_rate_zone" in rejection(workout_with_steps([step]))


def test_string_meters_are_rejected() -> None:
    step = easy_step()
    step["duration"] = {"meters": "1000"}

    assert "meters" in rejection(workout_with_steps([step]))


def test_integer_pace_fills_the_float_field() -> None:
    step = easy_step()
    step["pace_mps"] = {"min": 3, "max": 4}

    assert Workout.model_validate(workout_with_steps([step])).steps


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration", {"meters": 0}),
        ("duration", {"seconds": 0}),
        ("duration", {"meters": 1_000_001}),
        ("duration", {"seconds": 86_401}),
        ("pace_mps", {"min": 0, "max": 2.9}),
        ("pace_mps", {"min": 2.6, "max": 101}),
        ("heart_rate_zone", 0),
        ("cadence_spm", {"min": 0, "max": 180}),
        ("cadence_spm", {"min": 170, "max": 401}),
    ],
)
def test_step_values_outside_the_shape_bounds_are_rejected(
    field: str, value: object
) -> None:
    step = easy_step()
    step[field] = value

    assert field in rejection(workout_with_steps([step]))


def test_repeat_count_zero_is_rejected() -> None:
    steps = [repeat_group(0, [easy_step("interval")])]

    assert "count" in rejection(workout_with_steps(steps))


@pytest.mark.parametrize("name", ["\x1b[2J", "ab\u200bc", "a\nb"])
def test_name_with_control_or_format_characters_is_rejected(name: str) -> None:
    assert "name" in rejection(workout_with_steps([easy_step()], name=name))


def test_notes_of_501_characters_are_rejected() -> None:
    step = easy_step()
    step["notes"] = "a" * 501

    assert "notes" in rejection(workout_with_steps([step]))


def test_notes_with_an_escape_character_are_rejected() -> None:
    step = easy_step()
    step["notes"] = "Trote\x1b[2J"

    assert "notes" in rejection(workout_with_steps([step]))


def test_description_with_a_newline_is_accepted() -> None:
    payload = {**workout_with_steps([easy_step()]), "description": "Linha 1\nLinha 2"}

    assert Workout.model_validate(payload).description == "Linha 1\nLinha 2"


def test_description_of_2001_characters_is_rejected() -> None:
    payload = {**workout_with_steps([easy_step()]), "description": "a" * 2001}

    assert "description" in rejection(payload)


def test_cadence_minimum_above_maximum_is_rejected() -> None:
    step = easy_step()
    step["cadence_spm"] = {"min": 190, "max": 170}

    assert "cadence min 190 is above max 170" in rejection(workout_with_steps([step]))
