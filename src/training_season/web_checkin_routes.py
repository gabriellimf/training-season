"""The check-in on the chat page: the fixed questions, then the coach chat in memory."""

from fastapi import APIRouter

from training_season.checkin_chat import (
    CHECKIN_INTRO,
    CheckinConversation,
    CheckinReply,
    checkin_reply,
    open_checkin_conversation,
)
from training_season.checkin_questions import (
    CHECKIN_SAVED_TEXT,
    CheckinAnswered,
    CheckinAnswerRejected,
    CheckinNextQuestion,
    checkin_step,
)
from training_season.checkin_store import (
    CheckinAnswers,
    checkin_week_of,
    store_checkin_answers,
)
from training_season.coach_system import coach_system_for_now
from training_season.web_schemas import (
    AthleteTextBody,
    CheckinAnswersBody,
    CheckinEndView,
    CheckinReplyView,
    CheckinStepStatus,
    CheckinStepView,
    CheckinView,
    chat_turn_views,
)
from training_season.web_settings import (
    WebContext,
    WebContextParam,
    WebSettings,
    open_connection,
    opened_adapter,
)

NO_OPEN_CHECKIN_TEXT = "Nenhum check-in aberto. Comece um novo check-in."
CHECKIN_ENDED_TEXT = "Check-in encerrado. Suas respostas e mensagens estão salvas."

checkin_router = APIRouter(prefix="/api")


def _pending_view(step: CheckinNextQuestion | CheckinAnswerRejected) -> CheckinStepView:
    hint = step.hint if isinstance(step, CheckinAnswerRejected) else None
    status: CheckinStepStatus = "question" if hint is None else "hint"
    return CheckinStepView(
        status=status,
        question=step.question,
        hint=hint,
        accepted=step.accepted,
        answer_kind=step.kind,
        message=None,
    )


def _open_checkin(context: WebContext, answers: CheckinAnswers) -> None:
    setup = context.settings.setup
    with (
        context.work.run("checkin_save"),
        open_connection(context.settings) as connection,
    ):
        now = setup.clock()
        system, today = coach_system_for_now(
            setup.instruction, connection, now=now, zone=setup.zone
        )
        checkin_id = store_checkin_answers(
            connection, answers, week_start=checkin_week_of(today), now=now
        )
        conversation = open_checkin_conversation(connection, checkin_id, system, today)
        context.checkin.replace(conversation)


@checkin_router.post("/checkin/answers")
def answer_checkin(
    body: CheckinAnswersBody, context: WebContextParam
) -> CheckinStepView:
    step = checkin_step(body.answers)
    if not isinstance(step, CheckinAnswered):
        return _pending_view(step)
    _open_checkin(context, step.answers)
    return CheckinStepView(
        status="saved",
        question=None,
        hint=None,
        accepted=len(body.answers),
        answer_kind=None,
        message=CHECKIN_SAVED_TEXT,
    )


@checkin_router.get("/checkin")
def read_checkin(context: WebContextParam) -> CheckinView:
    conversation = context.checkin.current()
    if conversation is None:
        return CheckinView(
            open=False, intro=CHECKIN_INTRO, saved_message=None, messages=[]
        )
    return CheckinView(
        open=True,
        intro=CHECKIN_INTRO,
        saved_message=CHECKIN_SAVED_TEXT,
        messages=chat_turn_views(conversation.history),
    )


def _coach_reply(
    settings: WebSettings, conversation: CheckinConversation, text: str
) -> CheckinReply:
    with (
        open_connection(settings) as connection,
        opened_adapter(settings) as adapter,
    ):
        return checkin_reply(connection, adapter, conversation, text)


@checkin_router.post("/checkin/messages")
def send_checkin_text(
    body: AthleteTextBody, context: WebContextParam
) -> CheckinReplyView:
    with context.work.run("checkin_reply"):
        conversation = context.checkin.current()
        if conversation is None:
            return CheckinReplyView(status="not_open", message=NO_OPEN_CHECKIN_TEXT)
        reply = _coach_reply(context.settings, conversation, body.text)
        context.checkin.replace(None if reply.ends_conversation else reply.conversation)
    return CheckinReplyView(status=reply.status, message=reply.chat_message)


@checkin_router.post("/checkin/end")
def end_checkin(context: WebContextParam) -> CheckinEndView:
    with context.work.run("checkin_end"):
        context.checkin.replace(None)
    return CheckinEndView(status="ended", message=CHECKIN_ENDED_TEXT)
