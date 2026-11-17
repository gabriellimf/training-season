"""`GET /api/state` and `GET /api/messages`: what the page shows. No network call."""

import logging
import sqlite3
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from training_season.coach_context_store import (
    ActiveMacroDates,
    has_weekly_plan,
    read_active_macro_dates,
)
from training_season.garmin_auth import (
    GarminAuthConfig,
    GarminAuthState,
    GarminError,
    auth_status,
)
from training_season.onboarding_store import current_open_session, recent_messages
from training_season.web_schemas import (
    BusyView,
    GarminStateView,
    HistoryMessageView,
    MessagesView,
    PageMode,
    StateView,
)
from training_season.web_settings import (
    WebContext,
    open_connection,
    training_today,
    web_context,
)

MAX_HISTORY_MESSAGES = 200

_LOGGER = logging.getLogger(__name__)

state_router = APIRouter(prefix="/api")


def page_mode(
    connection: sqlite3.Connection, macro: ActiveMacroDates | None
) -> PageMode:
    """
    Pick the page mode from the stored plan state; the first rule that fits wins.

    Used as `mode = page_mode(connection, read_active_macro_dates(connection))`
    """
    if current_open_session(connection) is not None:
        return "onboarding"
    if macro is None:
        return "needs_onboarding"
    first = date.fromisoformat(macro.start_date)
    last = date.fromisoformat(macro.race_date)
    if not has_weekly_plan(connection, macro.macro_plan_id, first, last):
        return "week_one_pending"
    return "ready"


def garmin_state(config: GarminAuthConfig) -> GarminStateView:
    """
    Read the local Garmin login state. An unreadable state shows as no token.

    The login button then gives the Portuguese cause from the real login.
    Used as `garmin = garmin_state(context.settings.setup.auth_config)`
    """
    try:
        status = auth_status(config)
    except GarminError as error:
        _LOGGER.warning("Garmin auth state is unreadable: %s", type(error).__name__)
        return GarminStateView(state=GarminAuthState.NO_TOKEN, mfa_requested_at=None)
    return GarminStateView(state=status.state, mfa_requested_at=status.mfa_requested_at)


def _busy_view(context: WebContext) -> BusyView | None:
    running = context.work.current()
    if running is None:
        return None
    return BusyView(job=running.job, started_at=running.started_at)


@state_router.get("/state")
def read_state(context: Annotated[WebContext, Depends(web_context)]) -> StateView:
    setup = context.settings.setup
    with open_connection(context.settings) as connection:
        macro = read_active_macro_dates(connection)
        mode = page_mode(connection, macro)
    return StateView(
        mode=mode,
        reopening=mode == "onboarding" and macro is not None,
        has_active_macro=macro is not None,
        checkin_open=context.checkin.current() is not None,
        busy=_busy_view(context),
        garmin=garmin_state(setup.auth_config),
        today=training_today(setup),
    )


@state_router.get("/messages")
def read_messages(
    context: Annotated[WebContext, Depends(web_context)],
    limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_MESSAGES)] = 50,
) -> MessagesView:
    with open_connection(context.settings) as connection:
        history = recent_messages(connection, limit)
    return MessagesView(
        messages=[
            HistoryMessageView(
                role=turn.role,
                content=turn.content,
                created_at=turn.created_at,
                kind=turn.kind,
            )
            for turn in history
        ]
    )
