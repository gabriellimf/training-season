"""Ask the coach model for the workouts of first_day..last_day, validated."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from training_season.bounded_text import text_length_errors
from training_season.llm import ChatTurn, LLMJob
from training_season.push_week import PushWeek, PushWeekError, push_week_from_text
from training_season.validated_reply import (
    Accepted,
    Rejected,
    request_with_one_retry,
)
from training_season.workout_request import JSON_ONLY_HEADER, WORKOUT_FIELD_RULES
from training_season.workout_validation import json_body

if TYPE_CHECKING:
    from datetime import date

    from training_season.llm import LLMAdapter

MAX_WEEK_TEXT_LENGTH = 100_000

WEEK_OUTPUT_CONTRACT = f"""\
{JSON_ONLY_HEADER}
Campos do objeto da semana:
- week_start: o primeiro dia pedido, YYYY-MM-DD.
- workouts: lista de no máximo 7 itens {{"date": "YYYY-MM-DD", "workout": {{...}}}}.
No máximo um treino por data. Todas as datas ficam entre o primeiro e o último
dia pedidos. Dia sem treino fica fora da lista. Não use outras chaves.
O name de cada treino é "DD/MM <nome>": a data do treino, um espaço e um nome curto
sem acento, por exemplo "26/10 10x400". Cada treino da semana tem um nome diferente.
Cada workout segue as regras abaixo.
{WORKOUT_FIELD_RULES}"""

WEEK_REJECTION_HEADLINE = (
    "O treinador devolveu uma semana inválida duas vezes. Nada foi enviado ao Garmin."
)

type WeekOutcome = Accepted[PushWeek] | Rejected


def week_system_text(instruction_text: str) -> str:
    """
    Append the week output contract to the rendered coach instruction.

    Used as `week_system_text(render_coach_system(instruction, connection, ...))`
    """
    return f"{instruction_text}\n\n{WEEK_OUTPUT_CONTRACT}"


def _ending_on(week: PushWeek, last_day: date) -> PushWeek:
    try:
        return PushWeek(
            week_start=week.week_start, week_end=last_day, workouts=week.workouts
        )
    except ValidationError as error:
        raise PushWeekError.from_pydantic(error) from error


def week_from_text(text: str, first_day: date, last_day: date) -> PushWeek:
    """
    Parse the model week, plain or fenced, as the days first_day..last_day.

    Used as `week_from_text(reply.text, date(2027, 1, 6), date(2027, 1, 10))`
    """
    if errors := text_length_errors("week text", text, MAX_WEEK_TEXT_LENGTH):
        raise PushWeekError(errors)
    week = push_week_from_text(json_body(text))
    if week.week_start != first_day:
        found, expected = week.week_start.isoformat(), first_day.isoformat()
        raise PushWeekError((f"week_start is {found}, expected {expected}",))
    return _ending_on(week, last_day)


def _week_request_turn(context_text: str, first_day: date, last_day: date) -> ChatTurn:
    request = (
        f"Planeje os treinos de {first_day.isoformat()} a {last_day.isoformat()}. "
        f"Use week_start {first_day.isoformat()}."
    )
    return ChatTurn("user", f"{context_text}\n{request}")


def generate_week(
    adapter: LLMAdapter,
    *,
    instruction_text: str,
    context_text: str,
    first_day: date,
    last_day: date,
) -> WeekOutcome:
    """
    Ask the plan model for first_day..last_day from the coach context. Stores nothing.

    Used as `generate_week(adapter, instruction_text=rendered,
    context_text=assemble_coach_context(connection, first_day).text,
    first_day=date(2027, 1, 6), last_day=date(2027, 1, 10))`
    """
    return request_with_one_retry(
        adapter,
        LLMJob.PLAN,
        system=week_system_text(instruction_text),
        turns=(_week_request_turn(context_text, first_day, last_day),),
        parse=lambda text: week_from_text(text, first_day, last_day),
        rejection_headline=WEEK_REJECTION_HEADLINE,
    )
