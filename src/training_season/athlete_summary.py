"""The coach rewrites its state-of-the-athlete summary; each rewrite is a new row."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from training_season.coach_context_lines import first_unsafe_control
from training_season.coach_system import active_race_date
from training_season.database import transaction
from training_season.llm import ChatTurn, LLMJob
from training_season.timeformat import utc_now

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from training_season.coach_context import CoachContext
    from training_season.coach_instruction import CoachInstruction
    from training_season.llm import LLMAdapter, LLMReply

MAX_ATHLETE_SUMMARY_CHARS = 4000

ATHLETE_SUMMARY_CONTRACT = f"""\
Formato da resposta

A mensagem do usuário é o contexto do atleta. A seção "Resumo do atleta" traz o
resumo anterior.
Reescreva o resumo inteiro, não só o que mudou. Junte o resumo anterior com o que
o contexto mostra.
Conte o estado atual do atleta: carga recente, resposta ao treino, fadiga, dores,
check-ins e semanas sem check-in, e o que observar na próxima semana.
Natação é só leitura: conte como carga, não planeje natação.
O valor "Semanas até a prova" da seção "Check-in" do contexto é o que vale para a
semana planejada.
Responda só com o texto do resumo, sem JSON, sem títulos e sem texto antes ou depois.
Use no máximo {MAX_ATHLETE_SUMMARY_CHARS} caracteres."""

_LOGGER = logging.getLogger(__name__)


class AthleteSummaryRejectedError(ValueError):
    """The rewrite breaks the summary contract; nothing was stored."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"athlete summary {reason}; the previous summary stays")


@dataclass(frozen=True, slots=True)
class StoredSummary:
    """The new `coach_summary` row."""

    summary_id: int
    text: str = field(repr=False)


def athlete_summary_system_text(instruction_text: str) -> str:
    """
    Append the summary contract to the rendered coach instruction.

    Used as `athlete_summary_system_text(instruction.render(...))`
    """
    return f"{instruction_text}\n\n{ATHLETE_SUMMARY_CONTRACT}"


def athlete_summary_system(
    connection: sqlite3.Connection,
    instruction: CoachInstruction,
    *,
    now: datetime,
    zone: ZoneInfo,
) -> str:
    """
    Render the instruction with the active race date, then add the summary contract.

    Used as `athlete_summary_system(connection, instruction, now=now, zone=zone)`
    """
    rendered = instruction.render(
        now=now,
        zone=zone,
        athlete_name=None,
        race_date=active_race_date(connection),
    )
    return athlete_summary_system_text(rendered)


def _first_heading_line(summary: str) -> int | None:
    lines = enumerate(summary.splitlines(), start=1)
    return next(
        (number for number, line in lines if line.lstrip().startswith("#")), None
    )


def _rejection_reason(summary: str) -> str | None:
    if not 0 < len(summary) <= MAX_ATHLETE_SUMMARY_CHARS:
        return (
            f"has {len(summary)} characters, expected 1 to "
            f"{MAX_ATHLETE_SUMMARY_CHARS} after strip"
        )
    control = first_unsafe_control(summary)
    if control is not None:
        return (
            f"holds control character U+{ord(control):04X}, "
            "expected no control character but newline and tab"
        )
    heading_line = _first_heading_line(summary)
    if heading_line is not None:
        return f"line {heading_line} starts with '#', expected plain text, no headings"
    return None


def _accepted_summary(reply: LLMReply, context: CoachContext) -> str:
    summary = reply.text.strip()
    reason = _rejection_reason(summary)
    if reason is None:
        return summary
    _LOGGER.warning(
        "athlete summary rejected for week %s: %s, request %s",
        context.target_week_start.isoformat(),
        reason,
        reply.request_id,
    )
    raise AthleteSummaryRejectedError(reason)


def _insert_summary(connection: sqlite3.Connection, summary: str) -> int:
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO coach_summary (summary, created_at) VALUES (?, ?)",
            (summary, utc_now()),
        )
    if cursor.lastrowid is None:
        raise RuntimeError("coach_summary INSERT returned no row id, expected an int")
    return cursor.lastrowid


def update_athlete_summary(
    connection: sqlite3.Connection,
    adapter: LLMAdapter,
    *,
    system: str,
    context: CoachContext,
) -> StoredSummary:
    """
    Ask the model once for the rewritten summary and store it as a new row.

    Adapter errors reach the caller unchanged. No retry.
    Used as `update_athlete_summary(connection, adapter, system=text, context=ctx)`
    """
    turns = [ChatTurn("user", context.text)]
    reply = adapter.complete(LLMJob.CHAT, system=system, turns=turns)
    summary = _accepted_summary(reply, context)
    summary_id = _insert_summary(connection, summary)
    _LOGGER.info(
        "athlete summary %d stored for week %s: %d characters, request %s",
        summary_id,
        context.target_week_start.isoformat(),
        len(summary),
        reply.request_id,
    )
    return StoredSummary(summary_id, summary)
