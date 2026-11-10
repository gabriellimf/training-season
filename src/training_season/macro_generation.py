"""Ask the plan model for the macro plan from the onboarding result."""

from __future__ import annotations

from typing import TYPE_CHECKING

from training_season.garmin_pull_base import canonical_json
from training_season.llm import ChatTurn, LLMJob
from training_season.macro_schema import (
    MAX_PHASES,
    MAX_WEEKLY_KM,
    MacroPlan,
    macro_plan_from_text,
)
from training_season.validated_reply import Accepted, Rejected, request_with_one_retry
from training_season.workout_request import JSON_ONLY_HEADER
from training_season.workout_validation import json_body

if TYPE_CHECKING:
    from datetime import date

    from training_season.athlete_profile import OnboardingResult
    from training_season.llm import LLMAdapter

MACRO_OUTPUT_CONTRACT = f"""\
{JSON_ONLY_HEADER}
Campos do objeto do plano macro:
- start_date: o primeiro dia do plano, YYYY-MM-DD, o dia pedido.
- race_date: o dia da prova, YYYY-MM-DD, o dia pedido.
- summary: texto de 1 a 2000 caracteres com a visão geral do plano.
- phases: lista de 1 a {MAX_PHASES} fases, em ordem.
Cada fase tem:
- name: texto de 1 a 60 caracteres, em uma linha.
- start_date e end_date: YYYY-MM-DD, os dois dias contam.
- focus: texto de 1 a 1000 caracteres.
- weekly_km: {{"min": ..., "max": ...}} em km por semana, 0 <= min <= max <= \
{MAX_WEEKLY_KM}.
A primeira fase começa em start_date. Cada fase começa no dia seguinte ao fim da
anterior, sem buraco e sem sobreposição. A última fase termina em race_date.
Não use outras chaves."""

_REJECTION_HEADLINE = (
    "O treinador devolveu um plano macro inválido duas vezes. Nada foi salvo. "
    "Responda para tentar de novo."
)

type MacroOutcome = Accepted[MacroPlan] | Rejected


def macro_system_text(instruction_text: str) -> str:
    """
    Append the macro output contract to the rendered coach instruction.

    Used as `macro_system_text(render_coach_system(instruction, connection, ...))`
    """
    return f"{instruction_text}\n\n{MACRO_OUTPUT_CONTRACT}"


def _macro_request_turn(
    context_text: str, result: OnboardingResult, today: date
) -> ChatTurn:
    race_date = result.profile.goal.race_date.isoformat()
    profile_json = canonical_json(result.profile.model_dump(mode="json"))
    request = (
        f"## Resultado do onboarding\nPerfil: {profile_json}\n"
        f"Motivo: {result.reason}\n"
        f"Monte o plano macro de {today.isoformat()} até a prova em {race_date}. "
        f"Use start_date {today.isoformat()} e race_date {race_date}."
    )
    return ChatTurn("user", f"{context_text}\n{request}")


def generate_macro(
    adapter: LLMAdapter,
    *,
    instruction_text: str,
    result: OnboardingResult,
    context_text: str,
    today: date,
) -> MacroOutcome:
    """
    Ask the plan model for the macro from today to the race. Stores nothing.

    Used as `generate_macro(adapter, instruction_text=rendered, result=result,
    context_text=context.text, today=date(2026, 11, 10))`
    """
    race_date = result.profile.goal.race_date
    return request_with_one_retry(
        adapter,
        LLMJob.PLAN,
        system=macro_system_text(instruction_text),
        turns=(_macro_request_turn(context_text, result, today),),
        parse=lambda text: macro_plan_from_text(json_body(text), today, race_date),
        rejection_headline=_REJECTION_HEADLINE,
    )
