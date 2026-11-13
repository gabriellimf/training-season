"""Named fakes for the weekly cycle: Garmin factory, adapters and a command runner."""

from __future__ import annotations

import os
import signal
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from garmin_auth_fakes import _chain_rejected_token_error
from garmin_pull_fakes import FakeGarminData, PullFakeGarmin
from garmin_push_fakes import (
    FakeGarminCalendar,
    PushFakeGarmin,
    week_text,
    workout_rows,
)
from llm_fakes import QueuedReplyAdapter
from schema_rows import _insert_macro
from training_season.database import connect, initialize_database
from training_season.llm import LLMJob, LLMUnavailableError
from training_season.weekly_cycle_cli import main

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence
    from types import FrameType

    from garminconnect import Garmin

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig
    from training_season.llm import ChatTurn, LLMAdapter, LLMReply

WEDNESDAY_NOON = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
WEEK_REPLY = week_text(["2026-11-17", "2026-11-19"], week_start="2026-11-16")
SUMMARY_REPLY = "Resumo reescrito."


@dataclass
class CycleGarminFactory:
    """
    The first Garmin client pulls, every later one pushes; records both.

    `mfa_at_push` makes Garmin reject the saved token at the push session and
    ask for MFA at the credential login after it.
    """

    server: FakeGarminServer
    pull_data: FakeGarminData = field(default_factory=FakeGarminData)
    calendar: FakeGarminCalendar = field(default_factory=FakeGarminCalendar)
    pull_clients: list[PullFakeGarmin] = field(default_factory=list)
    push_clients: list[PushFakeGarmin] = field(default_factory=list)
    mfa_at_push: bool = False

    def __call__(self, email: str | None, password: str | None) -> Garmin:
        if not self.pull_clients:
            pull = PullFakeGarmin(self.server, email, password, self.pull_data)
            self.pull_clients.append(pull)
            return pull
        if self.mfa_at_push and not self.push_clients:
            self.server.token_login_errors.append(_chain_rejected_token_error())
            self.server.credential_outcomes.append("mfa")
        push = PushFakeGarmin(self.server, email, password, self.calendar)
        self.push_clients.append(push)
        return push


@dataclass
class ChatFailingAdapter(QueuedReplyAdapter):
    """Answers plan requests from the queue; every chat request is unreachable."""

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        if job is LLMJob.CHAT:
            raise LLMUnavailableError("fake provider is unreachable")
        return super().complete(job, system=system, turns=turns)


@dataclass
class CrashingAdapter(QueuedReplyAdapter):
    """Raises an error no cycle step expects, with `detail` as its text."""

    detail: str = "secret-value"

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        raise RuntimeError(self.detail)


@dataclass
class TerminatingAdapter(QueuedReplyAdapter):
    """Sends a stop signal to this process at the plan request, as Ctrl-C does."""

    stop_signal: signal.Signals = signal.SIGTERM

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        os.kill(os.getpid(), self.stop_signal)
        return super().complete(job, system=system, turns=turns)


@dataclass
class TerminatedAtCloseAdapter(QueuedReplyAdapter):
    """Answers from the queue, then sends SIGTERM to this process when it is closed."""

    def close(self) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        super().close()


