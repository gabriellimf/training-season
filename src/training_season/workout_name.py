"""Workout name builder for the Forerunner 165 first-15-characters rule."""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from training_season.workout_schema import MAX_WORKOUT_NAME_LENGTH
from training_season.workout_validation import bounded_line

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

_PRINTABLE_ASCII = frozenset(chr(code) for code in range(0x20, 0x7F))
_MEANING_CHANGING_CATEGORIES = frozenset({"No", "Nl"})
_ASCII_SHAPE = "printable ASCII (U+0020 to U+007E)"


class WorkoutNameError(ValueError):
    """A workout name breaks the ASCII, length or uniqueness rule."""

    def __init__(self, message: str) -> None:
        super().__init__(bounded_line(message))


def _no_ascii_form(character: str, text: str) -> WorkoutNameError:
    return WorkoutNameError(
        f"character U+{ord(character):04X} has no ASCII form, "
        f"expected {_ASCII_SHAPE}: {text!r}"
    )


def _fold_character(character: str, text: str) -> str:
    if unicodedata.category(character) in _MEANING_CHANGING_CATEGORIES:
        raise WorkoutNameError(
            f"character U+{ord(character):04X} would change meaning when "
            f"folded to ASCII, expected {_ASCII_SHAPE}: {text!r}"
        )
    decomposed = unicodedata.normalize("NFKD", character)
    folded = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    if not _PRINTABLE_ASCII.issuperset(folded):
        raise _no_ascii_form(character, text)
    return folded


def _ascii_token(content: str) -> str:
    folded = "".join(_fold_character(character, content) for character in content)
    token = " ".join(folded.split())
    if not token:
        raise WorkoutNameError(
            "content is empty after removing accents and whitespace, "
            f"expected a short token such as '10x400': {content!r}"
        )
    return token


def workout_name(day: date, content: str) -> str:
    """
    Build `DD/MM <content>` in ASCII and fail loudly above 15 characters.

    The watch compares only the first 15 characters, so the name is never
    truncated. Accents are removed, and a character that would change meaning
    (`10²` would become `102`) is rejected.

    Used as `workout_name(date(2026, 10, 26), "10x400")`, giving `26/10 10x400`
    """
    name = f"{day:%d/%m} {_ascii_token(content)}"
    if len(name) > MAX_WORKOUT_NAME_LENGTH:
        raise WorkoutNameError(
            f"workout name is {len(name)} characters, expected at most "
            f"{MAX_WORKOUT_NAME_LENGTH}: {name!r}"
        )
    return name


def _comparison_key(position: int, name: str) -> str:
    for character in name:
        if character not in _PRINTABLE_ASCII:
            raise WorkoutNameError(
                f"character U+{ord(character):04X} of the name at position "
                f"{position} is not printable ASCII, expected {_ASCII_SHAPE}: "
                f"{name!r}"
            )
    if len(name) > MAX_WORKOUT_NAME_LENGTH:
        raise WorkoutNameError(
            f"name at position {position} is {len(name)} characters, expected "
            f"at most {MAX_WORKOUT_NAME_LENGTH}: {name!r}"
        )
    return " ".join(name.split()).casefold()


def require_distinct_names(names: Sequence[str]) -> None:
    """
    Fail on the first pair of names that match in their first 15 characters.

    The compare ignores case and repeated spaces, because Garmin handling of
    both is unknown. A name above 15 characters or with a character outside
    printable ASCII fails: the watch compares the raw first 15 characters, so
    only a name of 15 or fewer gives an exact key.

    Used as `require_distinct_names(["26/10 10x400", "28/10 Leve 6k"])`
    """
    first_position: dict[str, int] = {}
    for position, name in enumerate(names, start=1):
        key = _comparison_key(position, name)
        if key in first_position:
            raise WorkoutNameError(
                f"workout names at positions {first_position[key]} and "
                f"{position} collide on the first {MAX_WORKOUT_NAME_LENGTH} "
                f"characters, expected distinct names: {name!r}"
            )
        first_position[key] = position
