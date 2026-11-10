"""Payload builders for the onboarding result, the macro plan and their stores."""

import json
from datetime import date, timedelta

from training_season.athlete_profile import (
    OnboardingResult,
    onboarding_result_from_text,
)
from training_season.macro_schema import MacroPlan, macro_plan_from_text
from training_season.onboarding_store import SessionExchange

TODAY = date(2026, 11, 10)
RACE_DAY = date(2027, 4, 18)
REASON = "Primeira maratona, meta de terminar bem."
CLOSING_EXCHANGE = SessionExchange("Pode montar.", "Plano salvo.")


def goal_payload(
    *, race_date: date = RACE_DAY, distance_km: object = 42.195
) -> dict[str, object]:
    return {
        "race_name": "Maratona de Teste",
        "race_date": race_date.isoformat(),
        "distance_km": distance_km,
        "goal": "Terminar abaixo de 4h",
    }


def availability_payload(
    *, days_per_week: int = 4, preferred_days: list[str] | None = None
) -> dict[str, object]:
    days = ["tuesday", "thursday", "saturday", "sunday"]
    return {
        "days_per_week": days_per_week,
        "preferred_days": days if preferred_days is None else preferred_days,
        "notes": None,
    }


def profile_payload(
    *,
    goal: dict[str, object] | None = None,
    availability: dict[str, object] | None = None,
    **fields: object,
) -> dict[str, object]:
    profile: dict[str, object] = {
        "athlete_name": "Gabi",
        "goal": goal_payload() if goal is None else goal,
        "running_history": "Corre há 3 anos, 30 km por semana, longão de 18 km.",
        "injuries": [{"description": "Canelite em 2025", "status": "recovered"}],
        "availability": availability_payload()
        if availability is None
        else availability,
        "notes": None,
    }
    profile.update(fields)
    return profile


def result_text(
    *,
    profile: dict[str, object] | None = None,
    reason: object = REASON,
    **extra: object,
) -> str:
    payload = {
        "profile": profile_payload() if profile is None else profile,
        "reason": reason,
        **extra,
    }
    return json.dumps(payload)


def valid_result(
    *, race_date: date = RACE_DAY, reason: str = REASON
) -> OnboardingResult:
    profile = profile_payload(goal=goal_payload(race_date=race_date))
    return onboarding_result_from_text(
        result_text(profile=profile, reason=reason), TODAY
    )


def phase_payload(
    name: str,
    start: date,
    end: date,
    *,
    weekly_km: object = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "name": name,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "focus": f"Foco da fase {name}",
        "weekly_km": {"min": 20, "max": 30} if weekly_km is None else weekly_km,
        **extra,
    }


def default_phases(
    start: date = TODAY, race_date: date = RACE_DAY
) -> list[dict[str, object]]:
    base_end = start + timedelta(days=20)
    taper_start = race_date - timedelta(days=20)
    return [
        phase_payload("Base", start, base_end),
        phase_payload(
            "Build", base_end + timedelta(days=1), taper_start - timedelta(1)
        ),
        phase_payload("Taper", taper_start, race_date),
    ]


def macro_text(
    *,
    phases: list[dict[str, object]] | None = None,
    start_date: date = TODAY,
    race_date: date = RACE_DAY,
    **extra: object,
) -> str:
    payload = {
        "start_date": start_date.isoformat(),
        "race_date": race_date.isoformat(),
        "summary": "Plano de 23 semanas até a maratona.",
        "phases": default_phases(start_date, race_date) if phases is None else phases,
        **extra,
    }
    return json.dumps(payload)


def valid_macro_plan(*, race_date: date = RACE_DAY) -> MacroPlan:
    return macro_plan_from_text(macro_text(race_date=race_date), TODAY, race_date)
