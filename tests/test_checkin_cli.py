"""Tests for the check-in command: the fixed questions, then the coach chat."""

from __future__ import annotations

import logging
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import entry_points
from io import StringIO
from typing import TYPE_CHECKING

import pytest

from console_fakes import ScriptedConsole
from llm_fakes import RequestCheckingAdapter, UnavailableAdapter
from training_season.checkin_cli import main
from training_season.database import connect
from training_season.onboarding_cli import default_adapter_factory

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from training_season.onboarding_cli import AdapterFactory

_SUNDAY_NIGHT_IN_SAO_PAULO = datetime(2026, 11, 23, 1, 30, tzinfo=UTC)
_PAIN_ANSWERS = ["7", "s", "panturrilha", "3"]
_NO_PAIN_ANSWERS = ["4", "n"]
_STORED_ANSWERS = (
    "Último check-in: semana 2026-11-16, fadiga 7/10, "
    "dor: sim, local panturrilha, nível 3/10."
)
_USAGE = "Usage: training-season-checkin\n"

type CheckinRow = tuple[str, int, int, str | None, int | None, str | None]


@dataclass(frozen=True)
class CliRun:
    code: int
    out: str
    console: ScriptedConsole


@dataclass
class CheckinCli:
    """Runs `main` with a fixed clock, a scripted console and a given adapter."""

    environ: dict[str, str]
    database_path: Path

    def run(
        self,
        lines: Sequence[str],
        adapter_factory: AdapterFactory,
        argv: Sequence[str] = (),
    ) -> CliRun:
        console = ScriptedConsole(list(lines))
        out = StringIO()
        code = main(
            argv,
            environ=self.environ,
            adapter_factory=adapter_factory,
            clock=lambda: _SUNDAY_NIGHT_IN_SAO_PAULO,
            read_line=console.read_line,
            out=out,
        )
        return CliRun(code, out.getvalue(), console)

    def run_with(
        self, adapter: RequestCheckingAdapter | UnavailableAdapter, lines: Sequence[str]
    ) -> CliRun:
        return self.run(lines, lambda _environ: adapter)

    def checkin_rows(self) -> list[CheckinRow]:
        if not self.database_path.exists():
            return []
        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                "SELECT week_start, fatigue, has_pain, pain_location, pain_level, "
                "free_text FROM checkin ORDER BY id"
            )
            return list(rows.fetchall())


@pytest.fixture
def cli(database_path: Path) -> CheckinCli:
    environ = {"DATABASE_PATH": str(database_path), "TZ": "America/Sao_Paulo"}
    return CheckinCli(environ, database_path)


def test_full_run_stores_answers_and_messages_and_prints_the_replies(
    cli: CheckinCli,
) -> None:
    adapter = RequestCheckingAdapter(["Entendi.", "Descanse amanhã."])
    lines = [*_PAIN_ANSWERS, "Dormi mal", "", "Treino puxado", "/sair"]

    run = cli.run_with(adapter, lines)

    assert run.code == 0
    assert run.out.startswith("Treinador: Vamos fazer o check-in")
    assert "/sair" in run.out.splitlines()[0]
    assert "Treinador: Check-in salvo.\n" in run.out
    assert run.out.endswith("Treinador: Entendi.\nTreinador: Descanse amanhã.\n")
    assert [request.job.value for request in adapter.requests] == ["chat", "chat"]
    assert adapter.closed
    assert cli.checkin_rows() == [
        ("2026-11-16", 7, 1, "panturrilha", 3, "Dormi mal\nTreino puxado")
    ]


def test_first_request_carries_the_kickoff_with_the_context_and_answers(
    cli: CheckinCli,
) -> None:
    adapter = RequestCheckingAdapter(["Entendi.", "Descanse."])

    cli.run_with(adapter, [*_PAIN_ANSWERS, "Dormi mal", "Treino puxado"])

    first, second = adapter.requests
    assert len(first.turns) == 1
    kickoff = first.turns[0].text
    assert kickoff.startswith("Início do check-in:")
    assert "\n\nContexto do atleta:\n## Plano macro\n" in kickoff
    assert f"## Check-in\n{_STORED_ANSWERS}\n" in kickoff
    assert "Texto livre" not in kickoff
    assert kickoff.endswith("\n\nMensagem do atleta:\nDormi mal")
    assert "Hoje: 2026-11-22" in first.system
    assert [turn.role for turn in second.turns] == ["user", "assistant", "user"]
    assert second.turns[0] == first.turns[0]
    assert [turn.text for turn in second.turns[1:]] == ["Entendi.", "Treino puxado"]


