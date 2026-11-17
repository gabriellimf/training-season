"""`GET /api/plan`: the plan cards as JSON, mapped from `plan_weeks`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from garmin_push_fakes import week_text
from schema_rows import _insert_macro, _insert_weekly_plan
from web_fakes import build_web_app, web_database

if TYPE_CHECKING:
    from pathlib import Path


def test_plan_without_a_macro_has_no_weeks(tmp_path: Path) -> None:
    with TestClient(build_web_app(tmp_path)) as client:
        body = client.get("/api/plan").json()

    assert body == {"today": "2026-11-16", "weeks": []}


def test_plan_card_is_the_stored_week_as_json(tmp_path: Path) -> None:
    app = build_web_app(tmp_path)
    with web_database(tmp_path) as connection:
        macro_id = _insert_macro(
            connection, start_date="2026-11-16", race_date="2027-04-18"
        )
        _insert_weekly_plan(
            connection,
            macro_id,
            week_start="2026-11-16",
            week_end="2026-11-22",
            raw_json=week_text(["2026-11-17"], week_start="2026-11-16"),
        )
        connection.commit()

    with TestClient(app) as client:
        weeks = client.get("/api/plan").json()["weeks"]

    assert [(w["week_start"], w["week_end"]) for w in weeks] == [
        ("2026-11-16", "2026-11-22")
    ]
    card = weeks[0]
    assert card["heading"].startswith("Treinos da semana de 16/11 a 22/11")
    assert card["volume_line"].startswith("Volume planejado:")
    workout = card["workouts"][0]
    assert (workout["date"], workout["weekday"], workout["scheduled"]) == (
        "2026-11-17",
        "ter",
        False,
    )
    assert workout["intensity"] in {"easy", "hard"}
    assert all(isinstance(step["text"], str) for step in workout["steps"])
