"""The `name` subcommand: build and check the names of one pushed week."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from training_season.timeformat import parse_iso_date
from training_season.workout_name import (
    WorkoutNameError,
    require_distinct_names,
    workout_name,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

_PAIR_SHAPE = "<YYYY-MM-DD> <content> [<YYYY-MM-DD> <content> ...]"


def _parse_day(text: str) -> date:
    parsed = parse_iso_date(text)
    if parsed is None:
        raise WorkoutNameError(
            f"date is not an ISO date, expected YYYY-MM-DD: {text!r}"
        )
    return parsed


def _build_names(arguments: Sequence[str]) -> list[str]:
    if not arguments or len(arguments) % 2 != 0:
        raise WorkoutNameError(
            f"expected pairs of {_PAIR_SHAPE}, got {len(arguments)} arguments"
        )
    pairs = zip(arguments[::2], arguments[1::2], strict=True)
    return [workout_name(_parse_day(day), content) for day, content in pairs]


def run_name_command(arguments: Sequence[str]) -> int:
    """
    Print one workout name per date and content pair, or one error line.

    Used as `run_name_command(["2026-10-26", "10x400"])`
    """
    try:
        names = _build_names(arguments)
        require_distinct_names(names)
    except WorkoutNameError as error:
        sys.stderr.write(f"{error}\n")
        return 2
    sys.stdout.write("".join(f"{name}\n" for name in names))
    return 0
