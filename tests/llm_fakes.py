"""A named LLM adapter fake that answers from a queue of reply texts."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from training_season.llm import LLMJob, LLMReply, LLMUnavailableError, LLMUsage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from training_season.llm import ChatTurn


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """One request the fake received."""

    job: LLMJob
    system: str
    turns: tuple[ChatTurn, ...]


@dataclass
class QueuedReplyAdapter:
    """Answers each request with the next text of `replies`; records requests."""

    replies: list[str]
    requests: list[RecordedRequest] = field(default_factory=list)
    closed: bool = False

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        self.requests.append(RecordedRequest(job, system, tuple(turns)))
        return LLMReply(
            text=self.replies.pop(0),
            job=job,
            model="fake-queued-model",
            stop_reason="end_turn",
            request_id=f"req_queued_{len(self.requests)}",
            usage=LLMUsage(10, 5, 0, 0, Decimal(0)),
            duration_seconds=0.1,
        )

    def close(self) -> None:
        self.closed = True


@dataclass
class UnavailableAdapter:
    """Fails every request as an unreachable provider; records the jobs."""

    jobs: list[LLMJob] = field(default_factory=list)

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        self.jobs.append(job)
        raise LLMUnavailableError("fake provider is unreachable")

    def close(self) -> None:
        return None
