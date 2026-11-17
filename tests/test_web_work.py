"""The single-flight coach lock and the in-memory check-in slot."""

import pytest

from training_season.checkin_chat import CheckinConversation
from training_season.web_responses import error_response
from training_season.web_work import CheckinSlot, CoachBusyError, CoachJobRun, CoachWork
from web_fakes import FixedClock


def test_a_second_job_is_refused_at_once_while_one_runs() -> None:
    work = CoachWork(FixedClock())

    with work.run("onboarding_turn") as running:
        assert running == CoachJobRun("onboarding_turn", "2026-11-16T20:00:00Z")
        assert work.current() == running
        with pytest.raises(CoachBusyError, match="week_one"), work.run("week_one"):
            pass

    assert work.current() is None


def test_a_failed_job_frees_the_lock() -> None:
    work = CoachWork(FixedClock())

    with pytest.raises(RuntimeError), work.run("garmin_mfa"):
        raise RuntimeError("Garmin call failed")

    with work.run("garmin_login"):
        assert work.current() == CoachJobRun("garmin_login", "2026-11-16T20:00:00Z")


def test_checkin_slot_holds_the_last_conversation_until_cleared() -> None:
    slot = CheckinSlot()
    conversation = CheckinConversation(7, "system", "kickoff")

    slot.replace(conversation)
    assert slot.current() is conversation
    slot.replace(None)
    assert slot.current() is None


def test_error_response_is_the_status_and_message_body() -> None:
    response = error_response(409, "conflict", "Nenhum plano ativo.")

    assert response.status_code == 409
    assert response.body == b'{"status":"conflict","message":"Nenhum plano ativo."}'
