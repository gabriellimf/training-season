"""Onboarding routes: session, turns, discard and week 1 on the chat page."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from garmin_auth_fakes import _write_fake_token
from garmin_push_fakes import FakeGarminCalendar, push_factory_for, week_text
from llm_fakes import QueuedReplyAdapter, UnavailableAdapter
from onboarding_rows import macro_text, result_text
from schema_rows import _insert_macro
from training_season.garmin_auth import GARMIN_EMAIL_ENV, GARMIN_PASSWORD_ENV
from training_season.onboarding_chat import (
    FIRST_INTRO,
    NO_OPEN_SESSION_TEXT,
    REOPEN_INTRO,
)
from training_season.onboarding_store import (
    append_exchange,
    open_session,
    session_messages,
)
from training_season.onboarding_turn import MAX_SESSION_HISTORY_LENGTH
from training_season.push_store import upsert_weekly_plan
from training_season.push_week import push_week_from_text
from training_season.web_onboarding_routes import (
    ACTIVE_PLAN_PAGE_TEXT,
    DISCARDED_PAGE_TEXT,
    MODEL_FAILED_PAGE_TEXT,
    NO_ACTIVE_PLAN_PAGE_TEXT,
    ONBOARDING_OPEN_PAGE_TEXT,
    SESSION_TOO_LONG_PAGE_TEXT,
    WEEK_ONE_SENT_PAGE_TEXT,
)
from training_season.web_responses import BUSY_TEXT, INVALID_REQUEST_TEXT
from web_fakes import (
    adapter_factory_for,
    build_web_app,
    context_of,
    web_database,
    web_environ,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from fastapi import FastAPI

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory
    from training_season.llm import LLMAdapter

_TODAY = date(2026, 11, 16)
_PRIVATE_TEXT = "segredo-do-atleta-4417"


def _app(tmp_path: Path, adapter: LLMAdapter | None = None) -> FastAPI:
    replies = adapter or QueuedReplyAdapter([])
    return build_web_app(tmp_path, adapter_factory=adapter_factory_for(replies))


def _seed_macro(tmp_path: Path) -> None:
    with web_database(tmp_path) as connection:
        _insert_macro(connection, start_date="2026-11-16", race_date="2027-04-18")


def _post(app: FastAPI, path: str, body: dict[str, Any] | None = None) -> Any:
    with TestClient(app) as client:
        return client.post(path, json={} if body is None else body)


def _stored_turns(tmp_path: Path) -> list[tuple[str, str]]:
    with web_database(tmp_path) as connection:
        rows = connection.execute("SELECT role, content FROM message ORDER BY id")
        return [(str(role), str(content)) for role, content in rows]


def test_onboarding_without_a_session_shows_the_first_intro(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        body = client.get("/api/onboarding").json()

    assert body == {
        "session_id": None,
        "reopening": False,
        "intro": FIRST_INTRO,
        "messages": [],
    }


def test_session_opens_once_and_then_resumes(tmp_path: Path) -> None:
    app = _app(tmp_path)

    first = _post(app, "/api/onboarding/session", {"reopening": False}).json()
    again = _post(app, "/api/onboarding/session", {"reopening": False}).json()

    assert first["session_id"] == again["session_id"] == 1


def test_reopening_session_uses_the_reopen_intro(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _seed_macro(tmp_path)

    body = _post(app, "/api/onboarding/session", {"reopening": True}).json()

    assert (body["session_id"], body["reopening"], body["intro"]) == (
        1,
        True,
        REOPEN_INTRO,
    )


@pytest.mark.parametrize(
    ("has_macro", "reopening", "text"),
    [(True, False, ACTIVE_PLAN_PAGE_TEXT), (False, True, NO_ACTIVE_PLAN_PAGE_TEXT)],
    ids=["active_plan", "no_active_plan"],
)
def test_session_that_does_not_fit_the_plan_state_is_a_conflict(
    tmp_path: Path, has_macro: bool, reopening: bool, text: str
) -> None:
    app = _app(tmp_path)
    if has_macro:
        _seed_macro(tmp_path)

    response = _post(app, "/api/onboarding/session", {"reopening": reopening})

    assert response.status_code == 409
    assert response.json() == {"status": "conflict", "message": text}


def test_turn_is_stored_and_comes_back_with_the_session(tmp_path: Path) -> None:
    app = _app(tmp_path, QueuedReplyAdapter(["Qual é a sua prova?"]))
    _post(app, "/api/onboarding/session", {"reopening": False})

    reply = _post(app, "/api/onboarding/messages", {"text": " Quero correr \x07"})
    with TestClient(app) as client:
        session = client.get("/api/onboarding").json()

    assert reply.json() == {
        "status": "asking",
        "message": "Qual é a sua prova?",
        "macro_plan_id": None,
    }
    assert session["messages"] == [
        {"role": "user", "content": "Quero correr"},
        {"role": "assistant", "content": "Qual é a sua prova?"},
    ]


def test_completed_turn_returns_the_new_plan(tmp_path: Path) -> None:
    replies = [
        f"Montando seu plano.\n<onboarding_result>{result_text()}</onboarding_result>",
        macro_text(start_date=_TODAY),
    ]
    app = _app(tmp_path, QueuedReplyAdapter(replies))
    _post(app, "/api/onboarding/session", {"reopening": False})

    body = _post(app, "/api/onboarding/messages", {"text": "Pode montar"}).json()

    assert body == {
        "status": "completed",
        "message": "Montando seu plano.",
        "macro_plan_id": 1,
    }


def test_model_failure_stores_nothing_and_logs_no_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = _app(tmp_path, UnavailableAdapter())
    _post(app, "/api/onboarding/session", {"reopening": False})
    caplog.set_level(logging.DEBUG)

    body = _post(app, "/api/onboarding/messages", {"text": _PRIVATE_TEXT}).json()

    assert body == {
        "status": "model_failed",
        "message": MODEL_FAILED_PAGE_TEXT,
        "macro_plan_id": None,
    }
    assert _stored_turns(tmp_path) == []
    assert "onboarding turn failed: LLMUnavailableError" in caplog.text
    assert _PRIVATE_TEXT not in caplog.text


def _seed_long_session(connection: sqlite3.Connection) -> None:
    session_id = open_session(connection)
    append_exchange(connection, session_id, "Oi", "x" * MAX_SESSION_HISTORY_LENGTH)


def test_session_too_long_gets_the_page_text(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with web_database(tmp_path) as connection:
        _seed_long_session(connection)

    body = _post(app, "/api/onboarding/messages", {"text": "Mais uma"}).json()

    assert (body["status"], body["message"]) == (
        "session_too_long",
        SESSION_TOO_LONG_PAGE_TEXT,
    )


def test_message_without_an_open_session_is_a_conflict(tmp_path: Path) -> None:
    response = _post(_app(tmp_path), "/api/onboarding/messages", {"text": "Oi"})

    assert response.status_code == 409
    assert response.json() == {"status": "conflict", "message": NO_OPEN_SESSION_TEXT}


@pytest.mark.parametrize("text", ["   ", "\x07\x1b", "x" * 16_001])
def test_blank_or_huge_text_is_invalid(tmp_path: Path, text: str) -> None:
    app = _app(tmp_path)
    _post(app, "/api/onboarding/session", {"reopening": False})

    response = _post(app, "/api/onboarding/messages", {"text": text})

    assert response.status_code == 422
    assert response.json() == {"status": "invalid", "message": INVALID_REQUEST_TEXT}
    assert _stored_turns(tmp_path) == []


def test_message_while_the_coach_works_is_busy(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _post(app, "/api/onboarding/session", {"reopening": False})

    with context_of(app).work.run("week_one"):
        response = _post(app, "/api/onboarding/messages", {"text": "Oi"})

    assert response.status_code == 409
    assert response.json() == {"status": "busy", "message": BUSY_TEXT}


def test_discard_closes_the_session_once(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _post(app, "/api/onboarding/session", {"reopening": False})

    first = _post(app, "/api/onboarding/discard")
    second = _post(app, "/api/onboarding/discard")

    assert first.json() == {"status": "discarded", "message": DISCARDED_PAGE_TEXT}
    assert second.status_code == 409
    with web_database(tmp_path) as connection:
        assert session_messages(connection, 1) == ()


def _week_one_app(
    tmp_path: Path,
    adapter: LLMAdapter,
    factory: GarminFactory,
    environ: dict[str, str] | None = None,
    *,
    with_macro: bool = True,
) -> FastAPI:
    app = build_web_app(
        tmp_path,
        environ=environ,
        adapter_factory=adapter_factory_for(adapter),
        garmin_factory=factory,
    )
    if with_macro:
        _seed_macro(tmp_path)
    return app


def _cycle_messages(tmp_path: Path) -> list[tuple[str, int | None]]:
    with web_database(tmp_path) as connection:
        rows = connection.execute(
            "SELECT content, weekly_plan_id FROM message ORDER BY id"
        ).fetchall()
    return [(str(content), week_id) for content, week_id in rows]


def test_week_one_push_is_stored_with_its_week(
    tmp_path: Path, auth_config: GarminAuthConfig, fake_server: FakeGarminServer
) -> None:
    _write_fake_token(auth_config)
    factory, _ = push_factory_for(fake_server, FakeGarminCalendar())
    reply = week_text(["2026-11-17", "2026-11-19"], week_start="2026-11-16")
    app = _week_one_app(tmp_path, QueuedReplyAdapter([reply]), factory)

    body = _post(app, "/api/week-one").json()

    assert body["status"] == "pushed"
    assert body["message"].startswith("Semana de 16/11 enviada ao Garmin.")
    assert _cycle_messages(tmp_path) == [(body["message"], 1)]


def test_week_one_with_no_workouts_is_empty(
    tmp_path: Path, auth_config: GarminAuthConfig, fake_server: FakeGarminServer
) -> None:
    _write_fake_token(auth_config)
    factory, _ = push_factory_for(fake_server, FakeGarminCalendar())
    reply = week_text([], week_start="2026-11-16")
    app = _week_one_app(tmp_path, QueuedReplyAdapter([reply]), factory)

    assert _post(app, "/api/week-one").json()["status"] == "empty"


def test_rejected_week_one_shows_no_model_text(
    tmp_path: Path, fake_server: FakeGarminServer
) -> None:
    factory, clients = push_factory_for(fake_server, FakeGarminCalendar())
    replies = QueuedReplyAdapter([_PRIVATE_TEXT, _PRIVATE_TEXT])
    app = _week_one_app(tmp_path, replies, factory)

    body = _post(app, "/api/week-one").json()

    assert body["status"] == "rejected"
    assert _PRIVATE_TEXT not in body["message"]
    assert _cycle_messages(tmp_path) == [(body["message"], None)]
    assert clients == []


def test_week_one_that_needs_a_code_says_so(
    tmp_path: Path, fake_server: FakeGarminServer
) -> None:
    fake_server.credential_outcomes.append("mfa")
    factory, _ = push_factory_for(fake_server, FakeGarminCalendar())
    environ = {
        **web_environ(tmp_path),
        GARMIN_EMAIL_ENV: "runner@example.test",
        GARMIN_PASSWORD_ENV: "not-a-real-password",
    }
    reply = week_text(["2026-11-17"], week_start="2026-11-16")
    app = _week_one_app(tmp_path, QueuedReplyAdapter([reply]), factory, environ)

    body = _post(app, "/api/week-one").json()
    with TestClient(app) as client:
        garmin = client.get("/api/state").json()["garmin"]

    assert body == {
        "status": "mfa_required",
        "message": (
            "A semana 1 não foi enviada: o Garmin pede um código MFA para o login."
        ),
    }
    assert garmin["state"] == "mfa_pending"


def test_week_one_model_failure_is_stored(
    tmp_path: Path, fake_server: FakeGarminServer
) -> None:
    factory, _ = push_factory_for(fake_server, FakeGarminCalendar())
    app = _week_one_app(tmp_path, UnavailableAdapter(), factory)

    body = _post(app, "/api/week-one").json()

    assert body == {
        "status": "failed",
        "message": "A semana 1 não foi enviada: o modelo não respondeu como esperado.",
    }
    assert _cycle_messages(tmp_path) == [(body["message"], None)]


def test_week_one_without_a_plan_is_a_conflict_and_stores_nothing(
    tmp_path: Path, fake_server: FakeGarminServer
) -> None:
    factory, _ = push_factory_for(fake_server, FakeGarminCalendar())
    app = _week_one_app(tmp_path, QueuedReplyAdapter([]), factory, with_macro=False)

    response = _post(app, "/api/week-one")

    assert response.status_code == 409
    assert response.json()["message"] == NO_ACTIVE_PLAN_PAGE_TEXT
    assert _cycle_messages(tmp_path) == []


def test_week_one_after_a_stored_week_is_a_conflict_with_no_model_call(
    tmp_path: Path, fake_server: FakeGarminServer
) -> None:
    factory, clients = push_factory_for(fake_server, FakeGarminCalendar())
    app = _week_one_app(tmp_path, QueuedReplyAdapter([]), factory)
    with web_database(tmp_path) as connection:
        week = push_week_from_text(week_text([], week_start="2026-11-16"))
        upsert_weekly_plan(connection, 1, week)

    response = _post(app, "/api/week-one")

    assert response.status_code == 409
    assert response.json() == {"status": "conflict", "message": WEEK_ONE_SENT_PAGE_TEXT}
    assert _cycle_messages(tmp_path) == []
    assert clients == []


def test_week_one_during_an_open_session_is_a_conflict(
    tmp_path: Path, fake_server: FakeGarminServer
) -> None:
    factory, clients = push_factory_for(fake_server, FakeGarminCalendar())
    app = _week_one_app(tmp_path, QueuedReplyAdapter([]), factory)
    _post(app, "/api/onboarding/session", {"reopening": True})

    response = _post(app, "/api/week-one")

    assert response.status_code == 409
    assert response.json()["message"] == ONBOARDING_OPEN_PAGE_TEXT
    assert clients == []
