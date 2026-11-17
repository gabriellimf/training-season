"""Pydantic bodies of the chat page JSON API. Every text in them is pt-BR."""

from collections.abc import Iterable
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from training_season.checkin_chat import CheckinReplyStatus
from training_season.checkin_questions import CheckinAnswerKind
from training_season.garmin_auth import GarminAuthState
from training_season.garmin_auth_text import GarminAuthOutcomeStatus
from training_season.onboarding_chat import cleaned_athlete_text
from training_season.onboarding_store import MessageKind, SessionMessage, SessionRole
from training_season.onboarding_turn import TurnStatus
from training_season.plan_view import Intensity
from training_season.web_work import CoachJob

type PageMode = Literal["needs_onboarding", "onboarding", "week_one_pending", "ready"]
type ErrorStatus = Literal["busy", "conflict", "invalid", "error"]
type OnboardingReplyStatus = TurnStatus | Literal["model_failed"]
type WeekOneStatus = Literal[
    "pushed", "partial", "empty", "rejected", "mfa_required", "failed"
]
type CheckinStepStatus = Literal["question", "hint", "saved"]
type CheckinMessageStatus = CheckinReplyStatus | Literal["not_open"]

MAX_PAGE_TEXT_LENGTH = 16_000
MAX_CHECKIN_ANSWERS = 4
MAX_CHECKIN_ANSWER_LENGTH = 400
MAX_MFA_CODE_LENGTH = 32


def _visible_athlete_text(text: str) -> str:
    cleaned = cleaned_athlete_text(text)
    if not cleaned:
        raise ValueError("text is blank after cleanup, expected a visible character")
    return cleaned


AthleteText = Annotated[
    str, Field(max_length=MAX_PAGE_TEXT_LENGTH), AfterValidator(_visible_athlete_text)
]


class ErrorView(BaseModel):
    """The body of every error answer."""

    status: ErrorStatus
    message: str


class BusyView(BaseModel):
    """The coach job that runs now and its UTC start time."""

    job: CoachJob
    started_at: str


class GarminStateView(BaseModel):
    """Local Garmin login state; it decides when the page shows the MFA input."""

    state: GarminAuthState
    mfa_requested_at: str | None


class StateView(BaseModel):
    """What the page shows on load and while it polls."""

    mode: PageMode
    reopening: bool
    has_active_macro: bool
    checkin_open: bool
    busy: BusyView | None
    garmin: GarminStateView
    today: date


class HistoryMessageView(BaseModel):
    """One stored chat turn of the history."""

    role: SessionRole
    content: str
    created_at: str
    kind: MessageKind


class MessagesView(BaseModel):
    """The newest chat turns, oldest first."""

    messages: list[HistoryMessageView]


class ChatTurnView(BaseModel):
    """One turn of the open onboarding session or check-in chat."""

    role: SessionRole
    content: str


def chat_turn_views(turns: Iterable[SessionMessage]) -> list[ChatTurnView]:
    """
    Copy stored or in-memory turns into their response bodies.

    Used as `messages=chat_turn_views(session_messages(connection, session_id))`
    """
    return [ChatTurnView(role=turn.role, content=turn.content) for turn in turns]


class AthleteTextBody(BaseModel):
    """One athlete chat message, cleaned like a console line."""

    text: AthleteText = Field(repr=False)


class SessionBody(BaseModel):
    """`reopening` is true for a review of the goal of the active plan."""

    reopening: bool


class OnboardingView(BaseModel):
    """The open session, or the intro the next session starts with."""

    session_id: int | None
    reopening: bool
    intro: str
    messages: list[ChatTurnView]


class OnboardingReplyView(BaseModel):
    """The outcome of one onboarding message; `completed` carries the new plan id."""

    status: OnboardingReplyStatus
    message: str
    macro_plan_id: int | None


class DiscardView(BaseModel):
    """The open onboarding session was discarded."""

    status: Literal["discarded"]
    message: str


class WeekOneView(BaseModel):
    """The outcome of the week 1 push; the message is also stored in the history."""

    status: WeekOneStatus
    message: str


class CheckinAnswersBody(BaseModel):
    """Every raw answer the page keeps, in question order."""

    answers: list[Annotated[str, Field(max_length=MAX_CHECKIN_ANSWER_LENGTH)]] = Field(
        max_length=MAX_CHECKIN_ANSWERS, repr=False
    )


class CheckinStepView(BaseModel):
    """The next question, the hint for a bad answer, or the saved check-in."""

    status: CheckinStepStatus
    question: str | None
    hint: str | None
    accepted: int
    answer_kind: CheckinAnswerKind | None
    message: str | None


class CheckinView(BaseModel):
    """
    The open check-in chat; `intro` is there also when none is open.

    `saved_message` is the saved-check-in text while the chat is open, else None.
    """

    open: bool
    intro: str
    saved_message: str | None
    messages: list[ChatTurnView]


class CheckinReplyView(BaseModel):
    """The outcome of one check-in chat message."""

    status: CheckinMessageStatus
    message: str


class CheckinEndView(BaseModel):
    """The check-in chat is over."""

    status: Literal["ended"]
    message: str


class MfaCodeBody(BaseModel):
    """The code Garmin sent. It is never logged or sent back."""

    code: str = Field(min_length=1, max_length=MAX_MFA_CODE_LENGTH, repr=False)


class GarminAuthView(BaseModel):
    """The outcome of a login or a code, and the Garmin state after it."""

    status: GarminAuthOutcomeStatus
    message: str
    garmin: GarminStateView


class StepTextView(BaseModel):
    """One step as Portuguese text; a repeat holds its child steps."""

    model_config = ConfigDict(from_attributes=True)

    text: str
    notes: str | None
    steps: list["StepTextView"] | None


class PlanWorkoutView(BaseModel):
    """One planned workout of a plan card."""

    model_config = ConfigDict(from_attributes=True)

    date: date
    weekday: str
    name: str
    intensity: Intensity
    description: str | None
    scheduled: bool
    steps: list[StepTextView]


class PlanWeekView(BaseModel):
    """One plan card with the km against last week and the easy/hard split."""

    model_config = ConfigDict(from_attributes=True)

    week_start: date
    week_end: date
    heading: str
    volume_line: str
    split_line: str
    workouts: list[PlanWorkoutView]


class PlanView(BaseModel):
    """The stored weeks of the active plan that are not over."""

    today: date
    weeks: list[PlanWeekView]
