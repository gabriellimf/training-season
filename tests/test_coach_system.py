"""Tests for the coach system text rendered from the stored profile and macro."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from coach_instruction_helpers import _NOW, _ZONE, _load_text
from onboarding_rows import CLOSING_EXCHANGE, valid_macro_plan, valid_result
from training_season.coach_system import StoredProfileError, render_coach_system
from training_season.macro_store import store_onboarding_outcome
from training_season.onboarding_store import open_session

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

_TEMPLATE = "Atleta: $athlete_name. Prova: $race_date."


def render(connection: sqlite3.Connection, tmp_path: Path) -> str:
    instruction = _load_text(tmp_path, _TEMPLATE)
    return render_coach_system(instruction, connection, now=_NOW, zone=_ZONE)


def test_no_profile_and_no_macro_render_dashes(
    migrated_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    assert render(migrated_connection, tmp_path) == "Atleta: -. Prova: -."


def test_stored_profile_and_macro_render_name_and_race_date(
    migrated_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    store_onboarding_outcome(
        migrated_connection,
        session_id=open_session(migrated_connection),
        result=valid_result(),
        plan=valid_macro_plan(),
        final_exchange=CLOSING_EXCHANGE,
    )

    assert render(migrated_connection, tmp_path) == "Atleta: Gabi. Prova: 2027-04-18."


def test_stored_profile_that_is_not_a_profile_names_the_problem(
    migrated_connection: sqlite3.Connection, tmp_path: Path
) -> None:
    migrated_connection.execute(
        "INSERT INTO profile (id, data_json, created_at, updated_at) "
        "VALUES (1, '{\"name\": \"x\"}', '2026-11-10T12:00:00Z', "
        "'2026-11-10T12:00:00Z')"
    )

    with pytest.raises(StoredProfileError) as raised:
        render(migrated_connection, tmp_path)

    assert "profile.data_json is not an athlete profile" in str(raised.value)
    assert "athlete_name: Field required" in str(raised.value)
