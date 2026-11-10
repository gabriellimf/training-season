"""Generate and push week 1 right after onboarding, from today to Sunday."""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from training_season.garmin_auth import default_garmin_factory
from training_season.garmin_push import push_garmin_week
from training_season.push_store import active_macro_plan_id
from training_season.validated_reply import Rejected
from training_season.week_generation import generate_week

if TYPE_CHECKING:
    import sqlite3

    from training_season.garmin_auth import GarminAuthConfig, GarminFactory
    from training_season.llm import LLMAdapter
    from training_season.push_result import PushResult

_SUNDAY = 6


def week_one_range(today: date) -> tuple[date, date]:
    """
    Return today and the first Sunday on or after today.

    The Monday-to-Sunday weekly cycle plans the days after it.
    Used as `first_day, last_day = week_one_range(date(2027, 1, 6))`
    """
    return today, today + timedelta(days=_SUNDAY - today.weekday())


def push_week_one(
    connection: sqlite3.Connection,
    adapter: LLMAdapter,
    auth_config: GarminAuthConfig,
    *,
    instruction_text: str,
    today: date,
    factory: GarminFactory = default_garmin_factory,
) -> PushResult | Rejected:
    """
    Ask the plan model for today..Sunday and push it under the active macro.

    Only that range is cleared on Garmin. A rejected week pushes nothing.
    With no active macro, `NoActiveMacroPlanError` is raised before any model call.
    Used as `push_week_one(connection, adapter, auth_config,
    instruction_text=rendered, today=date(2027, 1, 6))`
    """
    active_macro_plan_id(connection)
    first_day, last_day = week_one_range(today)
    outcome = generate_week(
        connection,
        adapter,
        instruction_text=instruction_text,
        first_day=first_day,
        last_day=last_day,
    )
    if isinstance(outcome, Rejected):
        return outcome
    return push_garmin_week(connection, auth_config, outcome.value, factory=factory)
