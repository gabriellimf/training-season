"""Tests for the weekly cycle command: eight steps, one chat message per run."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from garmin_auth_fakes import _write_fake_token
from garmin_pull_fakes import _connection_error
from garmin_push_fakes import week_text, workout_rows
from llm_fakes import QueuedReplyAdapter, UnavailableAdapter
from schema_rows import _insert_macro
from training_season.database import connect, initialize_database
from training_season.weekly_cycle import next_week_range
from training_season.weekly_cycle_cli import main
from weekly_cycle_fakes import ChatFailingAdapter, CycleGarminFactory

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig
    from training_season.llm import LLMAdapter

_WEDNESDAY_NOON = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
_WEEK_REPLY = week_text(["2026-11-17", "2026-11-19"], week_start="2026-11-16")
_SUMMARY_REPLY = "Resumo reescrito."
_PUSHED = "Semana de 16/11 enviada ao Garmin. Agendados: 17/11, 19/11."
_NOT_RUN = "As etapas seguintes não rodaram."
_ERROR_DETAIL = "simulated connection failure"


def _interval_in_seconds_reply(workout_index: int) -> str:
    reply = json.loads(_WEEK_REPLY)
    reply["workouts"][workout_index]["workout"]["steps"] = [
        {
            "type": "interval",
            "duration": {"seconds": 600},
            "pace_mps": {"min": 4.0, "max": 4.0},
        }
    ]
    return json.dumps(reply)


@dataclass(frozen=True)
class CycleRun:
    code: int
    out: str
    adapter: LLMAdapter
    garmin: CycleGarminFactory


@dataclass
class WeeklyCli:
    """Runs `main` with a fake model, a fake Garmin and a fixed Wednesday clock."""

    environ: dict[str, str]
    garmin: CycleGarminFactory
    replies: list[str] = field(default_factory=lambda: [_WEEK_REPLY, _SUMMARY_REPLY])
    adapter: LLMAdapter | None = None

    def run(self, argv: Sequence[str] = ()) -> CycleRun:
        adapter = self.adapter or QueuedReplyAdapter(list(self.replies))
        out = StringIO()
        code = main(
            argv,
            environ=self.environ,
            adapter_factory=lambda _environ: adapter,
            garmin_factory=self.garmin,
            clock=lambda: _WEDNESDAY_NOON,
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


@pytest.fixture
def cli(
    database_path: Path, auth_config: GarminAuthConfig, fake_server: FakeGarminServer
) -> WeeklyCli:
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


@pytest.fixture
def signed_in(auth_config: GarminAuthConfig) -> None:
    _write_fake_token(auth_config)


def _jobs(adapter: LLMAdapter) -> list[str]:
    if isinstance(adapter, QueuedReplyAdapter):
        return [request.job.value for request in adapter.requests]
    if isinstance(adapter, UnavailableAdapter):
        return [job.value for job in adapter.jobs]
    raise TypeError(f"adapter is {adapter!r}, expected a named fake adapter")


@pytest.mark.parametrize(
    ("today", "monday"),
    [
        (date(2026, 11, 11), date(2026, 11, 16)),
        (date(2026, 11, 15), date(2026, 11, 16)),
        (date(2026, 11, 16), date(2026, 11, 23)),
    ],
)
def test_next_week_is_the_monday_after_today_to_sunday(
    today: date, monday: date
) -> None:
    assert next_week_range(today) == (monday, date.fromordinal(monday.toordinal() + 6))


@pytest.mark.usefixtures("signed_in")
def test_cycle_runs_all_steps_and_writes_one_week_message(cli: WeeklyCli) -> None:
    run = cli.run()

    assert run.code == 0
    assert _jobs(run.adapter) == ["plan", "chat"]
    assert len(run.garmin.pull_clients) == 1
    assert len(run.garmin.push_clients) == 1
    assert cli.workout_dates() == ["2026-11-17", "2026-11-19"]
    assert cli.summary_count() == 1
    expected = (
        "Treinos da semana de 16/11 a 22/11:\n"
        "- ter 17/11 Leve\n"
        "- qui 19/11 Leve\n"
        "Volume planejado: 2,0 km (+2,0 km contra 0,0 km corridos na semana passada).\n"
        "Leve: 2,0 km · Forte: 0,0 km.\n"
        f"{_PUSHED}"
    )
    assert run.out == f"{expected}\n"
    assert cli.messages() == [("assistant", expected, cli.stored_week_id())]


@pytest.mark.usefixtures("signed_in")
def test_interval_in_seconds_is_pushed_and_counted_as_hard(cli: WeeklyCli) -> None:
    cli.replies = [_interval_in_seconds_reply(1), _SUMMARY_REPLY]

    run = cli.run()

    assert run.code == 0
    assert cli.workout_dates() == ["2026-11-17", "2026-11-19"]
    assert run.out.splitlines()[3:6] == [
        "Volume planejado: 3,4 km (+3,4 km contra 0,0 km corridos na semana passada).",
        "Leve: 1,0 km · Forte: 2,4 km.",
        _PUSHED,
    ]


@pytest.mark.usefixtures("signed_in")
def test_summary_reads_the_same_context_as_the_week(cli: WeeklyCli) -> None:
    run = cli.run()

    assert isinstance(run.adapter, QueuedReplyAdapter)
    plan_request, summary_request = run.adapter.requests
    context_text = summary_request.turns[0].text
    plan_text = plan_request.turns[0].text
    assert plan_text.startswith(f"{context_text}\nPlaneje")
    assert "Planeje os treinos de 2026-11-16 a 2026-11-22." in plan_text


@dataclass(frozen=True)
class StepFailure:
    """One forced failure: how to cause it and what must not happen after it."""

    arrange: Callable[[WeeklyCli], None]
    step_text: str
    code: int
    jobs: list[str]
    push_clients: int
    workouts: list[str] = field(default_factory=list)
    week_stored: bool = False
    cause: str = ""
    shown: str = ""


def _pull_error(cli: WeeklyCli) -> None:
    cli.garmin.pull_data.errors[("get_daily_summaries", 0)] = _connection_error(503)


def _pull_network_down(cli: WeeklyCli) -> None:
    cli.garmin.pull_data.errors[("get_daily_summaries", 0)] = OSError(_ERROR_DETAIL)


def _history_start_after_today(cli: WeeklyCli) -> None:
    cli.environ["GARMIN_HISTORY_START"] = "2026-12-01"


def _context_error(cli: WeeklyCli) -> None:
    with cli.database() as connection:
        connection.execute("DROP TABLE coach_summary")


def _model_down(cli: WeeklyCli) -> None:
    cli.adapter = UnavailableAdapter()


def _invalid_twice(cli: WeeklyCli) -> None:
    cli.replies = ["{}", "{}"]


def _bad_name_twice(cli: WeeklyCli) -> None:
    bad_name = _WEEK_REPLY.replace("17/11 Leve", "Leve")
    cli.replies = [bad_name, bad_name]


def _reply_values_in_errors(cli: WeeklyCli) -> None:
    reply = json.loads(_WEEK_REPLY)
    reply["workouts"][0]["date"] = _ERROR_DETAIL
    reply[f"{_ERROR_DETAIL} key"] = 1
    cli.replies = [json.dumps(reply), json.dumps(reply)]


def _workout_store_fails(cli: WeeklyCli) -> None:
    with cli.database() as connection:
        connection.execute(
            "CREATE TRIGGER workout_store_fails BEFORE INSERT ON workout "
            f"BEGIN SELECT RAISE(ABORT, '{_ERROR_DETAIL}'); END"
        )


def _first_upload_fails(cli: WeeklyCli) -> None:
    cli.garmin.calendar.errors[("upload_running_workout", 0)] = _connection_error(503)


def _interval_week_second_upload_fails(cli: WeeklyCli) -> None:
    cli.replies = [_interval_in_seconds_reply(0), _SUMMARY_REPLY]
    cli.garmin.calendar.errors[("upload_running_workout", 1)] = _connection_error(503)


def _summary_with_heading(cli: WeeklyCli) -> None:
    cli.replies = [_WEEK_REPLY, "# Resumo"]


def _summary_model_down(cli: WeeklyCli) -> None:
    cli.adapter = ChatFailingAdapter([_WEEK_REPLY])


_PULL = "etapa 1 (coleta do Garmin)"
_VALIDATION = "etapa 4 (validação da semana e dos nomes dos treinos)"
_PUSH = "etapa 6 (envio ao Garmin)"
_SUMMARY = "etapa 7 (resumo do atleta)"
_BOTH_DAYS = ["2026-11-17", "2026-11-19"]
_PARTIAL = (
    "Envio da semana de 16/11 incompleto. Agendados: 17/11. Falhou: 19/11, "
    "no envio do treino: o Garmin devolveu erro HTTP 503. Não tentados: nenhum."
)
_REJECTED_WEEK = (
    "O treinador devolveu uma semana inválida duas vezes. "
    "Nada foi enviado ao Garmin. 2 erros de validação."
)
_FAILURES = {
    "pull": StepFailure(_pull_error, _PULL, 1, [], 0),
    "pull_network": StepFailure(
        _pull_network_down, _PULL, 1, [], 0, cause="o Garmin não respondeu"
    ),
    "pull_config": StepFailure(
        _history_start_after_today,
        _PULL,
        2,
        [],
        0,
        cause="a configuração do Garmin não é válida.",
    ),
    "context": StepFailure(_context_error, "etapa 2 (contexto do treinador)", 1, [], 0),
    "model": StepFailure(_model_down, "etapa 3 (geração da semana)", 1, ["plan"], 0),
    "invalid": StepFailure(_invalid_twice, _VALIDATION, 3, ["plan", "plan"], 0),
    "name": StepFailure(_bad_name_twice, _VALIDATION, 3, ["plan", "plan"], 0),
    "reply_values": StepFailure(
        _reply_values_in_errors,
        _VALIDATION,
        3,
        ["plan", "plan"],
        0,
        cause=_REJECTED_WEEK,
    ),
    "push_total": StepFailure(_first_upload_fails, _PUSH, 1, ["plan"], 1, [], True),
    "push_store": StepFailure(
        _workout_store_fails,
        _PUSH,
        1,
        ["plan"],
        1,
        [],
        True,
        "o banco de dados falhou",
    ),
    "push_partial": StepFailure(
        _interval_week_second_upload_fails,
        _PUSH,
        1,
        ["plan"],
        1,
        ["2026-11-17"],
        True,
        shown=(
            "Treinos da semana de 16/11 a 22/11:\n- ter 17/11 Leve\n- qui 19/11 Leve\n"
            "Volume planejado: 3,4 km (+3,4 km contra 0,0 km corridos "
            "na semana passada).\n"
            "Leve: 1,0 km · Forte: 2,4 km.\n"
            f"Ciclo semanal parou na {_PUSH}: {_PARTIAL}\n{_NOT_RUN}"
        ),
    ),
    "summary": StepFailure(
        _summary_with_heading,
        _SUMMARY,
        3,
        ["plan", "chat"],
        1,
        _BOTH_DAYS,
        True,
        shown=f"{_PUSHED}\nCiclo semanal",
    ),
    "summary_model": StepFailure(
        _summary_model_down, _SUMMARY, 1, ["plan"], 1, _BOTH_DAYS, True
    ),
}


@pytest.mark.usefixtures("signed_in")
@pytest.mark.parametrize("failure", _FAILURES.values(), ids=_FAILURES.keys())
def test_failed_step_writes_one_message_and_stops_the_cycle(
    cli: WeeklyCli, failure: StepFailure
) -> None:
    failure.arrange(cli)

    run = cli.run()

    assert run.code == failure.code
    assert _jobs(run.adapter) == failure.jobs
    assert len(run.garmin.push_clients) == failure.push_clients
    assert cli.workout_dates() == failure.workouts
    [(role, content, weekly_plan_id)] = cli.messages()
    assert role == "assistant"
    assert f"Ciclo semanal parou na {failure.step_text}: {failure.cause}" in content
    assert failure.shown in content
    assert content.endswith(_NOT_RUN)
    assert _ERROR_DETAIL not in content
    assert weekly_plan_id == (cli.stored_week_id() if failure.week_stored else None)
    assert run.out == f"{content}\n"


@pytest.mark.usefixtures("signed_in")
@pytest.mark.parametrize("failure", ["summary", "push_partial", "push_total"])
def test_failure_after_the_week_is_planned_starts_with_the_planned_week(
    cli: WeeklyCli, failure: str
) -> None:
    _FAILURES[failure].arrange(cli)

    run = cli.run()

    assert run.out.startswith("Treinos da semana de 16/11 a 22/11:\n- ter 17/11 Leve\n")
    assert cli.summary_count() == 0


def test_mfa_at_the_pull_exits_three_with_the_login_hint(
    cli: WeeklyCli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.garmin.server.credential_outcomes.append("mfa")

    run = cli.run()

    _assert_mfa_stop(cli, run, capsys, _PULL)
    assert _jobs(run.adapter) == []


@pytest.mark.usefixtures("signed_in")
def test_mfa_at_the_push_exits_three_with_the_login_hint(
    cli: WeeklyCli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.garmin.mfa_at_push = True

    run = cli.run()

    _assert_mfa_stop(cli, run, capsys, _PUSH)
    assert _jobs(run.adapter) == ["plan"]
    assert cli.workout_dates() == []


def _assert_mfa_stop(
    cli: WeeklyCli, run: CycleRun, capsys: pytest.CaptureFixture[str], step: str
) -> None:
    assert run.code == 3
    assert "training-season-garmin" in capsys.readouterr().err
    [(_, content, weekly_plan_id)] = cli.messages()
    assert content.startswith(f"Ciclo semanal parou na {step}: o Garmin pede")
    assert weekly_plan_id is None


@pytest.mark.usefixtures("signed_in")
def test_message_write_failure_exits_one_on_stderr(
    cli: WeeklyCli, capsys: pytest.CaptureFixture[str]
) -> None:
    with cli.database() as connection:
        connection.execute("DROP TABLE message")

    run = cli.run()

    assert run.code == 1
    assert "chat message" in capsys.readouterr().err
    assert run.out.startswith("Treinos da semana de 16/11 a 22/11:")


@pytest.mark.usefixtures("signed_in")
@pytest.mark.parametrize(
    ("variable", "value"), [("TZ", "Not/AZone"), ("GARMIN_HISTORY_START", "x")]
)
def test_config_error_exits_two_before_any_call_and_writes_no_message(
    cli: WeeklyCli, variable: str, value: str
) -> None:
    cli.environ[variable] = value

    run = cli.run()

    assert run.code == 2
    assert _jobs(run.adapter) == []
    assert run.garmin.pull_clients == []
    assert cli.messages() == []


@pytest.mark.usefixtures("signed_in")
def test_no_active_macro_exits_two_before_any_call(cli: WeeklyCli) -> None:
    with cli.database() as connection:
        connection.execute("DELETE FROM macro_plan")

    run = cli.run()

    assert run.code == 2
    assert run.garmin.pull_clients == []
    assert cli.messages() == []


def test_arguments_print_the_usage(
    cli: WeeklyCli, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run(["run"]).code == 2
    assert capsys.readouterr().err == "Usage: training-season-weekly [--help]\n"
