"""Tests for the shared text rules of untrusted model JSON."""

from typing import Annotated

import pytest
from pydantic import BaseModel, ValidationError

from training_season.bounded_text import (
    MULTI_LINE,
    SINGLE_LINE,
    drop_control_characters_but_newline,
    text_length_errors,
    trimmed,
)


class _Note(BaseModel):
    title: Annotated[str, trimmed(5), SINGLE_LINE]
    body: Annotated[str, trimmed(20), MULTI_LINE]


def test_text_is_trimmed_and_keeps_inner_newlines() -> None:
    note = _Note(title="  Oi  ", body="linha 1\nlinha 2")

    assert (note.title, note.body) == ("Oi", "linha 1\nlinha 2")


@pytest.mark.parametrize(
    ("title", "body"),
    [("   ", "x"), ("longo!", "x"), ("a\nb", "x"), ("Oi", "a\tb"), ("Oi", "")],
)
def test_blank_long_or_control_text_is_rejected(title: str, body: str) -> None:
    with pytest.raises(ValidationError):
        _Note(title=title, body=body)


def test_drop_keeps_newlines_and_removes_other_control_characters() -> None:
    text = "a\x1b[2Jb\tc\r\nd\u200be\x07"

    assert drop_control_characters_but_newline(text) == "a[2Jbc\nde"


def test_text_length_errors_names_the_size_and_the_limit() -> None:
    assert text_length_errors("plan", "abc", 3) == ()
    assert text_length_errors("plan", "abcd", 3) == (
        "plan has 4 characters, the maximum is 3",
    )
