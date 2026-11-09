"""Usage line and argument check shared by the command-line entry points."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

_HELP_ARGUMENTS = (("--help",), ("-h",))


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
