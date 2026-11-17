"""`GET /api/state` per mode, busy job, check-in and Garmin; `GET /api/messages`."""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from garmin_auth_fakes import _begin_mfa, _write_fake_token
from schema_rows import _insert_macro, _insert_weekly_plan
from training_season.checkin_chat import CheckinConversation
from training_season.database import connect
from training_season.onboarding_store import (
    append_exchange,
    open_session,
    write_cycle_message,
)
from web_fakes import build_web_app, context_of

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from fastapi import FastAPI

    from garmin_auth_fakes import FakeGarminServer
    from training_season.garmin_auth import GarminAuthConfig, GarminFactory

type Seed = Callable[[sqlite3.Connection], object]


def _seed(tmp_path: Path, seed: Seed) -> None:
    with closing(connect(tmp_path / "web.db")) as connection:
        seed(connection)


def _macro(connection: sqlite3.Connection) -> int:
    return _insert_macro(connection, start_date="2026-11-16", race_date="2027-03-21")


def _macro_with_week(connection: sqlite3.Connection) -> None:
    _insert_weekly_plan(
        connection, _macro(connection), week_start="2026-11-16", week_end="2026-11-22"
    )


def _state(app: FastAPI) -> dict[str, object]:
    with TestClient(app) as client:
        response = client.get("/api/state")
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def test_empty_database_needs_onboarding(tmp_path: Path) -> None:
    assert _state(build_web_app(tmp_path)) == {
        "mode": "needs_onboarding",
        "reopening": False,
        "has_active_macro": False,
        "checkin_open": False,
        "busy": None,
        "garmin": {"state": "no_token", "mfa_requested_at": None},
        "today": "2026-11-16",
    }


@pytest.mark.parametrize(
    ("seed", "mode", "reopening", "has_active_macro"),
    [
        (open_session, "onboarding", False, False),
        (lambda c: (_macro(c), open_session(c)), "onboarding", True, True),
        (_macro, "week_one_pending", False, True),
        (_macro_with_week, "ready", False, True),
    ],
    ids=["first_onboarding", "reopening", "week_one_pending", "ready"],
)
def test_mode_follows_the_stored_plan_state(
    tmp_path: Path, seed: Seed, mode: str, reopening: bool, has_active_macro: bool
) -> None:
    app = build_web_app(tmp_path)
    _seed(tmp_path, seed)

    state = _state(app)

    assert (state["mode"], state["reopening"], state["has_active_macro"]) == (
        mode,
        reopening,
        has_active_macro,
    )


def test_a_week_outside_the_macro_dates_leaves_week_one_pending(
    tmp_path: Path,
) -> None:
    app = build_web_app(tmp_path)

    def seed(connection: sqlite3.Connection) -> None:
        _insert_weekly_plan(
            connection,
            _macro(connection),
            week_start="2026-11-09",
            week_end="2026-11-15",
        )

    _seed(tmp_path, seed)

    assert _state(app)["mode"] == "week_one_pending"


def test_running_job_and_open_checkin_come_from_memory(tmp_path: Path) -> None:
    app = build_web_app(tmp_path)
    context = context_of(app)
    context.checkin.replace(CheckinConversation(1, "system", "kickoff"))

    with context.work.run("week_one"):
        state = _state(app)

    assert state["busy"] == {"job": "week_one", "started_at": "2026-11-16T20:00:00Z"}
    assert state["checkin_open"] is True
    assert _state(app)["busy"] is None


def test_garmin_state_shows_a_pending_code(
    tmp_path: Path,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
    factory: GarminFactory,
) -> None:
    app = build_web_app(tmp_path)
    _begin_mfa(auth_config, fake_server, factory)

    garmin = _state(app)["garmin"]

    assert isinstance(garmin, dict)
    assert garmin["state"] == "mfa_pending"
    assert isinstance(garmin["mfa_requested_at"], str)


def test_garmin_state_shows_a_cached_token(
    tmp_path: Path, auth_config: GarminAuthConfig
) -> None:
    app = build_web_app(tmp_path)
    _write_fake_token(auth_config)

    assert _state(app)["garmin"] == {"state": "token_cached", "mfa_requested_at": None}


def test_unreadable_garmin_state_shows_no_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = build_web_app(tmp_path)
    (tmp_path / "garmin").mkdir()
    (tmp_path / "garmin" / "garmin_mfa_state.json").write_text("{", encoding="utf-8")

    garmin = _state(app)["garmin"]

    assert garmin == {"state": "no_token", "mfa_requested_at": None}
    assert "Garmin auth state is unreadable: GarminMfaStateInvalidError" in (
        caplog.text
    )


def _seed_history(connection: sqlite3.Connection) -> None:
    session_id = open_session(connection)
    append_exchange(connection, session_id, "Quero correr 21 km", "Quando é a prova?")
    write_cycle_message(connection, "Semana enviada ao Garmin.", None)


def test_messages_are_the_newest_turns_oldest_first(tmp_path: Path) -> None:
    app = build_web_app(tmp_path)
    _seed(tmp_path, _seed_history)

    with TestClient(app) as client:
        response = client.get("/api/messages", params={"limit": 2})

    messages = response.json()["messages"]
    assert [(m["role"], m["content"], m["kind"]) for m in messages] == [
        ("assistant", "Quando é a prova?", "onboarding"),
        ("assistant", "Semana enviada ao Garmin.", "coach"),
    ]
    assert all(m["created_at"].endswith("Z") for m in messages)


def test_messages_without_a_limit_return_the_stored_turns(tmp_path: Path) -> None:
    app = build_web_app(tmp_path)
    _seed(tmp_path, _seed_history)

    with TestClient(app) as client:
        response = client.get("/api/messages")

    assert len(response.json()["messages"]) == 3


@pytest.mark.parametrize("limit", ["0", "201", "x"])
def test_messages_refuse_a_limit_outside_1_to_200(tmp_path: Path, limit: str) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        response = client.get("/api/messages", params={"limit": limit})

    assert response.status_code == 422
