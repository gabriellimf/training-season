"""Tests for the weekly job guard: run record, run lock, crash, SIGTERM, open runs."""

from __future__ import annotations

import fcntl
import os
import signal
import stat
from typing import TYPE_CHECKING

import pytest

from garmin_auth_fakes import _write_fake_token
from llm_fakes import UnavailableAdapter
from weekly_cycle_fakes import (
    SUMMARY_REPLY,
    WEEK_REPLY,
    CrashingAdapter,
    CycleGarminFactory,
    CycleRunsAtSignal,
    LockWaitTerminatingAdapter,
    TerminatedAtCloseAdapter,
    TerminatingAdapter,
    TerminatingOutput,
    WeeklyCli,
    WriteLockHolder,
    new_weekly_cli,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig

_RETRY_HINT = "Rode training-season-weekly para tentar de novo."
_CRASH_TEXT = (
    "Ciclo semanal parou por um erro inesperado.\n"
    f"As etapas seguintes não rodaram.\n{_RETRY_HINT}"
)
_REFUSED_TEXT = "Ciclo semanal não rodou: outra execução já estava em andamento."


@pytest.fixture
def cli(
    database_path: Path, auth_config: GarminAuthConfig, fake_server: FakeGarminServer
) -> WeeklyCli:
    return new_weekly_cli(database_path, auth_config, fake_server)


@pytest.fixture
def signed_in(auth_config: GarminAuthConfig) -> None:
    _write_fake_token(auth_config)


@pytest.fixture
def other_run_holds_the_lock(database_path: Path) -> Iterator[None]:
    descriptor = os.open(database_path.parent / "weekly.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        yield
    finally:
        os.close(descriptor)


@pytest.fixture
def sigterm_seen(cli: WeeklyCli) -> Iterator[CycleRunsAtSignal]:
    recorder = CycleRunsAtSignal(cli)
    previous = signal.signal(signal.SIGTERM, recorder)
    try:
        yield recorder
    finally:
        signal.signal(signal.SIGTERM, previous)


def _stop_handlers() -> tuple[object, object]:
    return signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)


def _insert_open_run(cli: WeeklyCli, started_at: str) -> None:
    with cli.database() as connection:
        connection.execute(
            "INSERT INTO cycle_run (trigger, started_at) VALUES ('scheduled', ?)",
            (started_at,),
        )


def _interrupted_text(start: str) -> str:
    return (
        f"A execução do ciclo semanal iniciada em {start} foi interrompida "
        f"antes do fim. {_RETRY_HINT}"
    )


@pytest.mark.usefixtures("signed_in")
def test_full_run_records_a_done_manual_run_with_its_message(cli: WeeklyCli) -> None:
    run = cli.run()

    assert run.code == 0
    assert cli.cycle_runs() == [("manual", "done", 0, cli.message_ids()[0])]


@pytest.mark.usefixtures("signed_in")
def test_scheduled_flag_records_the_scheduled_trigger(cli: WeeklyCli) -> None:
    run = cli.run(["--scheduled"])

    assert run.code == 0
    assert cli.cycle_runs() == [("scheduled", "done", 0, cli.message_ids()[0])]


@pytest.mark.usefixtures("signed_in")
def test_failed_step_records_a_failed_run_with_the_cycle_message(
    cli: WeeklyCli,
) -> None:
    cli.adapter = UnavailableAdapter()

    run = cli.run()

    assert run.code == 1
    [(_, content, _)] = cli.messages()
    assert content.startswith("Ciclo semanal parou na etapa 3")
    assert cli.cycle_runs() == [("manual", "failed", 1, cli.message_ids()[0])]


def test_pre_step_failure_records_a_failed_run_with_its_message(
    cli: WeeklyCli,
) -> None:
    cli.environ["TZ"] = "Not/AZone"

    run = cli.run(["--scheduled"])

    assert run.code == 2
    assert cli.cycle_runs() == [("scheduled", "failed", 2, cli.message_ids()[0])]


@pytest.mark.usefixtures("signed_in")
def test_open_run_left_by_a_killed_process_is_closed_as_interrupted(
    cli: WeeklyCli,
) -> None:
    _insert_open_run(cli, "2026-11-08T20:00:00Z")

    run = cli.run()

    assert run.code == 0
    first_id, second_id = cli.message_ids()
    assert cli.cycle_runs() == [
        ("scheduled", "interrupted", 1, first_id),
        ("manual", "done", 0, second_id),
    ]
    interrupted, _ = cli.messages()
    assert interrupted == ("assistant", _interrupted_text("08/11 às 17:00"), None)
    assert run.out.startswith(f"{interrupted[1]}\n")


def test_open_run_start_is_shown_in_utc_when_tz_is_not_valid(cli: WeeklyCli) -> None:
    _insert_open_run(cli, "2026-11-08T20:00:00Z")
    cli.environ["TZ"] = "Not/AZone"

    run = cli.run()

    assert run.code == 2
    [(_, interrupted, _), _] = cli.messages()
    assert interrupted == _interrupted_text("08/11 às 20:00 UTC")


@pytest.mark.usefixtures("signed_in", "other_run_holds_the_lock")
def test_run_is_refused_while_another_run_holds_the_lock(cli: WeeklyCli) -> None:
    run = cli.run(["--scheduled"])

    assert run.code == 4
    assert run.garmin.pull_clients == []
    assert cli.messages() == [("assistant", _REFUSED_TEXT, None)]
    assert cli.cycle_runs() == [("scheduled", "refused", 4, cli.message_ids()[0])]
    assert run.out == f"{_REFUSED_TEXT}\n"
    with cli.database() as connection:
        row = connection.execute("SELECT started_at = ended_at FROM cycle_run")
        assert row.fetchone() == (1,)


@pytest.mark.usefixtures("signed_in", "other_run_holds_the_lock")
def test_refused_run_leaves_open_runs_to_the_lock_holder(cli: WeeklyCli) -> None:
    _insert_open_run(cli, "2026-11-08T20:00:00Z")

    cli.run()

    assert cli.cycle_runs()[0] == ("scheduled", None, None, None)


@pytest.mark.usefixtures("signed_in")
def test_lock_file_that_is_a_symlink_is_not_followed(
    cli: WeeklyCli, database_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = database_path.parent / "elsewhere"
    (database_path.parent / "weekly.lock").symlink_to(target)

    run = cli.run()

    assert run.code == 1
    assert not target.exists()
    assert run.garmin.pull_clients == []
    assert cli.messages() == [
        (
            "assistant",
            "Ciclo semanal não rodou: a trava do ciclo semanal não pode ser usada.",
            None,
        )
    ]
    assert cli.cycle_runs() == [("manual", "failed", 1, cli.message_ids()[0])]
    assert "weekly.lock" in capsys.readouterr().err


@pytest.mark.usefixtures("signed_in")
@pytest.mark.parametrize(
    "stop_signal", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"]
)
def test_stop_signal_during_the_cycle_records_an_interrupted_run(
    cli: WeeklyCli, stop_signal: signal.Signals
) -> None:
    handlers_before = _stop_handlers()
    cli.adapter = TerminatingAdapter([], stop_signal=stop_signal)

    run = cli.run()

    assert run.code == 1
    assert _stop_handlers() == handlers_before
    assert cli.workout_dates() == []
    [(_, content, _)] = cli.messages()
    assert content.startswith("A execução do ciclo semanal iniciada em ")
    assert content.endswith(f"foi interrompida antes do fim. {_RETRY_HINT}")
    assert cli.cycle_runs() == [("manual", "interrupted", 1, cli.message_ids()[0])]


@pytest.mark.usefixtures("signed_in")
def test_unexpected_error_records_a_crashed_run_without_its_text(
    cli: WeeklyCli, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = CrashingAdapter([])
    cli.adapter = adapter

    run = cli.run()

    assert run.code == 1
    assert cli.messages() == [("assistant", _CRASH_TEXT, None)]
    assert cli.cycle_runs() == [("manual", "crashed", 1, cli.message_ids()[0])]
    assert run.out == f"{_CRASH_TEXT}\n"
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert adapter.detail not in err
    assert adapter.detail not in run.out


@pytest.mark.usefixtures("signed_in")
def test_next_run_after_a_crash_is_not_blocked(cli: WeeklyCli) -> None:
    cli.adapter = CrashingAdapter([])
    cli.run()
    cli.adapter = None
    cli.garmin = CycleGarminFactory(cli.garmin.server)

    run = cli.run()

    assert run.code == 0
    assert [row[1] for row in cli.cycle_runs()] == ["crashed", "done"]


@pytest.mark.usefixtures("signed_in")
def test_sigterm_after_the_cycle_reaches_the_old_handler_after_the_run_record(
    cli: WeeklyCli, sigterm_seen: CycleRunsAtSignal
) -> None:
    cli.adapter = TerminatedAtCloseAdapter([WEEK_REPLY, SUMMARY_REPLY])

    run = cli.run()

    assert run.code == 0
    [(_, content, _)] = cli.messages()
    assert content.startswith("Treinos da semana de 16/11 a 22/11:")
    done = [("manual", "done", 0, cli.message_ids()[0])]
    assert cli.cycle_runs() == done
    assert sigterm_seen.seen == [done]


@pytest.mark.usefixtures("signed_in")
def test_sigterm_before_the_cycle_is_held_until_the_cycle_starts(
    cli: WeeklyCli, sigterm_seen: CycleRunsAtSignal
) -> None:
    _insert_open_run(cli, "2026-11-08T20:00:00Z")
    cli.output = TerminatingOutput

    run = cli.run()

    assert run.code == 1
    assert run.garmin.pull_clients == []
    assert sigterm_seen.seen == []
    first_id, second_id = cli.message_ids()
    assert cli.cycle_runs() == [
        ("scheduled", "interrupted", 1, first_id),
        ("manual", "interrupted", 1, second_id),
    ]


@pytest.mark.usefixtures("signed_in")
def test_sigterm_while_waiting_for_the_write_lock_records_an_interrupted_run(
    cli: WeeklyCli, database_path: Path
) -> None:
    holder = WriteLockHolder(database_path)
    cli.adapter = LockWaitTerminatingAdapter([WEEK_REPLY, SUMMARY_REPLY], holder=holder)

    run = cli.run()

    assert holder.thread is not None
    holder.thread.join()
    assert run.code == 1
    [(_, content, _)] = cli.messages()
    assert content.startswith("A execução do ciclo semanal iniciada em ")
    assert cli.cycle_runs() == [("manual", "interrupted", 1, cli.message_ids()[0])]


@pytest.mark.usefixtures("signed_in")
@pytest.mark.parametrize("mode_before", [None, 0o644])
def test_run_lock_file_is_readable_only_by_its_owner(
    cli: WeeklyCli, database_path: Path, mode_before: int | None
) -> None:
    lock_path = database_path.parent / "weekly.lock"
    if mode_before is not None:
        lock_path.touch()
        lock_path.chmod(mode_before)

    cli.run()

    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_close_open_runs_closes_each_open_run_with_its_message(cli: WeeklyCli) -> None:
    _insert_open_run(cli, "2026-11-08T20:00:00Z")
    _insert_open_run(cli, "2026-11-09T20:00:00Z")

    run = cli.run(["--close-open-runs"])

    assert run.code == 0
    assert run.garmin.pull_clients == []
    first = _interrupted_text("08/11 às 17:00")
    second = _interrupted_text("09/11 às 17:00")
    assert cli.messages() == [("assistant", first, None), ("assistant", second, None)]
    first_id, second_id = cli.message_ids()
    assert cli.cycle_runs() == [
        ("scheduled", "interrupted", 1, first_id),
        ("scheduled", "interrupted", 1, second_id),
    ]
    assert run.out == f"{first}\n{second}\n"


@pytest.mark.usefixtures("other_run_holds_the_lock")
def test_close_open_runs_leaves_them_to_the_lock_holder(
    cli: WeeklyCli, capsys: pytest.CaptureFixture[str]
) -> None:
    _insert_open_run(cli, "2026-11-08T20:00:00Z")

    run = cli.run(["--close-open-runs"])

    assert run.code == 0
    assert cli.cycle_runs() == [("scheduled", None, None, None)]
    assert cli.messages() == []
    assert "weekly.lock" in capsys.readouterr().err


def test_close_open_runs_without_open_runs_stores_nothing(cli: WeeklyCli) -> None:
    run = cli.run(["--close-open-runs"])

    assert (run.code, run.out) == (0, "")
    assert cli.cycle_runs() == []
    assert cli.messages() == []


def test_close_open_runs_with_a_bad_database_path_exits_one(
    cli: WeeklyCli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.environ["DATABASE_PATH"] = "relative.db"

    run = cli.run(["--close-open-runs"])

    assert run.code == 1
    assert "DATABASE_PATH" in capsys.readouterr().err