@dataclass
class WriteLockHolder:
    """
    A second connection that holds the SQLite write lock for 0.4 s from a thread.

    0.1 s after it takes the lock it sends SIGTERM to the main thread, which is then
    waiting in `BEGIN IMMEDIATE`, as `docker stop` during a busy wait does.
    """

    database_path: Path
    held: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self) -> None:
        main_thread = threading.main_thread().ident
        self.thread = threading.Thread(target=self._hold, args=(main_thread,))
        self.thread.start()
        self.held.wait()

    def _hold(self, main_thread: int) -> None:
        with closing(connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.held.set()
            time.sleep(0.1)
            signal.pthread_kill(main_thread, signal.SIGTERM)
            time.sleep(0.3)
            connection.execute("ROLLBACK")


@dataclass
class LockWaitTerminatingAdapter(QueuedReplyAdapter):
    """At the plan request, starts the lock holder before it answers from the queue."""

    holder: WriteLockHolder = field(kw_only=True)

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        if self.holder.thread is None:
            self.holder.start()
        return super().complete(job, system=system, turns=turns)


class TerminatingOutput(StringIO):
    """Stdout that sends SIGTERM to this process at its first write."""

    def write(self, text: str) -> int:
        if not self.getvalue():
            os.kill(os.getpid(), signal.SIGTERM)
        return super().write(text)


@dataclass(frozen=True)
class CycleRun:
    """The exit code and stdout of one command run, and the fakes it used."""

    code: int
    out: str
    adapter: LLMAdapter
    garmin: CycleGarminFactory


@dataclass
class WeeklyCli:
    """Runs `main` with a fake model, a fake Garmin and a fixed Wednesday clock."""

    environ: dict[str, str]
    garmin: CycleGarminFactory
    replies: list[str] = field(default_factory=lambda: [WEEK_REPLY, SUMMARY_REPLY])
    adapter: LLMAdapter | None = None
    output: Callable[[], StringIO] = StringIO

    def run(self, argv: Sequence[str] = ()) -> CycleRun:
        adapter = self.adapter or QueuedReplyAdapter(list(self.replies))
        out = self.output()
        code = main(
            argv,
            environ=self.environ,
            adapter_factory=lambda _environ: adapter,
            garmin_factory=self.garmin,
            clock=lambda: WEDNESDAY_NOON,
            out=out,
        )
        return CycleRun(code, out.getvalue(), adapter, self.garmin)

    def database(self) -> closing[sqlite3.Connection]:
        initialize_database(self.environ)
        return closing(connect(Path(self.environ["DATABASE_PATH"])))

    def messages(self) -> list[tuple[str, str, int | None]]:
        with self.database() as connection:
            rows = connection.execute(
                "SELECT role, content, weekly_plan_id FROM message ORDER BY id"
            )
            return rows.fetchall()

    def message_ids(self) -> list[int]:
        with self.database() as connection:
            rows = connection.execute("SELECT id FROM message ORDER BY id")
            return [int(row[0]) for row in rows]

    def cycle_runs(self) -> list[tuple[str, str | None, int | None, int | None]]:
        """Return trigger, outcome, exit code and message id of each run, in order."""
        with self.database() as connection:
            rows = connection.execute(
                "SELECT trigger, outcome, exit_code, message_id FROM cycle_run "
                "ORDER BY id"
            )
            return rows.fetchall()

    def summary_count(self) -> int:
        with self.database() as connection:
            row = connection.execute("SELECT count(*) FROM coach_summary").fetchone()
            return int(row[0])

    def workout_dates(self) -> list[str]:
        with self.database() as connection:
            return [row[1] for row in workout_rows(connection)]

    def stored_week_id(self) -> int:
        with self.database() as connection:
            row = connection.execute(
                "SELECT id FROM weekly_plan WHERE week_start = '2026-11-16'"
            ).fetchone()
            return int(row[0])


@dataclass
class CycleRunsAtSignal:
    """A signal handler that records the run rows each time the signal arrives."""

    cli: WeeklyCli
    seen: list[list[tuple[str, str | None, int | None, int | None]]] = field(
        default_factory=list
    )

    def __call__(self, _signal_number: int, _frame: FrameType | None) -> None:
        self.seen.append(self.cli.cycle_runs())


def new_weekly_cli(
    database_path: Path, auth_config: GarminAuthConfig, fake_server: FakeGarminServer
) -> WeeklyCli:
    """
    Return a command runner on a fresh database with an active macro plan.

    Used as `cli = new_weekly_cli(database_path, auth_config, fake_server)`
    """
    environ = {
        "DATABASE_PATH": str(database_path),
        "TZ": "America/Sao_Paulo",
        "GARMIN_TOKEN_DIR": str(auth_config.token_dir),
        "GARMIN_EMAIL": "runner@example.test",
        "GARMIN_PASSWORD": "not-a-real-password",
        "GARMIN_HISTORY_START": "2026-11-01",
    }
    weekly = WeeklyCli(environ, CycleGarminFactory(fake_server))
    with weekly.database() as connection:
        _insert_macro(connection, start_date="2026-11-09", race_date="2027-04-18")
    return weekly
