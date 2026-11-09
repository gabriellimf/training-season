from __future__ import annotations

from datetime import date

import pytest

from training_season.workout_name import (
    WorkoutNameError,
    require_distinct_names,
    workout_name,
)


def test_name_is_the_day_and_month_then_the_content() -> None:
    assert workout_name(date(2026, 10, 26), "10x400") == "26/10 10x400"


def test_day_and_month_are_zero_padded() -> None:
    assert workout_name(date(2026, 11, 2), "Leve 6k") == "02/11 Leve 6k"


def test_a_name_of_exactly_15_characters_is_accepted() -> None:
    assert workout_name(date(2026, 10, 30), "Long 14km") == "30/10 Long 14km"


def test_a_name_of_16_characters_is_rejected_with_the_name_and_the_limit() -> None:
    with pytest.raises(WorkoutNameError) as caught:
        workout_name(date(2026, 10, 30), "Long 14.5k")

    message = str(caught.value)
    assert "'30/10 Long 14.5k'" in message
    assert "is 16 characters" in message
    assert "at most 15" in message


def test_outer_whitespace_of_the_content_is_removed() -> None:
    assert workout_name(date(2026, 10, 26), "  10x400  ") == "26/10 10x400"


def test_accents_are_stripped() -> None:
    assert workout_name(date(2026, 10, 26), "Fácil 6k") == "26/10 Facil 6k"
    assert workout_name(date(2026, 10, 26), "Longão") == "26/10 Longao"


def test_a_character_without_an_ascii_form_is_rejected_by_code_point() -> None:
    with pytest.raises(WorkoutNameError) as caught:
        workout_name(date(2026, 10, 26), "6k 🏃")

    assert "U+1F3C3" in str(caught.value)
    assert "'6k 🏃'" in str(caught.value)


def test_a_control_character_is_rejected() -> None:
    with pytest.raises(WorkoutNameError) as caught:
        workout_name(date(2026, 10, 26), "6k\tleve")

    assert "U+0009" in str(caught.value)


@pytest.mark.parametrize("content", ["", "   ", chr(0x0301)])
def test_content_with_no_visible_character_is_rejected(content: str) -> None:
    with pytest.raises(WorkoutNameError, match="empty"):
        workout_name(date(2026, 10, 26), content)


def test_a_superscript_digit_is_rejected_by_its_input_code_point() -> None:
    with pytest.raises(WorkoutNameError, match="U\\+00B2"):
        workout_name(date(2026, 10, 26), "10²")


def test_a_vulgar_fraction_is_rejected_by_its_input_code_point() -> None:
    with pytest.raises(WorkoutNameError, match="U\\+00BD"):
        workout_name(date(2026, 10, 26), "½ 6k")


@pytest.mark.parametrize("content", ["\n10x400", "10x400\x1f", "\x8510x400"])
def test_an_outer_control_character_is_rejected(content: str) -> None:
    with pytest.raises(WorkoutNameError, match="has no ASCII form"):
        workout_name(date(2026, 10, 26), content)


def test_inner_whitespace_runs_collapse_to_one_space() -> None:
    assert workout_name(date(2026, 10, 26), "Leve  6k") == "26/10 Leve 6k"


def test_a_fullwidth_digit_is_folded_to_ascii() -> None:
    content = chr(0xFF11) + chr(0xFF10) + "x400"

    assert workout_name(date(2026, 10, 26), content) == "26/10 10x400"


def test_the_length_is_counted_after_compatibility_folding() -> None:
    with pytest.raises(WorkoutNameError) as caught:
        workout_name(date(2026, 10, 30), "Long 14kﬁ")

    assert "is 16 characters" in str(caught.value)
    assert "'30/10 Long 14kfi'" in str(caught.value)


def test_the_error_text_is_at_most_200_characters() -> None:
    with pytest.raises(WorkoutNameError) as caught:
        workout_name(date(2026, 10, 26), "x" * 10_000)

    assert len(str(caught.value)) <= 200


def test_the_cut_error_text_keeps_the_length_and_the_limit() -> None:
    with pytest.raises(WorkoutNameError) as caught:
        workout_name(date(2026, 10, 26), "x" * 300)

    message = str(caught.value)
    assert "is 306 characters" in message
    assert "at most 15" in message


def test_distinct_names_are_accepted() -> None:
    require_distinct_names(["26/10 10x400", "28/10 Leve 6k", "30/10 Long 14k"])


def test_an_exact_duplicate_names_both_positions() -> None:
    with pytest.raises(WorkoutNameError) as caught:
        require_distinct_names(["26/10 10x400", "28/10 Leve 6k", "26/10 10x400"])

    message = str(caught.value)
    assert "positions 1 and 3" in message
    assert "'26/10 10x400'" in message


def test_names_that_differ_only_in_case_collide() -> None:
    with pytest.raises(WorkoutNameError, match="positions 1 and 2"):
        require_distinct_names(["26/10 Leve 6k", "26/10 LEVE 6K"])


def test_a_name_above_15_characters_is_rejected_with_position_length_and_limit() -> (
    None
):
    with pytest.raises(WorkoutNameError) as caught:
        require_distinct_names(["26/10 Leve 6k", "26/10 Long 14kmX"])

    message = str(caught.value)
    assert "position 2" in message
    assert "is 16 characters" in message
    assert "at most 15" in message
    assert message.endswith("'26/10 Long 14kmX'")


def test_names_that_differ_only_in_trailing_space_collide() -> None:
    with pytest.raises(WorkoutNameError, match="positions 1 and 2"):
        require_distinct_names(["26/10 Leve 6k", "26/10 Leve 6k "])


def test_a_zero_width_space_in_a_name_is_rejected_with_position_and_code_point() -> (
    None
):
    name = "26/10 Leve" + chr(0x200B) + "6k"

    with pytest.raises(WorkoutNameError) as caught:
        require_distinct_names(["26/10 10x400", name])

    message = str(caught.value)
    assert "position 2" in message
    assert "U+200B" in message
    assert "is not printable ASCII" in message
    assert message.endswith(repr(name))


def test_a_newline_in_a_name_is_rejected() -> None:
    with pytest.raises(WorkoutNameError, match="U\\+000A"):
        require_distinct_names(["26/10 10x400\n"])


def test_an_empty_week_is_accepted() -> None:
    require_distinct_names([])
