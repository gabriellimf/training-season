"""Ask the model once, and once more with the errors when its reply is invalid."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from training_season.llm import ChatTurn
from training_season.workout_validation import WorkoutValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from training_season.llm import LLMAdapter, LLMJob, LLMReply

type RetryRequest = str | Callable[[str], str]

_LOGGER = logging.getLogger(__name__)
_RETRY_HEADLINE = "O JSON anterior falhou na validação:"
_RETRY_REQUEST = "Responda apenas com o JSON corrigido."
_EMPTY_REPLY_PLACEHOLDER = "(resposta vazia)"


@dataclass(frozen=True, slots=True)
class Accepted[T]:
    """A parsed reply value, with the replies that produced it."""

    value: T
    attempts: int
    replies: tuple[LLMReply, ...]


@dataclass(frozen=True, slots=True)
class Rejected:
    """Two invalid replies. `chat_message` is the Portuguese text for the chat."""

    chat_message: str
    errors: tuple[str, ...]
    attempts: int
    replies: tuple[LLMReply, ...]


@dataclass(frozen=True, slots=True)
class _ReplyRequest[T]:
    adapter: LLMAdapter
    job: LLMJob
    system: str
    parse: Callable[[str], T]
    rejection_headline: str
    retry_request: RetryRequest

    def send(self, turns: Sequence[ChatTurn]) -> LLMReply:
        return self.adapter.complete(self.job, system=self.system, turns=turns)

    def retry_text(self, first: LLMReply) -> str:
        if isinstance(self.retry_request, str):
            return self.retry_request
        return self.retry_request(first.text)


def _retry_turns(
    turns: Sequence[ChatTurn],
    first: LLMReply,
    errors: tuple[str, ...],
    retry_request: str,
) -> list[ChatTurn]:
    echoed = first.text if first.text.strip() else _EMPTY_REPLY_PLACEHOLDER
    lines = "\n".join(f"- {line}" for line in errors)
    retry = ChatTurn("user", f"{_RETRY_HEADLINE}\n{lines}\n{retry_request}")
    return [*turns, ChatTurn("assistant", echoed), retry]


def _log_failed_attempt(attempt: int, error: WorkoutValidationError) -> None:
    _LOGGER.warning(
        "Reply validation failed attempt=%d error_count=%d",
        attempt,
        len(error.errors),
    )


def request_with_one_retry[T](
    adapter: LLMAdapter,
    job: LLMJob,
    *,
    system: str,
    turns: Sequence[ChatTurn],
    parse: Callable[[str], T],
    rejection_headline: str,
    retry_request: RetryRequest = _RETRY_REQUEST,
) -> Accepted[T] | Rejected:
    """
    Request one reply. When `parse` raises, ask once more with its error lines.

    `parse` raises `WorkoutValidationError` or a subclass. An `LLMError`
    reaches the caller. `retry_request` ends the retry turn: a fixed text, or
    a function of the first reply text.

    Used as `request_with_one_retry(adapter, LLMJob.PLAN, system=text,
    turns=turns, parse=workout_from_text, rejection_headline="...")`
    """
    request = _ReplyRequest(
        adapter, job, system, parse, rejection_headline, retry_request
    )
    first = request.send(turns)
    try:
        return Accepted(parse(first.text), 1, (first,))
    except WorkoutValidationError as error:
        _log_failed_attempt(1, error)
        retry_text = request.retry_text(first)
        retry_turns = _retry_turns(turns, first, error.errors, retry_text)
    return _second_outcome(request, first, request.send(retry_turns))


def _second_outcome[T](
    request: _ReplyRequest[T], first: LLMReply, second: LLMReply
) -> Accepted[T] | Rejected:
    replies = (first, second)
    try:
        return Accepted(request.parse(second.text), 2, replies)
    except WorkoutValidationError as error:
        _log_failed_attempt(2, error)
        message = "\n".join((request.rejection_headline, *error.errors))
        return Rejected(message, error.errors, 2, replies)
