"""Instruction file writers, loaders and the fixed clock for coach instruction tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from training_season.coach_instruction import (
    COACH_INSTRUCTION_PATH_ENV,
    CoachInstruction,
    load_coach_instruction,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_NOW = datetime(2026, 3, 2, 2, 30, tzinfo=UTC)
_ZONE = ZoneInfo("America/Sao_Paulo")
_TODAY = _NOW.astimezone(_ZONE).date()


def _clear_coach_instruction_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(COACH_INSTRUCTION_PATH_ENV, raising=False)


def _write_instruction(path: Path, content: str | bytes) -> Path:
    instruction_bytes = content.encode("utf-8") if isinstance(content, str) else content
    path.write_bytes(instruction_bytes)
    return path


def _load_path(path: Path) -> CoachInstruction:
    return load_coach_instruction({COACH_INSTRUCTION_PATH_ENV: str(path)})


def _load_text(tmp_path: Path, text: str) -> CoachInstruction:
    return _load_path(_write_instruction(tmp_path / "coach.md", text))


def _render(
    instruction: CoachInstruction,
    *,
    now: datetime = _NOW,
    zone: ZoneInfo = _ZONE,
    athlete_name: str | None = None,
    race_date: date | None = None,
) -> str:
    return instruction.render(
        now=now,
        zone=zone,
        athlete_name=athlete_name,
        race_date=race_date,
    )
