"""Tests for the onboarding command line: chat, reopen, week and history."""

from __future__ import annotations

import logging
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from garmin_auth_fakes import _write_fake_token
from garmin_push_fakes import (
    FakeGarminCalendar,
    push_factory_for,
    seed_active_macro,
    week_text,
    workout_rows,
)
from llm_fakes import QueuedReplyAdapter
from onboarding_rows import macro_text, result_text
from training_season.database import connect, initialize_database
from training_season.garmin_pending_mfa import remove_mfa_state
from training_season.macro_store import read_macro_history, read_macro_plan_json
from training_season.onboarding_chat import session_for
from training_season.onboarding_cli import main
from training_season.onboarding_store import (
    append_exchange,
    current_open_session,
    session_messages,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig
    from training_season.macro_store import MacroHistoryEntry

_USAGE = (
    "Usage: training-season-onboarding chat | reopen | week | discard "
    "| history [<id>]\n"
)
_NEAR_UTC_MIDNIGHT = datetime(2026, 11, 11, 1, 30, tzinfo=UTC)
_WEEK_REPLY = week_text(["2026-11-11", "2026-11-14"], week_start="2026-11-10")
_ACTIVE_PLAN = "Já existe um plano ativo. Use `reopen` para mudar o objetivo."


def tagged(visible: str, body: str = "") -> str:
    return f"{visible}\n<onboarding_result>{body or result_text()}</onboarding_result>"


_COMPLETING = [tagged("Plano pronto."), macro_text(), _WEEK_REPLY]


@dataclass
class ScriptedConsole:
    """Answers each prompt with the next line; EOF when the lines run out."""

    lines: list[str]
    prompts: list[str] = field(default_factory=list)

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


@dataclass(frozen=True)
class CliRun:
    code: int
    out: str
    adapter: QueuedReplyAdapter
    console: ScriptedConsole


@dataclass
class OnboardingCli:
    """Runs `main` with a queued adapter, a scripted console and a fake Garmin."""

    environ: dict[str, str]
    fake_server: FakeGarminServer
    calendar: FakeGarminCalendar = field(default_factory=FakeGarminCalendar)

    def run(
        self, argv: list[str], replies: Sequence[str] = (), lines: Sequence[str] = ()
    ) -> CliRun:
        adapter = QueuedReplyAdapter(list(replies))
        console = ScriptedConsole(list(lines))
        out = StringIO()
        factory, _ = push_factory_for(self.fake_server, self.calendar)
        code = main(
            argv,
            environ=self.environ,
            adapter_factory=lambda _environ: adapter,
            garmin_factory=factory,
            clock=lambda: _NEAR_UTC_MIDNIGHT,
            read_line=console.read_line,
            out=out,
        )
        return CliRun(code, out.getvalue(), adapter, console)


@pytest.fixture
def cli(
    database_path: Path, auth_config: GarminAuthConfig, fake_server: FakeGarminServer
) -> OnboardingCli:
    environ = {
        "DATABASE_PATH": str(database_path),
        "TZ": "America/Sao_Paulo",
        "GARMIN_TOKEN_DIR": str(auth_config.token_dir),
        "GARMIN_EMAIL": "runner@example.test",
        "GARMIN_PASSWORD": "not-a-real-password",
    }
    return OnboardingCli(environ, fake_server)


@pytest.fixture
def signed_in(auth_config: GarminAuthConfig) -> None:
    _write_fake_token(auth_config)


def _database(environ: Mapping[str, str]) -> closing[sqlite3.Connection]:
    initialize_database(environ)
    return closing(connect(Path(environ["DATABASE_PATH"])))


def _history(cli: OnboardingCli) -> tuple[MacroHistoryEntry, ...]:
    with _database(cli.environ) as connection:
        return read_macro_history(connection)


def _workout_dates(cli: OnboardingCli) -> list[str]:
    with _database(cli.environ) as connection:
        return [row[1] for row in workout_rows(connection)]


@pytest.mark.usefixtures("signed_in")
def test_chat_completes_onboarding_and_pushes_week_one(cli: OnboardingCli) -> None:
    run = cli.run(["chat"], _COMPLETING, ["Oi"])

    assert run.code == 0
    assert [request.job.value for request in run.adapter.requests] == [
        "chat",
        "plan",
        "plan",
    ]
    assert run.adapter.closed
    assert run.out.startswith("Treinador: Olá!")
    assert "Treinador: Plano pronto.\n" in run.out
    assert "enviada ao Garmin" in run.out
    assert _workout_dates(cli) == ["2026-11-11", "2026-11-14"]
    with _database(cli.environ) as connection:
        week = connection.execute("SELECT week_start, week_end FROM weekly_plan")
        assert week.fetchall() == [("2026-11-10", "2026-11-15")]


def test_chat_with_an_active_macro_and_no_open_session_exits_two(
    cli: OnboardingCli, capsys: pytest.CaptureFixture[str]
) -> None:
    with _database(cli.environ) as connection:
        seed_active_macro(connection)

    run = cli.run(["chat"], [], ["Oi"])

    assert run.code == 2
    assert capsys.readouterr().err == f"{_ACTIVE_PLAN}\n"
    assert run.adapter.requests == []


def test_reopen_with_no_active_macro_exits_two(
    cli: OnboardingCli, capsys: pytest.CaptureFixture[str]
) -> None:
    run = cli.run(["reopen"], [], ["Oi"])

    assert run.code == 2
    assert "Use `chat`" in capsys.readouterr().err


def test_eof_keeps_the_session_open_and_chat_resumes(cli: OnboardingCli) -> None:
    first = cli.run(["chat"], ["Qual é a sua prova?"], ["Oi"])

    second = cli.run(["chat"], ["Quando é?"], ["Uma maratona"])

    assert (first.code, second.code) == (0, 0)
    assert first.out.startswith("Treinador: Olá!")
    assert second.out == "Treinador: Quando é?\n\n"
    roles = [turn.role for turn in second.adapter.requests[0].turns]
    assert roles == ["user", "assistant", "user"]
    with _database(cli.environ) as connection:
        session_id = current_open_session(connection)
        assert session_id is not None
        assert len(session_messages(connection, session_id)) == 4


def test_blank_lines_are_ignored_and_sair_ends_the_loop(cli: OnboardingCli) -> None:
    run = cli.run(["chat"], [], ["", "   ", "/sair", "Oi"])

    assert run.code == 0
    assert run.adapter.requests == []
    assert run.console.prompts == ["Você: "] * 3


def test_athlete_line_loses_control_characters(cli: OnboardingCli) -> None:
    run = cli.run(["chat"], ["Qual é a sua prova?"], ["\x1b\x07", "Oi\x1b[2J"])

    assert run.code == 0
    assert run.console.prompts == ["Você: "] * 3
    assert run.adapter.requests[0].turns[-1].text.endswith("\nOi[2J")


def test_line_over_the_cap_is_refused_and_the_chat_goes_on(
    cli: OnboardingCli,
) -> None:
    run = cli.run(["chat"], ["Qual é a sua prova?"], ["x" * 4001, "Oi"])

    assert run.code == 0
    assert "4000 caracteres" in run.out
    assert len(run.adapter.requests) == 1


def test_too_long_session_exits_two_and_names_discard(cli: OnboardingCli) -> None:
    with _database(cli.environ) as connection:
        session_id = session_for(connection, reopening=False)
        for _ in range(30):
            append_exchange(connection, session_id, "x" * 4000, "y" * 4000)

    run = cli.run(["chat"], [], ["Oi", "Oi"])

    assert run.code == 2
    assert "training-season-onboarding discard" in run.out
    assert run.adapter.requests == []
    assert run.console.prompts == ["Você: "]


def test_discard_then_chat_starts_a_new_session(cli: OnboardingCli) -> None:
    cli.run(["chat"], ["Qual é a sua prova?"], ["Oi"])

    discard = cli.run(["discard"])
    chat = cli.run(["chat"], ["Qual é a sua prova?"], ["Recomeçando"])

    assert discard.code == 0
    assert discard.out == (
        "Sessão de onboarding 1 descartada. As mensagens ficam salvas. "
        "Use `chat` ou `reopen` para começar de novo.\n"
    )
    assert chat.out.startswith("Treinador: Olá!")
    assert len(chat.adapter.requests[0].turns) == 1
    with _database(cli.environ) as connection:
        assert current_open_session(connection) == 2
        assert len(session_messages(connection, 1)) == 2


def test_discard_with_no_open_session_exits_two(
    cli: OnboardingCli, capsys: pytest.CaptureFixture[str]
) -> None:
    run = cli.run(["discard"])

    assert run.code == 2
    assert capsys.readouterr().err == "Nenhuma sessão de onboarding aberta.\n"


def test_mfa_exits_three_and_week_pushes_after_the_login(
    cli: OnboardingCli,
    auth_config: GarminAuthConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.fake_server.credential_outcomes.append("mfa")

    chat = cli.run(["chat"], _COMPLETING, ["Oi"])
    assert chat.code == 3
    assert "training-season-onboarding week" in capsys.readouterr().err
    assert [entry.status for entry in _history(cli)] == ["active"]
    assert _workout_dates(cli) == []

    _write_fake_token(auth_config)
    remove_mfa_state(auth_config)
    week = cli.run(["week"], [_WEEK_REPLY])
    assert week.code == 0
    assert _workout_dates(cli) == ["2026-11-11", "2026-11-14"]


@pytest.mark.usefixtures("signed_in")
def test_rejected_week_exits_three_and_pushes_nothing(cli: OnboardingCli) -> None:
    with _database(cli.environ) as connection:
        seed_active_macro(connection)

    run = cli.run(["week"], ["{}", "{}"])

    assert run.code == 3
    assert run.out.startswith("O treinador devolveu uma semana inválida")
    assert _workout_dates(cli) == []


def test_week_with_no_active_macro_exits_two(cli: OnboardingCli) -> None:
    run = cli.run(["week"], [])

    assert run.code == 2
    assert run.adapter.requests == []


@pytest.mark.usefixtures("signed_in")
def test_reopen_archives_the_old_macro_and_history_reads_both(
    cli: OnboardingCli,
) -> None:
    cli.run(["chat"], _COMPLETING, ["Oi"])
    new_goal = tagged("Plano novo.", result_text(reason="Mudei para meia maratona."))

    reopen = cli.run(["reopen"], [new_goal, macro_text(), _WEEK_REPLY], ["Mudei"])
    history = cli.run(["history"])
    old_plan = cli.run(["history", "1"])

    assert reopen.code == 0
    assert reopen.out.startswith("Treinador: Vamos revisar")
    assert [entry.status for entry in _history(cli)] == ["archived", "active"]
    lines = history.out.splitlines()
    assert lines[0].startswith("Plano macro 1: arquivado, 2026-11-10 a 2027-04-18")
    assert "Mudei para meia maratona." in history.out
    assert history.out.count("Revisão 1") == 2
    with _database(cli.environ) as connection:
        assert old_plan.out == f"{read_macro_plan_json(connection, 1)}\n"
    assert _workout_dates(cli) == ["2026-11-11", "2026-11-14"]


def test_history_with_no_macro_says_so(cli: OnboardingCli) -> None:
    run = cli.run(["history"])

    assert (run.code, run.out) == (0, "Nenhum plano macro salvo.\n")


@pytest.mark.parametrize("macro_id", ["0", "x", "-1", "9"])
def test_history_rejects_a_bad_or_unknown_id(
    cli: OnboardingCli, macro_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    run = cli.run(["history", macro_id])

    assert run.code == 2
    assert repr(macro_id) in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["chat", "x"], ["history", "1", "2"], ["show"]])
def test_bad_arguments_print_the_usage(
    cli: OnboardingCli, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run(argv).code == 2
    assert capsys.readouterr().err == _USAGE


@pytest.mark.usefixtures("signed_in")
def test_logs_hold_no_profile_or_message_text(
    cli: OnboardingCli, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    cli.run(["chat"], _COMPLETING, ["Segredo do atleta"])

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for private in ("Segredo do atleta", "Plano pronto", "Gabi", "Canelite"):
        assert private not in logged
