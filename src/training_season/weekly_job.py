"""
Guard around one weekly cycle run: run lock, run record, crash and stop signals.

Every run that takes the database ends with a chat message, also when the cycle
could not write one: a refused run, a crash, a SIGTERM or SIGINT, a run a hard kill
left open. The exception is a message that cannot be stored: the run then ends
`failed` with no message.
"""

from __future__ import annotations

import fcntl
import os
import signal
import sqlite3
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from training_season.cycle_run_store import (
    CycleRunEnd,
    CycleRunNotice,
    finish_cycle_run,
    finish_cycle_run_with_notice,
    open_cycle_runs,
    record_ended_cycle_run,
    start_cycle_run,
)
from training_season.garmin_auth_base import no_follow_flags
from training_season.weekly_job_text import (
    CRASH_TEXT,
    LOCK_FAILED_TEXT,
    REFUSED_TEXT,
    interrupted_text,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from types import FrameType
    from typing import TextIO
    from zoneinfo import ZoneInfo

    from training_season.cycle_run_store import CycleTrigger, OpenCycleRun

    type _SignalHandler = (
        Callable[[int, FrameType | None], object] | int | signal.Handlers | None
    )

RUN_LOCK_FILENAME = "weekly.lock"
REFUSED_EXIT_CODE = 4
_FAILED_EXIT_CODE = 1
_STOP_SIGNALS = frozenset({signal.SIGTERM, signal.SIGINT})


class CycleInterruptedError(BaseException):
    """A stop signal arrived during the cycle. Not an Exception, so no step takes it."""


@dataclass(frozen=True, slots=True)
class WeeklyJob:
    """
    One run of the weekly command.

    `body` runs the cycle and returns how it ended, with the chat message still to
    be stored. SIGTERM and SIGINT stop it only inside `stop_signals_interrupt()`.
    """

    trigger: CycleTrigger
    zone: ZoneInfo | None
    out: TextIO
    body: Callable[[], CycleRunNotice]


def run_weekly_job(
    connection: sqlite3.Connection, database_path: Path, job: WeeklyJob
) -> int:
    """
    Run the job under the run lock beside the database and record how it ended.

    SIGTERM and SIGINT wait while the guard writes. A signal still pending at the
    end goes to the handler the process had before, after the run row is closed.

    Returns the exit code. Used as `run_weekly_job(connection, path, job)`
    """
    with _stop_signals_held():
        return _run_under_lock(connection, database_path, job)


def close_runs_left_open(
    connection: sqlite3.Connection,
    database_path: Path,
    zone: ZoneInfo | None,
    out: TextIO,
) -> None:
    """
    Close every open run row as interrupted, with its chat message.

    Writes no run row of its own. When another run holds the run lock, the rows stay
    for that run. Raises OSError when the lock file cannot be used.

    Used as `close_runs_left_open(connection, path, zone, sys.stdout)`
    """
    lock_path = database_path.parent / RUN_LOCK_FILENAME
    with _stop_signals_held():
        descriptor = _take_run_lock(lock_path)
        if descriptor is None:
            sys.stderr.write(f"weekly run lock {lock_path} is held, open runs stay\n")
            return
        try:
            _close_open_runs(connection, zone, out)
        finally:
            os.close(descriptor)


@contextmanager
def stop_signals_interrupt() -> Iterator[None]:
    """
    Let SIGTERM and SIGINT stop the code inside with `CycleInterruptedError`.

    Use it only inside the body of `run_weekly_job`, which restores the handlers.
    Used as `with stop_signals_interrupt(): outcome = run_weekly_cycle(...)`
    """
    for number in _STOP_SIGNALS:
        signal.signal(number, _raise_interrupted)
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _STOP_SIGNALS)
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)


