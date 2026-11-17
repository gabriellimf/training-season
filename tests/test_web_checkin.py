"""Check-in routes: the stateless questions, the saved row and the chat in memory."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from llm_fakes import QueuedReplyAdapter, UnavailableAdapter
from training_season.checkin_chat import CHECKIN_INTRO
from training_season.checkin_questions import CHECKIN_SAVED_TEXT
from training_season.web_checkin_routes import CHECKIN_ENDED_TEXT, NO_OPEN_CHECKIN_TEXT
from training_season.web_responses import BUSY_TEXT
from web_fakes import adapter_factory_for, build_web_app, context_of, web_database

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from training_season.llm import LLMAdapter

_PAIN_ANSWERS = ["6", "s", "joelho direito", "3"]


def _app(tmp_path: Path, adapter: LLMAdapter | None = None) -> FastAPI:
    replies = adapter or QueuedReplyAdapter([])
    return build_web_app(tmp_path, adapter_factory=adapter_factory_for(replies))


def _post(app: FastAPI, path: str, body: dict[str, Any] | None = None) -> Any:
    with TestClient(app) as client:
        return client.post(path, json={} if body is None else body)


def _checkin(app: FastAPI) -> Any:
    with TestClient(app) as client:
        return client.get("/api/checkin").json()


def _stored_checkins(tmp_path: Path) -> list[tuple[Any, ...]]:
    with web_database(tmp_path) as connection:
        return connection.execute(
            "SELECT week_start, fatigue, has_pain, pain_location, pain_level, "
            "free_text FROM checkin"
        ).fetchall()


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (
            [],
            ("question", "Como está sua fadiga hoje, de 1 a 10?", None, 0, "scale"),
        ),
        (
            ["7", "x"],
            ("hint", "Algo está doendo? (s/n)", "Responda s ou n.", 1, "yes_no"),
        ),
        (["7", "s"], ("question", "Onde dói?", None, 2, "text")),
        (
            ["11"],
            (
                "hint",
                "Como está sua fadiga hoje, de 1 a 10?",
                "Responda com um número inteiro de 1 a 10.",
                0,
                "scale",
            ),
        ),
    ],
    ids=["first", "bad_yes_no", "location", "bad_scale"],
)
def test_answers_give_the_next_question_or_the_hint(
    tmp_path: Path, answers: list[str], expected: tuple[object, ...]
) -> None:
    body = _post(_app(tmp_path), "/api/checkin/answers", {"answers": answers}).json()

    status, question, hint, accepted, kind = expected
    assert (body["status"], body["question"], body["accepted"]) == (
        status,
        question,
        accepted,
    )
    assert body["answer_kind"] == kind
    assert body["hint"] == hint
    assert body["message"] is None
    assert _stored_checkins(tmp_path) == []


def test_last_answer_saves_the_checkin_and_opens_the_chat(tmp_path: Path) -> None:
    app = _app(tmp_path)

    body = _post(app, "/api/checkin/answers", {"answers": _PAIN_ANSWERS}).json()

    assert body == {
        "status": "saved",
        "question": None,
        "hint": None,
        "accepted": 4,
        "answer_kind": None,
        "message": "Check-in salvo.",
    }
    assert _stored_checkins(tmp_path) == [
        ("2026-11-16", 6, 1, "joelho direito", 3, None)
    ]
    assert _checkin(app) == {
        "open": True,
        "intro": CHECKIN_INTRO,
        "saved_message": CHECKIN_SAVED_TEXT,
        "messages": [],
    }


def test_closed_checkin_still_carries_the_intro(tmp_path: Path) -> None:
    assert _checkin(_app(tmp_path)) == {
        "open": False,
        "intro": CHECKIN_INTRO,
        "saved_message": None,
        "messages": [],
    }


@pytest.mark.parametrize(
    "answers", [["6", "s", "joelho", "3", "extra"], ["x" * 401]], ids=["five", "long"]
)
def test_too_many_or_too_long_answers_are_invalid(
    tmp_path: Path, answers: list[str]
) -> None:
    response = _post(_app(tmp_path), "/api/checkin/answers", {"answers": answers})

    assert response.status_code == 422


def test_reply_grows_the_chat_and_the_free_text(tmp_path: Path) -> None:
    app = _app(tmp_path, QueuedReplyAdapter(["Descanse hoje."]))
    _post(app, "/api/checkin/answers", {"answers": ["5", "n"]})

    body = _post(app, "/api/checkin/messages", {"text": "Dormi mal"}).json()

    assert body == {"status": "replied", "message": "Descanse hoje."}
    assert _checkin(app)["messages"] == [
        {"role": "user", "content": "Dormi mal"},
        {"role": "assistant", "content": "Descanse hoje."},
    ]
    assert _stored_checkins(tmp_path)[0][-1] == "Dormi mal"


def test_model_failure_ends_the_chat_and_keeps_the_message(tmp_path: Path) -> None:
    app = _app(tmp_path, UnavailableAdapter())
    _post(app, "/api/checkin/answers", {"answers": ["5", "n"]})

    body = _post(app, "/api/checkin/messages", {"text": "Dormi mal"}).json()

    assert body["status"] == "model_failed"
    assert _checkin(app)["open"] is False
    assert _stored_checkins(tmp_path)[0][-1] == "Dormi mal"


def test_message_without_an_open_checkin_is_not_open(tmp_path: Path) -> None:
    body = _post(_app(tmp_path), "/api/checkin/messages", {"text": "Oi"}).json()

    assert body == {"status": "not_open", "message": NO_OPEN_CHECKIN_TEXT}


def test_end_closes_the_chat(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _post(app, "/api/checkin/answers", {"answers": ["5", "n"]})

    body = _post(app, "/api/checkin/end").json()

    assert body == {"status": "ended", "message": CHECKIN_ENDED_TEXT}
    assert _checkin(app)["open"] is False


def test_save_while_the_coach_works_is_busy_and_stores_nothing(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)

    with context_of(app).work.run("onboarding_turn"):
        response = _post(app, "/api/checkin/answers", {"answers": ["5", "n"]})

    assert response.status_code == 409
    assert response.json() == {"status": "busy", "message": BUSY_TEXT}
    assert _stored_checkins(tmp_path) == []