@pytest.mark.parametrize("ending", [["/sair", "Mais uma"], []], ids=["sair", "eof"])
def test_sair_and_eof_end_the_chat_with_exit_zero(
    cli: CheckinCli, ending: list[str]
) -> None:
    adapter = RequestCheckingAdapter(["Entendi."])

    run = cli.run_with(adapter, [*_NO_PAIN_ANSWERS, "Oi", *ending])

    assert run.code == 0
    assert len(adapter.requests) == 1
    assert cli.checkin_rows() == [("2026-11-16", 4, 0, None, None, "Oi")]


@pytest.mark.parametrize("lines", [["7"], ["7", "/sair"]], ids=["eof", "sair"])
def test_leaving_the_questions_stores_nothing_and_calls_no_model(
    cli: CheckinCli, lines: list[str]
) -> None:
    adapter = RequestCheckingAdapter([])

    run = cli.run_with(adapter, lines)

    assert run.code == 0
    assert adapter.requests == []
    assert adapter.closed
    assert cli.checkin_rows() == []
    assert "Check-in salvo." not in run.out


def test_too_long_message_is_refused_not_stored_and_the_chat_goes_on(
    cli: CheckinCli,
) -> None:
    adapter = RequestCheckingAdapter(["Entendi."])

    run = cli.run_with(adapter, [*_NO_PAIN_ANSWERS, "x" * 4001, "Oi"])

    assert run.code == 0
    assert "4000 caracteres" in run.out
    assert len(adapter.requests) == 1
    assert cli.checkin_rows()[0][5] == "Oi"


def test_too_long_conversation_is_refused_and_exits_two(cli: CheckinCli) -> None:
    adapter = RequestCheckingAdapter(["y" * 100_000])

    run = cli.run_with(adapter, [*_NO_PAIN_ANSWERS, "Oi", "Mais uma", "Outra"])

    assert run.code == 2
    assert "longa demais" in run.out
    assert len(adapter.requests) == 1
    assert len(run.console.prompts) == 4
    assert cli.checkin_rows()[0][5] == "Oi"


def test_model_error_keeps_the_answers_and_the_message_and_exits_one(
    cli: CheckinCli, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    run = cli.run_with(UnavailableAdapter(), [*_PAIN_ANSWERS, "Segredo do atleta"])

    assert run.code == 1
    assert run.out.endswith("Suas respostas e mensagens estão salvas.\n")
    assert cli.checkin_rows() == [
        ("2026-11-16", 7, 1, "panturrilha", 3, "Segredo do atleta")
    ]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "LLMUnavailableError" in logged
    for private in ("Segredo do atleta", "panturrilha", "fake provider"):
        assert private not in logged


def test_reply_control_characters_are_not_printed(cli: CheckinCli) -> None:
    adapter = RequestCheckingAdapter(["Bom\x1b[2J dia\x07\nAté\r"])

    run = cli.run_with(adapter, [*_NO_PAIN_ANSWERS, "Oi"])

    assert "\nTreinador: Bom[2J dia\nAté\n" in run.out
    assert "\x1b" not in run.out
    assert "\x07" not in run.out


def test_empty_reply_after_cleaning_ends_the_chat_with_exit_one(
    cli: CheckinCli, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = RequestCheckingAdapter(["  \x1b ", "Entendi."])

    run = cli.run_with(adapter, [*_NO_PAIN_ANSWERS, "Oi", "Mais uma"])

    assert run.code == 1
    assert run.out.endswith("Suas respostas e mensagens estão salvas.\n")
    assert "Treinador: \n" not in run.out
    assert len(adapter.requests) == 1
    assert cli.checkin_rows() == [("2026-11-16", 4, 0, None, None, "Oi")]
    assert "Check-in reply failed error_type=EmptyReply" in caplog.text


def test_missing_timezone_exits_two_before_any_question(
    cli: CheckinCli, capsys: pytest.CaptureFixture[str]
) -> None:
    del cli.environ["TZ"]
    adapter = RequestCheckingAdapter([])

    run = cli.run_with(adapter, _NO_PAIN_ANSWERS)

    assert run.code == 2
    assert capsys.readouterr().err == "TZ must name an IANA timezone\n"
    assert run.console.prompts == []
    assert not cli.database_path.exists()


def test_bad_llm_provider_exits_two_before_any_question(
    cli: CheckinCli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.environ["LLM_PROVIDER"] = "nope"

    run = cli.run(_NO_PAIN_ANSWERS, default_adapter_factory)

    assert run.code == 2
    assert "LLM_PROVIDER must be openai_compatible or anthropic" in (
        capsys.readouterr().err
    )
    assert run.console.prompts == []
    assert not cli.database_path.exists()


@pytest.mark.parametrize("argv", [["now"], ["--help", "x"]])
def test_arguments_print_the_usage_and_exit_two(
    cli: CheckinCli, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    run = cli.run([], default_adapter_factory, argv)

    assert run.code == 2
    assert capsys.readouterr().err == _USAGE


def test_script_entry_runs_the_checkin_main() -> None:
    (entry,) = entry_points(group="console_scripts", name="training-season-checkin")

    assert entry.load() is main
