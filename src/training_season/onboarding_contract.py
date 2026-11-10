"""The onboarding interview contract: system text, turns and the result block."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from training_season.athlete_profile import (
    OnboardingResult,
    OnboardingResultError,
    onboarding_result_from_text,
)
from training_season.bounded_text import drop_control_characters_but_newline
from training_season.llm import ChatTurn
from training_season.workout_validation import json_body

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from training_season.onboarding_store import SessionMessage

RESULT_OPEN_TAG = "<onboarding_result>"
RESULT_CLOSE_TAG = "</onboarding_result>"

ONBOARDING_CONTRACT = f"""\
Onboarding

Você conduz a entrevista inicial com o atleta, em português, com poucas perguntas
por vez. A primeira mensagem do usuário traz o contexto do atleta e diz se é o
primeiro onboarding ou uma revisão do objetivo.
Antes de terminar, você precisa saber:
- athlete_name: nome ou apelido do atleta, de 1 a 40 caracteres, ou null se ele
não quiser dar.
- goal: race_name (nome da prova), race_date (YYYY-MM-DD, depois de hoje),
distance_km (número maior que zero) e goal (o que é sucesso para o atleta).
- running_history: há quanto tempo corre, km por semana recentes, maior distância
recente e provas recentes.
- injuries: lista de {{"description": texto, "status": "active" ou "recovered"}}.
Lista vazia se não houver lesão.
- availability: days_per_week (1 a 7), preferred_days (dias distintos, no máximo
days_per_week, entre monday, tuesday, wednesday, thursday, friday, saturday e
sunday) e notes (texto ou null).
- notes: outra informação que você quer guardar, ou null.
- reason: por que este plano existe. No primeiro onboarding, com suas palavras. Na
revisão do objetivo, o motivo da mudança que o atleta deu.
Quando souber tudo, resuma os dados e peça confirmação. Só depois da confirmação,
termine a resposta com um bloco
{RESULT_OPEN_TAG}{{"profile": {{...}}, "reason": "..."}}{RESULT_CLOSE_TAG}
O bloco tem um objeto JSON com profile (os campos acima, menos reason) e reason, sem
outras chaves. Nunca mande o bloco antes da confirmação. O atleta não vê o bloco."""

ONBOARDING_RETRY_REQUEST = (
    f"Responda só com o bloco {RESULT_OPEN_TAG}...{RESULT_CLOSE_TAG} corrigido."
)
ANSWER_RETRY_REQUEST = "Responda à última mensagem do atleta."

_FIRST_RUN_KIND = "primeiro onboarding"
_REOPEN_KIND = "revisão do objetivo, o plano macro ativo será substituído"
_ATHLETE_MESSAGE_HEADING = "Mensagem do atleta:"


@dataclass(frozen=True, slots=True)
class SplitReply:
    """A coach reply cut into the text the athlete sees and the result body."""

    visible: str = field(repr=False)
    result_json: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class InterviewReply:
    """A valid interview reply: the visible text and, at the end, the result."""

    visible: str = field(repr=False)
    result: OnboardingResult | None = field(repr=False)


def onboarding_system_text(instruction_text: str) -> str:
    """
    Append the onboarding contract to the rendered coach instruction.

    Used as `onboarding_system_text(render_coach_system(instruction, connection, ...))`
    """
    return f"{instruction_text}\n\n{ONBOARDING_CONTRACT}"


def kickoff_text(context_text: str, *, reopening: bool) -> str:
    """
    The code-owned opening of every onboarding request. It is never stored.

    Used as `kickoff_text(assemble_coach_context(connection, today).text,
    reopening=False)`
    """
    kind = _REOPEN_KIND if reopening else _FIRST_RUN_KIND
    return f"Início do onboarding: {kind}.\n\nContexto do atleta:\n{context_text}"


def conversation_turns(
    kickoff: str, history: Sequence[SessionMessage], user_text: str
) -> list[ChatTurn]:
    """
    The kickoff joined to the first athlete turn, the history, then the new text.

    Used as `conversation_turns(kickoff, session_messages(connection, 4), "Oi")`
    """
    turns = [ChatTurn(message.role, message.content) for message in history]
    turns.append(ChatTurn("user", user_text))
    first = f"{kickoff}\n\n{_ATHLETE_MESSAGE_HEADING}\n{turns[0].text}"
    return [ChatTurn("user", first), *turns[1:]]


def split_result(reply_text: str) -> SplitReply:
    """
    Cut the result block out of a reply. An unclosed block runs to the end.

    Used as `split_result("Pronto. <onboarding_result>{...}</onboarding_result>")`
    """
    before, tag, rest = reply_text.partition(RESULT_OPEN_TAG)
    if not tag:
        return SplitReply(_shown(reply_text), None)
    body, _, after = rest.partition(RESULT_CLOSE_TAG)
    visible = "\n\n".join(part for part in (_shown(before), _shown(after)) if part)
    return SplitReply(visible, body.strip())


def _shown(reply_part: str) -> str:
    return drop_control_characters_but_newline(reply_part).strip()


def interview_retry_request(first_reply_text: str) -> str:
    """
    Ask for the corrected block only when the first reply sent one.

    A reply with no block is still mid-interview, so the block is not asked for.
    Used as `request_with_one_retry(..., retry_request=interview_retry_request)`
    """
    if split_result(first_reply_text).result_json is None:
        return ANSWER_RETRY_REQUEST
    return ONBOARDING_RETRY_REQUEST


def interview_reply_from_text(text: str, today: date) -> InterviewReply:
    """
    Parse one interview reply. A result block must hold a valid result.

    Used as `interview_reply_from_text(reply.text, date(2026, 11, 10))`
    """
    split = split_result(text)
    if split.result_json is not None:
        result_json = json_body(split.result_json)
        return InterviewReply(
            split.visible, onboarding_result_from_text(result_json, today)
        )
    if not split.visible:
        raise OnboardingResultError(
            ("reply has no text, expected a question or a result block",)
        )
    return InterviewReply(split.visible, None)
