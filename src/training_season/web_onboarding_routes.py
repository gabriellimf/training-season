"""Onboarding on the chat page: the session, its turns, the discard and week 1."""

import logging
import sqlite3

from fastapi import APIRouter

from training_season.coach_context_store import (
    read_active_macro,
    read_active_macro_dates,
)
from training_season.coach_system import StoredProfileError
from training_season.database import transaction
from training_season.garmin_auth import GarminError, GarminMfaRequiredError
from training_season.garmin_auth_text import garmin_failure_cause
from training_season.llm import LLMAdapter, LLMError
from training_season.onboarding_chat import (
    NO_OPEN_SESSION_TEXT,
    OnboardingStateError,
    first_week_outcome,
    onboarding_intro,
    send_onboarding_message,
    session_for,
)
from training_season.onboarding_store import (
    current_open_session,
    discard_open_session,
    session_messages,
    write_cycle_message,
)
from training_season.push_result import PushResult
from training_season.push_store import NoActiveMacroPlanError, active_weekly_plan_id
from training_season.validated_reply import Rejected
from training_season.web_responses import WebConflictError
from training_season.web_schemas import (
    AthleteTextBody,
    DiscardView,
    OnboardingReplyView,
    OnboardingView,
    PageMode,
    SessionBody,
    WeekOneStatus,
    WeekOneView,
    chat_turn_views,
)
from training_season.web_settings import (
    WebContextParam,
    WebSettings,
    open_connection,
    opened_adapter,
)
from training_season.web_state_routes import page_mode
from training_season.weekly_cycle_text import error_cause, rejected_week_cause

ACTIVE_PLAN_PAGE_TEXT = (
    "Já existe um plano ativo. Use Revisar objetivo para mudar o objetivo."
)
NO_ACTIVE_PLAN_PAGE_TEXT = "Nenhum plano ativo. Comece o onboarding."
SESSION_TOO_LONG_PAGE_TEXT = (
    "Esta sessão de onboarding ficou longa demais para continuar. "
    "Descarte a sessão para começar de novo."
)
MODEL_FAILED_PAGE_TEXT = (
    "Não consegui falar com o treinador agora. Sua mensagem não foi salva. "
    "Envie de novo."
)
DISCARDED_PAGE_TEXT = "Sessão de onboarding descartada. As mensagens ficam salvas."
WEEK_ONE_FAILED_HEAD = "A semana 1 não foi enviada: "
WEEK_ONE_SENT_PAGE_TEXT = "A semana 1 já foi enviada."
ONBOARDING_OPEN_PAGE_TEXT = (
    "Uma sessão de onboarding está aberta. "
    "Termine ou descarte a sessão antes de enviar a semana 1."
)
_WEEK_ONE_CONFLICT_TEXTS: dict[PageMode, str] = {
    "needs_onboarding": NO_ACTIVE_PLAN_PAGE_TEXT,
    "onboarding": ONBOARDING_OPEN_PAGE_TEXT,
    "ready": WEEK_ONE_SENT_PAGE_TEXT,
}

_LOGGER = logging.getLogger(__name__)

onboarding_router = APIRouter(prefix="/api")

type _WeekOneOutcome = PushResult | Rejected | WeekOneView


def _onboarding_view(connection: sqlite3.Connection) -> OnboardingView:
    session_id = current_open_session(connection)
    reopening = read_active_macro(connection) is not None
    turns = () if session_id is None else session_messages(connection, session_id)
    return OnboardingView(
        session_id=session_id,
        reopening=reopening,
        intro=onboarding_intro(reopening=reopening),
        messages=chat_turn_views(turns),
    )


@onboarding_router.get("/onboarding")
def read_onboarding(context: WebContextParam) -> OnboardingView:
    with open_connection(context.settings) as connection:
        return _onboarding_view(connection)


def _page_session(connection: sqlite3.Connection, *, reopening: bool) -> None:
    try:
        session_for(connection, reopening=reopening)
    except OnboardingStateError as error:
        text = NO_ACTIVE_PLAN_PAGE_TEXT if reopening else ACTIVE_PLAN_PAGE_TEXT
        raise WebConflictError(text) from error


@onboarding_router.post("/onboarding/session")
def open_onboarding_session(
    body: SessionBody, context: WebContextParam
) -> OnboardingView:
    with (
        context.work.run("onboarding_session"),
        open_connection(context.settings) as connection,
    ):
        _page_session(connection, reopening=body.reopening)
        return _onboarding_view(connection)


