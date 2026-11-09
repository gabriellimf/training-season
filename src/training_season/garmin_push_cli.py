"""Command-line entry point for the week push and the calendar read-back."""

from __future__ import annotations

import logging
import sqlite3
import sys
from contextlib import closing
from typing import TYPE_CHECKING

from training_season.cli_entry import read_limited_text, run_cli
from training_season.database import (
    DatabaseConfigurationError,
    MigrationError,
    connect,
    database_path_from_environment,
    initialize_database,
)
from training_season.garmin_auth import (
    GarminConfigurationError,
    GarminError,
    GarminMfaRequiredError,
    garmin_auth_config_from_environment,
)
from training_season.garmin_push import push_garmin_week, show_garmin_week
from training_season.push_store import NoActiveMacroPlanError
from training_season.push_week import PushWeekError, push_week_from_text
from training_season.timeformat import parse_iso_date

if TYPE_CHECKING:
    from collections.abc import Sequence

_USAGE = "training-season-push <week.json> | show <YYYY-MM-DD>"
_INPUT_ERROR = 2


class _BadArgumentError(ValueError):
    """An argument is not usable; the message names it and the expected shape."""


def _is_known_command(arguments: tuple[str, ...]) -> bool:
    if len(arguments) == 2:
        return arguments[0] == "show"
    return len(arguments) == 1 and arguments[0] != "show"


def _push_file(path: str) -> int:
    text = read_limited_text(path)
    if text is None:
        return _INPUT_ERROR
    week = push_week_from_text(text)
    initialize_database()
    auth_config = garmin_auth_config_from_environment()
    with closing(connect(database_path_from_environment())) as connection:
        result = push_garmin_week(connection, auth_config, week)
    sys.stdout.write(f"{result.chat_message}\n")
    return 0 if result.failed is None else 1


def _show_week(week_start_text: str) -> int:
    week_start = parse_iso_date(week_start_text)
    if week_start is None:
        raise _BadArgumentError(
            f"week start is {week_start_text!r}, expected YYYY-MM-DD"
        )
    lines = show_garmin_week(garmin_auth_config_from_environment(), week_start)
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    return 0


def _run_command(arguments: tuple[str, ...]) -> int:
    if arguments[0] == "show":
        return _show_week(arguments[1])
    return _push_file(arguments[0])


def _push_cli(arguments: tuple[str, ...]) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        return _run_command(arguments)
    except GarminMfaRequiredError as error:
        sys.stderr.write(f"{error}\n")
        return 3
    except PushWeekError as error:
        sys.stderr.write("".join(f"{line}\n" for line in error.errors))
        return _INPUT_ERROR
    except (
        _BadArgumentError,
        DatabaseConfigurationError,
        GarminConfigurationError,
        NoActiveMacroPlanError,
    ) as error:
        sys.stderr.write(f"{error}\n")
        return _INPUT_ERROR
    except (GarminError, MigrationError, sqlite3.Error) as error:
        sys.stderr.write(f"{error}\n")
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    """
    Push one week file to the Garmin calendar, or show one week of it.

    Used as `main(["week.json"])` or `main(["show", "2027-01-04"])`
    """
    return run_cli(argv, _USAGE, _is_known_command, _push_cli)


if __name__ == "__main__":
    raise SystemExit(main())