def _run_under_lock(
    connection: sqlite3.Connection, database_path: Path, job: WeeklyJob
) -> int:
    lock_path = database_path.parent / RUN_LOCK_FILENAME
    try:
        descriptor = _take_run_lock(lock_path)
    except OSError as error:
        sys.stderr.write(f"weekly run lock {lock_path} cannot be used: {error}\n")
        notice = CycleRunNotice("failed", _FAILED_EXIT_CODE, LOCK_FAILED_TEXT)
        return _record_ended(connection, job, notice)
    if descriptor is None:
        notice = CycleRunNotice("refused", REFUSED_EXIT_CODE, REFUSED_TEXT)
        return _record_ended(connection, job, notice)
    try:
        return _locked_run(connection, job)
    finally:
        os.close(descriptor)


def _take_run_lock(lock_path: Path) -> int | None:
    descriptor = os.open(lock_path, no_follow_flags(os.O_CREAT | os.O_RDWR), 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _show(out: TextIO, text: str) -> None:
    out.write(f"{text}\n")


def _record_ended(
    connection: sqlite3.Connection, job: WeeklyJob, notice: CycleRunNotice
) -> int:
    _show(job.out, notice.content)
    record_ended_cycle_run(connection, job.trigger, notice)
    return notice.exit_code


def _interrupted_notice(run: OpenCycleRun, zone: ZoneInfo | None) -> CycleRunNotice:
    text = interrupted_text(run.started_at, zone)
    return CycleRunNotice("interrupted", _FAILED_EXIT_CODE, text)


def _close_open_runs(
    connection: sqlite3.Connection, zone: ZoneInfo | None, out: TextIO
) -> None:
    for open_run in open_cycle_runs(connection):
        notice = _interrupted_notice(open_run, zone)
        _show(out, notice.content)
        finish_cycle_run_with_notice(connection, open_run.run_id, notice)


def _locked_run(connection: sqlite3.Connection, job: WeeklyJob) -> int:
    _close_open_runs(connection, job.zone, job.out)
    run = start_cycle_run(connection, job.trigger)
    notice = _guarded_body(job, run)
    _show(job.out, notice.content)
    return _finish_run(connection, run, notice)


def _finish_run(
    connection: sqlite3.Connection, run: OpenCycleRun, notice: CycleRunNotice
) -> int:
    try:
        finish_cycle_run_with_notice(connection, run.run_id, notice)
    except sqlite3.Error as error:
        sys.stderr.write(f"weekly cycle chat message was not stored: {error}\n")
        end = CycleRunEnd("failed", _FAILED_EXIT_CODE, None)
        finish_cycle_run(connection, run.run_id, end)
        return _FAILED_EXIT_CODE
    return notice.exit_code


def _guarded_body(job: WeeklyJob, run: OpenCycleRun) -> CycleRunNotice:
    try:
        return job.body()
    except CycleInterruptedError:
        return _interrupted_notice(run, job.zone)
    except Exception as error:  # noqa: BLE001 - an unexpected error must still reach the chat
        _log_crash(error)
        return CycleRunNotice("crashed", _FAILED_EXIT_CODE, CRASH_TEXT)


def _log_crash(error: Exception) -> None:
    # The error text can hold model or Garmin values: log only type and frames.
    frames = "".join(traceback.format_tb(error.__traceback__))
    sys.stderr.write(f"weekly cycle crashed with {type(error).__name__}\n{frames}")


@contextmanager
def _stop_signals_held() -> Iterator[None]:
    # Python runs a handler between any two bytecodes. Outside the cycle a stop signal
    # would cut a guard write: an open row, or a second message for one run.
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    previous = {number: signal.getsignal(number) for number in _STOP_SIGNALS}
    try:
        yield
    finally:
        _restore_handlers(previous)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _restore_handlers(previous: dict[signal.Signals, _SignalHandler]) -> None:
    for number, handler in previous.items():
        signal.signal(number, signal.SIG_DFL if handler is None else handler)


def _raise_interrupted(signal_number: int, _frame: FrameType | None) -> NoReturn:
    # Hold every later stop signal, so a second one cannot cut the guard's writes.
    signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    raise CycleInterruptedError(f"signal {signal_number} stopped the weekly cycle run")
