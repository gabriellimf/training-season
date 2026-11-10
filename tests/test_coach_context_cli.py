"""Tests for the coach context command line entry point."""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING

from athlete_summary_fakes import SummaryChatTransport
from training_season.coach_context_cli import main
from training_season.database import connect

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_USAGE = "Usage: training-season-context show <YYYY-MM-DD> | summary <YYYY-MM-DD>\n"
_LLM_ENVIRONMENT = {
    "OPENAI_COMPATIBLE_API_KEY": "sk-or-test-not-a-real-key",
    "TZ": "America/Sao_Paulo",
}


def _summary_environment(database_path: Path) -> dict[str, str]:
    return {**_LLM_ENVIRONMENT, "DATABASE_PATH": str(database_path)}


def _run_summary(script: SummaryChatTransport, environ: dict[str, str]) -> int:
    return main(
        ["summary", "2026-11-02"], environ=environ, transport=script.transport()
    )


def _summary_rows(database_path: Path) -> list[str]:
    with closing(connect(database_path)) as connection:
        rows = connection.execute("SELECT summary FROM coach_summary ORDER BY id")
        return [str(row[0]) for row in rows]


def _messages(body: dict[str, object]) -> list[dict[str, str]]:
    messages = body["messages"]
    assert isinstance(messages, list)
    return messages


def test_summary_stores_the_rewrite_and_prints_its_id(
    capsys: pytest.CaptureFixture[str], database_path: Path
) -> None:
    script = SummaryChatTransport(reply_text="Resumo novo.")

    code = _run_summary(script, _summary_environment(database_path))

    assert code == 0
    assert capsys.readouterr().out == "Resumo salvo: id 1\nResumo novo.\n"
    assert _summary_rows(database_path) == ["Resumo novo."]
    system, user = _messages(script.bodies[0])
    assert system["content"].endswith("no máximo 4000 caracteres.")
    assert user["content"].startswith("## Plano macro\nSem plano macro ativo.\n")


def test_rejected_summary_exits_with_three_and_stores_nothing(
    capsys: pytest.CaptureFixture[str], database_path: Path
) -> None:
    script = SummaryChatTransport(reply_text="a" * 4001)

    code = _run_summary(script, _summary_environment(database_path))

    assert code == 3
    assert "4001 characters, expected 1 to 4000" in capsys.readouterr().err
    assert _summary_rows(database_path) == []


def test_summary_provider_failure_exits_with_one(
    capsys: pytest.CaptureFixture[str], database_path: Path
) -> None:
    script = SummaryChatTransport(status_code=401)

    code = _run_summary(script, _summary_environment(database_path))

    assert code == 1
    assert "rejected the API key (HTTP 401)" in capsys.readouterr().err
    assert _summary_rows(database_path) == []


def test_summary_without_llm_key_exits_with_two_before_the_database(
    capsys: pytest.CaptureFixture[str], database_path: Path
) -> None:
    script = SummaryChatTransport()
    environ = {"TZ": "America/Sao_Paulo", "DATABASE_PATH": str(database_path)}

    code = _run_summary(script, environ)

    assert code == 2
    assert "OPENAI_COMPATIBLE_API_KEY" in capsys.readouterr().err
    assert script.bodies == []
    assert not database_path.exists()


def test_show_prints_the_context_and_its_size(
    capsys: pytest.CaptureFixture[str], database_path: Path
) -> None:
    code = main(["show", "2026-11-02"], environ={"DATABASE_PATH": str(database_path)})

    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("## Plano macro\nSem plano macro ativo.\n")
    assert f"Contexto: {len(captured.out)} caracteres\n" in captured.err


def test_show_rejects_a_bad_date_before_opening_the_database(
    capsys: pytest.CaptureFixture[str], database_path: Path
) -> None:
    code = main(["show", "02/11/2026"], environ={"DATABASE_PATH": str(database_path)})

    assert code == 2
    assert "'02/11/2026', expected YYYY-MM-DD" in capsys.readouterr().err
    assert not database_path.exists()


def test_missing_database_path_is_a_configuration_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["show", "2026-11-02"], environ={})

    assert code == 2
    assert "DATABASE_PATH" in capsys.readouterr().err


def test_sqlite_error_exits_with_one(
    capsys: pytest.CaptureFixture[str], database_path: Path
) -> None:
    database_path.write_bytes(b"not a sqlite database file " * 100)

    code = main(["show", "2026-11-02"], environ={"DATABASE_PATH": str(database_path)})

    assert code == 1
    assert "not a database" in capsys.readouterr().err


def test_unknown_command_prints_the_usage_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["list"], environ={}) == 2
    assert capsys.readouterr().err == _USAGE
