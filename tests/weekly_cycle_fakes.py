"""Named fakes for the weekly cycle: one Garmin factory and a chat-failing adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from garmin_auth_fakes import _chain_rejected_token_error
from garmin_pull_fakes import FakeGarminData, PullFakeGarmin
from garmin_push_fakes import FakeGarminCalendar, PushFakeGarmin
from llm_fakes import QueuedReplyAdapter
from training_season.llm import LLMJob, LLMUnavailableError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from garminconnect import Garmin

    from garmin_auth_fakes import FakeGarminServer
    from training_season.llm import ChatTurn, LLMReply


@dataclass
class CycleGarminFactory:
    """
    The first Garmin client pulls, every later one pushes; records both.

    `mfa_at_push` makes Garmin reject the saved token at the push session and
    ask for MFA at the credential login after it.
    """

    server: FakeGarminServer
    pull_data: FakeGarminData = field(default_factory=FakeGarminData)
    calendar: FakeGarminCalendar = field(default_factory=FakeGarminCalendar)
    pull_clients: list[PullFakeGarmin] = field(default_factory=list)
    push_clients: list[PushFakeGarmin] = field(default_factory=list)
    mfa_at_push: bool = False

    def __call__(self, email: str | None, password: str | None) -> Garmin:
        if not self.pull_clients:
            pull = PullFakeGarmin(self.server, email, password, self.pull_data)
            self.pull_clients.append(pull)
            return pull
        if self.mfa_at_push and not self.push_clients:
            self.server.token_login_errors.append(_chain_rejected_token_error())
            self.server.credential_outcomes.append("mfa")
        push = PushFakeGarmin(self.server, email, password, self.calendar)
        self.push_clients.append(push)
        return push


@dataclass
class ChatFailingAdapter(QueuedReplyAdapter):
    """Answers plan requests from the queue; every chat request is unreachable."""

    def complete(
        self, job: LLMJob, *, system: str, turns: Sequence[ChatTurn]
    ) -> LLMReply:
        if job is LLMJob.CHAT:
            raise LLMUnavailableError("fake provider is unreachable")
        return super().complete(job, system=system, turns=turns)
