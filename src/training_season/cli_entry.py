"""Usage line and argument check shared by the command-line entry points."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

_HELP_ARGUMENTS = (("--help",), ("-h",))
MAX_INPUT_FILE_BYTES = 1_048_576


def read_limited_text(path: str) -> str | None:
    """
    Read a UTF-8 input file of at most 1 MiB, or print why not and return None.

    Used as `text = read_limited_text("week.json")`
    """
    try:
        size = Path(path).stat().st_size
        if size > MAX_INPUT_FILE_BYTES:
            sys.stderr.write(
                f"file {path} is {size} bytes, above the limit of "
                f"{MAX_INPUT_FILE_BYTES} bytes\n"
            )
            return None
        with Path(path).open("rb") as handle:
            return handle.read(MAX_INPUT_FILE_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        sys.stderr.write(f"cannot read file {path}: {error}\n")
        return None


def report_failure(error: Exception, exit_code: int) -> int:
    """
    Write the error message on stderr and return the exit code for it.

    Used as `return report_failure(error, 2)`
    """
    sys.stderr.write(f"{error}\n")
    return exit_code


def run_cli(
    argv: Sequence[str] | None,
    usage: str,
    accepted: Collection[tuple[str, ...]] | Callable[[tuple[str, ...]], bool],
    run: Callable[[tuple[str, ...]], int],
) -> int:
    """
    Print the usage line for help or a bad argument list, else run the command.

    `accepted` is a collection of exact argument tuples or a predicate.

    Used as
    `run_cli(argv, "training-season-llm check", {("check",)}, _print_llm_models)`
    """
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments in _HELP_ARGUMENTS:
        sys.stderr.write(f"Usage: {usage}\n")
        return 0
    is_accepted = accepted(arguments) if callable(accepted) else arguments in accepted
    if not is_accepted:
        sys.stderr.write(f"Usage: {usage}\n")
        return 2
    return run(arguments)