def _turn_view(
    connection: sqlite3.Connection, settings: WebSettings, session_id: int, text: str
) -> OnboardingReplyView:
    try:
        with opened_adapter(settings) as adapter:
            result = send_onboarding_message(
                connection, adapter, settings.setup, session_id, text
            )
    except LLMError as error:
        _LOGGER.warning("onboarding turn failed: %s", type(error).__name__)
        return OnboardingReplyView(
            status="model_failed", message=MODEL_FAILED_PAGE_TEXT, macro_plan_id=None
        )
    too_long = result.status == "session_too_long"
    message = SESSION_TOO_LONG_PAGE_TEXT if too_long else result.chat_message
    return OnboardingReplyView(
        status=result.status, message=message, macro_plan_id=result.macro_plan_id
    )


@onboarding_router.post("/onboarding/messages")
def send_onboarding_text(
    body: AthleteTextBody, context: WebContextParam
) -> OnboardingReplyView:
    with (
        context.work.run("onboarding_turn"),
        open_connection(context.settings) as connection,
    ):
        session_id = current_open_session(connection)
        if session_id is None:
            raise WebConflictError(NO_OPEN_SESSION_TEXT)
        return _turn_view(connection, context.settings, session_id, body.text)


@onboarding_router.post("/onboarding/discard")
def discard_onboarding(context: WebContextParam) -> DiscardView:
    with (
        context.work.run("onboarding_discard"),
        open_connection(context.settings) as connection,
    ):
        if discard_open_session(connection) is None:
            raise WebConflictError(NO_OPEN_SESSION_TEXT)
    return DiscardView(status="discarded", message=DISCARDED_PAGE_TEXT)


def _week_one_failure(status: WeekOneStatus, cause: str) -> WeekOneView:
    return WeekOneView(status=status, message=f"{WEEK_ONE_FAILED_HEAD}{cause}")


def _week_one_outcome(
    connection: sqlite3.Connection, adapter: LLMAdapter, settings: WebSettings
) -> _WeekOneOutcome:
    try:
        return first_week_outcome(connection, adapter, settings.setup)
    except NoActiveMacroPlanError as error:
        raise WebConflictError(NO_ACTIVE_PLAN_PAGE_TEXT) from error
    except GarminError as error:
        _LOGGER.warning("week one push failed: %s", type(error).__name__)
        mfa = isinstance(error, GarminMfaRequiredError)
        return _week_one_failure(
            "mfa_required" if mfa else "failed", garmin_failure_cause(error)
        )
    except (LLMError, StoredProfileError) as error:
        _LOGGER.warning("week one push failed: %s", type(error).__name__)
        return _week_one_failure("failed", error_cause(error))


def _push_status(result: PushResult) -> WeekOneStatus:
    if result.failed is not None:
        return "partial"
    if not result.landed:
        return "empty"
    return "pushed"


def _week_one_view(outcome: _WeekOneOutcome) -> WeekOneView:
    if isinstance(outcome, WeekOneView):
        return outcome
    if isinstance(outcome, Rejected):
        return WeekOneView(
            status="rejected", message=rejected_week_cause(outcome.errors)
        )
    return WeekOneView(status=_push_status(outcome), message=outcome.chat_message)


def _store_week_one_message(
    connection: sqlite3.Connection, outcome: _WeekOneOutcome, message: str
) -> None:
    week_id = None
    if isinstance(outcome, PushResult):
        week_id = active_weekly_plan_id(connection, outcome.week_start)
    with transaction(connection):
        write_cycle_message(connection, message, week_id)


def _check_week_one_pending(connection: sqlite3.Connection) -> None:
    mode = page_mode(connection, read_active_macro_dates(connection))
    if mode != "week_one_pending":
        raise WebConflictError(_WEEK_ONE_CONFLICT_TEXTS[mode])


@onboarding_router.post("/week-one")
def send_week_one(context: WebContextParam) -> WeekOneView:
    with (
        context.work.run("week_one"),
        open_connection(context.settings) as connection,
    ):
        _check_week_one_pending(connection)
        with opened_adapter(context.settings) as adapter:
            outcome = _week_one_outcome(connection, adapter, context.settings)
        view = _week_one_view(outcome)
        _store_week_one_message(connection, outcome, view.message)
    return view
