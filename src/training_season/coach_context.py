"""
Assemble the coach context for one target week from SQLite, read only.

The last four weeks are in full detail. Older weeks are one totals line each, and
only back to the window that holds the macro start. The running athlete summary
carries everything older, so the size stays flat across a long block.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

from training_season.coach_context_lines import (
    activity_line,
    checkin_text,
    laps_line,
    week_totals_text,
    wellness_line,
)
from training_season.coach_context_store import (
    read_active_macro,
    read_activities_between,
    read_latest_athlete_summary,
    read_latest_checkin_before,
    read_profile_json,
    read_week_totals,
    read_weekly_plans,
    read_wellness_between,
)
from training_season.coach_instruction import weeks_to_race
from training_season.garmin_pull_base import canonical_json
from training_season.push_week import WEEK_DAYS

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Sequence

    from training_season.coach_context_store import (
        ActiveMacroRow,
        ContextActivityRow,
        ContextCheckinRow,
        ContextWellnessRow,
        WeekSportTotals,
    )

RECENT_WEEK_COUNT = 4
NO_MACRO_PLAN = "Sem plano macro ativo."
NO_ATHLETE_SUMMARY = "Ainda não há resumo."
NO_OLDER_WEEKS = "Nenhuma semana anterior."
NO_PROFILE = "Perfil ainda não preenchido."
NO_CHECKIN = "Nenhum check-in registrado."
NO_WEEK_PLAN = "sem plano"
NO_ACTIVITIES = "sem atividades"
NO_WELLNESS_DAYS = "sem dados"

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CoachContext:
    """One body per section; `text` joins them under fixed Portuguese headings."""

    target_week_start: date
    macro_plan: str = field(repr=False)
    athlete_summary: str = field(repr=False)
    recent_weeks: str = field(repr=False)
    older_weeks: str = field(repr=False)
    profile: str = field(repr=False)
    checkin: str = field(repr=False)

    @property
    def text(self) -> str:
        """The whole context, the same bytes for the same database: `context.text`."""
        sections = (
            ("Plano macro", self.macro_plan),
            ("Resumo do atleta", self.athlete_summary),
            ("Últimas quatro semanas", self.recent_weeks),
            ("Semanas anteriores", self.older_weeks),
            ("Perfil", self.profile),
            ("Check-in", self.checkin),
        )
        return "\n\n".join(f"## {heading}\n{body}" for heading, body in sections) + "\n"


@dataclass(frozen=True, slots=True)
class WeekWindow:
    """The 7 days `weeks_back` windows before the target week."""

    weeks_back: int
    first_day: date

    @property
    def last_day(self) -> date:
        """Sixth day after the first: `window.last_day`."""
        return self.first_day + timedelta(days=WEEK_DAYS - 1)

    @property
    def label(self) -> str:
        """Both dates for a heading: `window.label` -> `2026-10-26 a 2026-11-01`."""
        return f"{self.first_day.isoformat()} a {self.last_day.isoformat()}"


def week_window(target_week_start: date, weeks_back: int) -> WeekWindow:
    """
    The window that starts 7 x `weeks_back` days before the target week.

    Used as `week_window(date(2026, 11, 2), 1)` -> 2026-10-26 .. 2026-11-01
    """
    first_day = target_week_start - timedelta(days=WEEK_DAYS * weeks_back)
    return WeekWindow(weeks_back, first_day)


def weeks_back_of(target_week_start: date, day: date) -> int:
    """
    The window number that holds `day`: 1 for the 7 days before the target week.

    Used as `weeks_back_of(date(2026, 11, 2), date(2026, 10, 26))` -> 1
    """
    return ((target_week_start - day).days + WEEK_DAYS - 1) // WEEK_DAYS


def _macro_floor(target_week_start: date, macro: ActiveMacroRow | None) -> int:
    if macro is None:
        return 0
    start = date.fromisoformat(macro.start_date)
    return max(0, weeks_back_of(target_week_start, start))


def _canonical(stored_json: str) -> str:
    return canonical_json(json.loads(stored_json))


def _macro_section(macro: ActiveMacroRow | None) -> str:
    if macro is None:
        return NO_MACRO_PLAN
    dates = f"Início: {macro.start_date}. Prova: {macro.race_date}."
    return f"{dates}\n{_canonical(macro.plan_json)}"


def _group_by_window[Row](
    target_week_start: date, rows: Iterable[Row], day_of: Callable[[Row], str]
) -> dict[int, list[Row]]:
    grouped: dict[int, list[Row]] = {}
    for row in rows:
        day = date.fromisoformat(day_of(row))
        grouped.setdefault(weeks_back_of(target_week_start, day), []).append(row)
    return grouped


def _activity_lines(activities: Sequence[ContextActivityRow]) -> list[str]:
    lines: list[str] = []
    for activity in activities:
        lines.append(activity_line(activity))
        laps = laps_line(activity.splits_json)
        if laps is not None:
            lines.append(laps)
    return lines


def _week_block(
    window: WeekWindow,
    plan_json: str | None,
    activities: Sequence[ContextActivityRow],
    wellness: Sequence[ContextWellnessRow],
) -> str:
    plan = NO_WEEK_PLAN if plan_json is None else _canonical(plan_json)
    lines = [f"### {window.label}", f"Plano: {plan}", "Atividades:"]
    lines.extend(_activity_lines(activities) or [NO_ACTIVITIES])
    lines.append("Bem-estar:")
    lines.extend([wellness_line(day) for day in wellness] or [NO_WELLNESS_DAYS])
    return "\n".join(lines)


def _recent_plans(
    connection: sqlite3.Connection,
    target: date,
    macro: ActiveMacroRow | None,
    windows: Sequence[WeekWindow],
) -> dict[int, str]:
    if macro is None:
        return {}
    first, last = windows[0].first_day, windows[-1].last_day
    rows = read_weekly_plans(connection, macro.macro_plan_id, first, last)
    by_window = _group_by_window(target, rows, lambda row: row.week_start)
    return {back: plans[-1].raw_json for back, plans in by_window.items()}


def _recent_weeks_section(
    connection: sqlite3.Connection, target: date, macro: ActiveMacroRow | None
) -> str:
    windows = [week_window(target, back) for back in range(RECENT_WEEK_COUNT, 0, -1)]
    first, last = windows[0].first_day, windows[-1].last_day
    plans = _recent_plans(connection, target, macro, windows)
    activity_rows = read_activities_between(connection, first, last)
    activities = _group_by_window(target, activity_rows, lambda row: row.activity_date)
    wellness_rows = read_wellness_between(connection, first, last)
    wellness = _group_by_window(target, wellness_rows, lambda row: row.wellness_date)
    blocks = (
        _week_block(
            window,
            plans.get(window.weeks_back),
            activities.get(window.weeks_back, []),
            wellness.get(window.weeks_back, []),
        )
        for window in windows
    )
    return "\n\n".join(blocks)


def _older_weeks_section(
    connection: sqlite3.Connection, target: date, macro: ActiveMacroRow | None
) -> str:
    floor_weeks_back = _macro_floor(target, macro)
    if macro is None or floor_weeks_back <= RECENT_WEEK_COUNT:
        return NO_OLDER_WEEKS
    oldest = week_window(target, floor_weeks_back)
    first = max(oldest.first_day, date.fromisoformat(macro.start_date))
    newest = week_window(target, RECENT_WEEK_COUNT + 1)
    totals = read_week_totals(connection, target, first, newest.last_day)
    by_window: dict[int, list[WeekSportTotals]] = {}
    for row in totals:
        by_window.setdefault(row.weeks_back, []).append(row)
    windows = range(floor_weeks_back, RECENT_WEEK_COUNT, -1)
    return "\n".join(
        f"- {week_window(target, back).label}: "
        f"{week_totals_text(by_window.get(back, []))}"
        for back in windows
    )


def _silent_weeks(
    checkin: ContextCheckinRow | None, target: date, floor_weeks_back: int
) -> int:
    if checkin is None:
        return floor_weeks_back
    since = weeks_back_of(target, date.fromisoformat(checkin.week_start)) - 1
    return min(since, floor_weeks_back)


def _silence_text(silent_weeks: int) -> str:
    if silent_weeks == 1:
        return "Sem check-in há 1 semana."
    return f"Sem check-in há {silent_weeks} semanas seguidas."


def _checkin_section(
    connection: sqlite3.Connection, target: date, macro: ActiveMacroRow | None
) -> str:
    checkin = read_latest_checkin_before(connection, target)
    lines = [NO_CHECKIN if checkin is None else checkin_text(checkin)]
    silent = _silent_weeks(checkin, target, _macro_floor(target, macro))
    if silent > 0:
        lines.append(_silence_text(silent))
    if macro is not None:
        remaining = weeks_to_race(target, date.fromisoformat(macro.race_date))
        lines.append(f"Semanas até a prova: {remaining}")
    return "\n".join(lines)


def _profile_section(profile_json: str | None) -> str:
    return NO_PROFILE if profile_json is None else _canonical(profile_json)


def assemble_coach_context(
    connection: sqlite3.Connection, target_week_start: date
) -> CoachContext:
    """
    Read the context for the week the coach will plan; never fails on empty tables.

    Used as `assemble_coach_context(connection, date(2026, 11, 2)).text`
    """
    macro = read_active_macro(connection)
    floor_weeks_back = _macro_floor(target_week_start, macro)
    context = CoachContext(
        target_week_start=target_week_start,
        macro_plan=_macro_section(macro),
        athlete_summary=read_latest_athlete_summary(connection) or NO_ATHLETE_SUMMARY,
        recent_weeks=_recent_weeks_section(connection, target_week_start, macro),
        older_weeks=_older_weeks_section(connection, target_week_start, macro),
        profile=_profile_section(read_profile_json(connection)),
        checkin=_checkin_section(connection, target_week_start, macro),
    )
    _LOGGER.info(
        "coach context for week %s: %d older weeks, %d characters",
        target_week_start.isoformat(),
        max(0, floor_weeks_back - RECENT_WEEK_COUNT),
        len(context.text),
    )
    return context
