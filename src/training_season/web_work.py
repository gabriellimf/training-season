"""In-memory web state of the one server process: the coach lock and the check-in."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from training_season.timeformat import utc_timestamp

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime

    from training_season.checkin_chat import CheckinConversation

type CoachJob = Literal[
    "onboarding_session",
    "onboarding_turn",
    "onboarding_discard",
    "week_one",
    "checkin_save",
    "checkin_reply",
    "checkin_end",
    "garmin_login",
    "garmin_mfa",
]


class CoachBusyError(Exception):
    """Another coach job holds the lock; the web answers 409 busy."""


@dataclass(frozen=True, slots=True)
class CoachJobRun:
    """The job that holds the lock and its UTC start time (`...Z`)."""

    job: CoachJob
    started_at: str


class CoachWork:
    """
    A single-flight lock for the slow model and Garmin jobs, never waited on.

    Used as `with context.work.run("onboarding_turn"): ...`
    """

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._current: CoachJobRun | None = None

    @contextmanager
    def run(self, job: CoachJob) -> Iterator[CoachJobRun]:
        """Hold the lock for one job, or raise `CoachBusyError` at once."""
        if not self._lock.acquire(blocking=False):
            raise CoachBusyError(f"coach job {job} refused, expected no running job")
        try:
            self._current = CoachJobRun(job, utc_timestamp(self._clock()))
            yield self._current
        finally:
            self._current = None
            self._lock.release()

    def current(self) -> CoachJobRun | None:
        """Return the running job, or None when the coach is free."""
        return self._current


class CheckinSlot:
    """
    The one open check-in conversation. A restart ends it.

    Used as `context.checkin.replace(conversation)`
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conversation: CheckinConversation | None = None

    def current(self) -> CheckinConversation | None:
        """Return the open conversation, or None."""
        with self._lock:
            return self._conversation

    def replace(self, conversation: CheckinConversation | None) -> None:
        """Store the new conversation state, or None to end it."""
        with self._lock:
            self._conversation = conversation
