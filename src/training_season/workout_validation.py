"""Turn coach model text into a validated workout, with readable errors."""

from __future__ import annotations

import re
import unicodedata

from pydantic import ValidationError

from training_season.workout_schema import Workout

_FENCED_JSON = re.compile(r"\A```json[ \t]*\n(.*)\n```\Z", re.DOTALL)
_MAX_LINE_LENGTH = 200
MAX_ERROR_LINES = 20
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


class WorkoutValidationError(ValueError):
    """The text is not a valid workout. `errors` holds one line per problem."""

    def __init__(self, errors: tuple[str, ...]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors

    @classmethod
    def from_pydantic(cls, error: ValidationError) -> WorkoutValidationError:
        """
        Build bounded, single-line errors from a Pydantic error.

        Keeps 20 lines of at most 200 characters, with no control characters,
        and ends with `and N more errors` when lines were dropped.

        Used as `WorkoutValidationError.from_pydantic(caught_error)`
        """
        items = error.errors()
        lines = [
            bounded_line(f"{_json_path(item['loc'])}: {item['msg']}")
            for item in items[:MAX_ERROR_LINES]
        ]
        if len(items) > MAX_ERROR_LINES:
            lines.append(f"and {len(items) - MAX_ERROR_LINES} more errors")
        return cls(tuple(lines))


def bounded_line(line: str) -> str:
    """
    Drop control, format, line and paragraph separator characters.

    Cuts the line to 200 characters. A cut line ends with `…`.

    Used as `bounded_line(f"bad value{chr(10)}")`, giving `bad value`
    """
    clean = "".join(
        character
        for character in line
        if unicodedata.category(character) not in _DROPPED_CATEGORIES
    )
    if len(clean) <= _MAX_LINE_LENGTH:
        return clean
    return f"{clean[: _MAX_LINE_LENGTH - 1]}…"


def _json_path(location: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in location) or "$"


def json_body(text: str) -> str:
    """
    Strip whitespace and one ```json fence around a model reply.

    Used as `json_body(reply.text)`: a fenced `{}` gives `{}`
    """
    stripped = text.strip()
    fenced = _FENCED_JSON.match(stripped)
    return fenced.group(1) if fenced else stripped


def workout_from_text(text: str) -> Workout:
    """
    Parse model text, plain or in one ```json fence, into a validated workout.

    Used as `workout_from_text('{"name": "10x400", "steps": [...]}')`
    """
    try:
        return Workout.model_validate_json(json_body(text))
    except ValidationError as error:
        raise WorkoutValidationError.from_pydantic(error) from error
