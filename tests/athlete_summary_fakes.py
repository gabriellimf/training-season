"""Named LLM adapter and transport fakes for the athlete summary rewrite."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx2

from openai_compatible_fakes import _completion
from training_season.llm import LLMJob, LLMReply, LLMUsage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from training_season.llm import ChatTurn, LLMError

FAKE_REQUEST_ID = "req_summary_test"


@dataclass
class SummaryChatTransport:
    """An OpenAI-compatible endpoint: `reply_text` on 200, else an empty error body."""

    reply_text: str = "Resumo reescrito."
    status_code: int = 200
    bodies: list[dict[str, object]] = field(default_factory=list)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._answer)

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.status_code != httpx2.codes.OK:
            return httpx2.Response(self.status_code, json={})
        completion = _completion(content=self.reply_text, model=body["model"])
        return httpx2.Response(httpx2.codes.OK, json=completion)


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    """One request the fake received."""

    job: LLMJob
    system: str
    turns: tuple[ChatTurn, ...]


@dataclass
class ScriptedSummaryAdapter:
    """Answers every request with `reply_text`, or raises `error`; records requests."""

    reply_text: str = "Resumo reescrito."
    error: LLMError | None = None
    requests: list[SummaryRequest] = field(default_factory=list)
    closed: bool = False

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        self.requests.append(SummaryRequest(job, system, tuple(turns)))
        if self.error is not None:
            raise self.error
        return LLMReply(
            text=self.reply_text,
            job=job,
            model="fake-summary-model",
            stop_reason="end_turn",
            request_id=FAKE_REQUEST_ID,
            usage=LLMUsage(10, 5, 0, 0, Decimal(0)),
            duration_seconds=0.1,
        )

    def close(self) -> None:
        self.closed = True
