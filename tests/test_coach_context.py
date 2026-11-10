"""Tests for the coach context assembled from SQLite."""

import json
import sqlite3
from datetime import date, timedelta

import pytest

from coach_context_rows import (
    BLOCK_START,
    insert_block_macro,
    insert_full_activity,
    insert_full_wellness,
    insert_profile,
    insert_summary,
    insert_week_checkin,
    run_laps_json,
    seed_training_block,
    week_of_block,
)
from schema_rows import _insert_checkin, _insert_macro, _insert_weekly_plan
from training_season.coach_context import assemble_coach_context
from training_season.coach_context_lines import (
    CHECKIN_TEXT_CUT_SUFFIX,
    MAX_CHECKIN_TEXT_CHARS,
    MAX_CONTEXT_LAPS,
    MAX_PAIN_LOCATION_CHARS,
    checkin_text,
    laps_line,
)
from training_season.coach_context_store import (
    _LATEST_CHECKIN_BEFORE,
    ContextCheckinRow,
)

FLAT_CONTEXT_RATIO = 1.2
_TARGET = date(2026, 11, 2)
_HEADINGS = [
    "## Plano macro",
    "## Resumo do atleta",
    "## Últimas quatro semanas",
    "## Semanas anteriores",
    "## Perfil",
    "## Check-in",
]
_RUN_LINE = (
    "- 2026-10-27 corrida (road_run): 10.00 km, 50:00, 5:00 min/km, "
    "FC média 150, FC máx 172, cadência 170 ppm"
)
_SWIM_LINE = (
    "- 2026-10-31 natação, só leitura, não planejar (lap_swimming): 10.00 km, "
    "50:00, 5:00 min/km, FC média 150, FC máx 172, cadência 170 ppm"
)
_LAP_TEXT = "1.00 km, 5:0{}, 5:03 min/km, FC 150"
_WELLNESS_LINE = (
    "- 2026-10-27: FC repouso 48, sono 7.5 h, Body Battery máx 90, "
    "Body Battery mín 20, estresse 30, VFC 62 ms, VO2max 52"
)


def _statement_count(connection: sqlite3.Connection, target: date) -> int:
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        assemble_coach_context(connection, target)
    finally:
        connection.set_trace_callback(None)
    return len(statements)


def _seed_last_week_before_target(connection: sqlite3.Connection) -> None:
    macro_id = insert_block_macro(connection)
    plan = {"workouts": [], "week_start": "2026-10-26"}
    _insert_weekly_plan(connection, macro_id, raw_json=json.dumps(plan))
    insert_full_activity(
        connection,
        activity_id="run",
        day=date(2026, 10, 27),
        splits_json=run_laps_json(2),
    )
    insert_full_activity(
        connection, activity_id="swim", day=date(2026, 10, 31), sport="swimming"
    )
    insert_full_activity(connection, activity_id="target-day", day=_TARGET)
    insert_full_wellness(connection, date(2026, 10, 27))


def test_empty_database_renders_every_section_with_fixed_lines(
    migrated_connection: sqlite3.Connection,
) -> None:
    context = assemble_coach_context(migrated_connection, _TARGET)

    headings = [line for line in context.text.splitlines() if line.startswith("## ")]
    assert headings == _HEADINGS
    assert context.macro_plan == "Sem plano macro ativo."
    assert context.athlete_summary == "Ainda não há resumo."
    assert context.older_weeks == "Nenhuma semana anterior."
    assert context.profile == "Perfil ainda não preenchido."
    assert context.checkin == "Nenhum check-in registrado."
    assert context.recent_weeks.count("Plano: sem plano") == 4
    assert context.recent_weeks.splitlines()[0] == "### 2026-10-05 a 2026-10-11"


def test_recent_week_shows_plan_activities_laps_and_wellness(
    migrated_connection: sqlite3.Connection,
) -> None:
    _seed_last_week_before_target(migrated_connection)

    context = assemble_coach_context(migrated_connection, _TARGET)

    last_week = context.recent_weeks.split("\n\n")[-1].splitlines()
    assert last_week == [
        "### 2026-10-26 a 2026-11-01",
        'Plano: {"week_start":"2026-10-26","workouts":[]}',
        "Atividades:",
        _RUN_LINE,
        f"  voltas: {_LAP_TEXT.format(1)}; {_LAP_TEXT.format(2)}",
        _SWIM_LINE,
        "Bem-estar:",
        _WELLNESS_LINE,
    ]
    assert "2026-11-02 corrida" not in context.text
    assert "secretRawKey" not in context.text


def test_lap_without_a_known_key_skips_that_value(
    migrated_connection: sqlite3.Connection,
) -> None:
    laps = {"lapDTOs": [{"distance": 400.0, "duration": 90.0, "averageHR": True}]}
    insert_full_activity(
        migrated_connection,
        activity_id="run",
        day=date(2026, 10, 27),
        splits_json=json.dumps(laps),
    )

    context = assemble_coach_context(migrated_connection, _TARGET)

    assert "  voltas: 0.40 km, 1:30\n" in context.recent_weeks


@pytest.mark.parametrize(
    ("lap_count", "is_cut"),
    [(MAX_CONTEXT_LAPS, False), (MAX_CONTEXT_LAPS + 1, True)],
)
def test_laps_line_shows_at_most_the_lap_cap(lap_count: int, is_cut: bool) -> None:
    line = laps_line(run_laps_json(lap_count))

    assert line is not None
    assert line.count(" km,") == MAX_CONTEXT_LAPS
    assert line.endswith("; +1 volta") is is_cut


def _insert_plan_on(
    connection: sqlite3.Connection, macro_id: int, week_start: str
) -> None:
    _insert_weekly_plan(
        connection,
        macro_id,
        week_start=week_start,
        week_end=week_start,
        raw_json=json.dumps({"week_start": week_start}),
    )


def _plan_line_of_last_week(connection: sqlite3.Connection) -> str:
    context = assemble_coach_context(connection, _TARGET)
    return context.recent_weeks.split("\n\n")[-1].splitlines()[1]


def test_plan_shows_in_the_window_that_holds_its_week_start(
    migrated_connection: sqlite3.Connection,
) -> None:
    _insert_plan_on(
        migrated_connection, insert_block_macro(migrated_connection), "2026-10-27"
    )

    plan_line = _plan_line_of_last_week(migrated_connection)

    assert plan_line == 'Plano: {"week_start":"2026-10-27"}'


def test_later_plan_wins_when_two_share_a_window(
    migrated_connection: sqlite3.Connection,
) -> None:
    macro_id = insert_block_macro(migrated_connection)
    for week_start in ("2026-10-28", "2026-10-26"):
        _insert_plan_on(migrated_connection, macro_id, week_start)

    plan_line = _plan_line_of_last_week(migrated_connection)

    assert plan_line == 'Plano: {"week_start":"2026-10-28"}'


def test_older_weeks_skip_days_before_a_mid_window_macro_start(
    migrated_connection: sqlite3.Connection,
) -> None:
    _insert_macro(migrated_connection, start_date="2026-10-03")
    for activity_id, day in (
        ("before", date(2026, 9, 29)),
        ("start", date(2026, 10, 3)),
    ):
        insert_full_activity(migrated_connection, activity_id=activity_id, day=day)

    context = assemble_coach_context(migrated_connection, _TARGET)

    assert context.older_weeks == (
        "- 2026-09-28 a 2026-10-04: corrida 1x, 10.00 km, 50:00; natação 0x, 0.00 km"
    )


def test_older_weeks_are_totals_back_to_the_macro_start(
    migrated_connection: sqlite3.Connection,
) -> None:
    seed_training_block(migrated_connection, 8)
    insert_full_activity(
        migrated_connection,
        activity_id="before-macro",
        day=BLOCK_START - timedelta(days=7),
    )

    context = assemble_coach_context(migrated_connection, week_of_block(8))

    totals = "corrida 5x, 50.00 km, 4:10:00; natação 1x, 10.00 km"
    assert context.older_weeks.splitlines() == [
        f"- 2026-05-04 a 2026-05-10: {totals}",
        f"- 2026-05-11 a 2026-05-17: {totals}",
        f"- 2026-05-18 a 2026-05-24: {totals}",
    ]


def test_latest_summary_profile_and_macro_render_in_their_sections(
    migrated_connection: sqlite3.Connection,
) -> None:
    insert_block_macro(migrated_connection)
    insert_summary(migrated_connection, "Resumo antigo.")
    insert_summary(migrated_connection, "Resumo novo.")
    insert_profile(migrated_connection, '{"name": "Ana", "age": 34}')

    context = assemble_coach_context(migrated_connection, _TARGET)

    assert context.athlete_summary == "Resumo novo."
    assert context.profile == '{"age":34,"name":"Ana"}'
    assert context.macro_plan == "Início: 2026-05-04. Prova: 2026-12-20.\n{}"


def test_latest_checkin_is_the_newest_week_and_silence_counts_from_it(
    migrated_connection: sqlite3.Connection,
) -> None:
    insert_block_macro(migrated_connection)
    insert_week_checkin(
        migrated_connection, week_of_block(5), created_at="2026-06-03T00:00:00Z"
    )
    insert_week_checkin(
        migrated_connection,
        week_of_block(4),
        created_at="2026-06-04T00:00:00Z",
        fatigue=3,
    )

    context = assemble_coach_context(migrated_connection, week_of_block(10))

    assert context.checkin.splitlines() == [
        "Último check-in: semana 2026-06-01, fadiga 5/10, dor: não.",
        "Sem check-in há 4 semanas seguidas.",
        "Semanas até a prova: 23",
    ]


def test_one_silent_week_is_singular(
    migrated_connection: sqlite3.Connection,
) -> None:
    insert_block_macro(migrated_connection)
    insert_week_checkin(migrated_connection, week_of_block(8))

    context = assemble_coach_context(migrated_connection, week_of_block(10))

    assert context.checkin.splitlines()[1] == "Sem check-in há 1 semana."


def test_latest_checkin_query_walks_the_week_start_index_without_a_sort(
    migrated_connection: sqlite3.Connection,
) -> None:
    plan = migrated_connection.execute(
        "EXPLAIN QUERY PLAN " + _LATEST_CHECKIN_BEFORE, ("2026-11-02",)
    ).fetchall()

    assert [str(row[-1]) for row in plan] == [
        "SEARCH checkin USING INDEX checkin_week_start (week_start<?)"
    ]


def test_no_checkin_counts_silence_back_to_the_macro_start(
    migrated_connection: sqlite3.Connection,
) -> None:
    insert_block_macro(migrated_connection)
    insert_week_checkin(migrated_connection, week_of_block(5))

    context = assemble_coach_context(migrated_connection, week_of_block(5))

    assert context.checkin.splitlines() == [
        "Nenhum check-in registrado.",
        "Sem check-in há 4 semanas seguidas.",
        "Semanas até a prova: 28",
    ]


def test_checkin_with_pain_renders_location_level_and_free_text(
    migrated_connection: sqlite3.Connection,
) -> None:
    _insert_checkin(
        migrated_connection,
        fatigue=6,
        has_pain=1,
        pain_location="joelho",
        pain_level=4,
        free_text="doeu na subida",
    )

    context = assemble_coach_context(migrated_connection, _TARGET)

    pain = "dor: sim, local joelho, nível 4/10"
    assert context.checkin.splitlines() == [
        f"Último check-in: semana 2026-10-26, fadiga 6/10, {pain}.",
        "Texto livre: doeu na subida",
    ]


@pytest.mark.parametrize(
    ("free_text_chars", "rendered_chars", "is_cut"),
    [
        (MAX_CHECKIN_TEXT_CHARS, MAX_CHECKIN_TEXT_CHARS, False),
        (MAX_CHECKIN_TEXT_CHARS + 1, MAX_CHECKIN_TEXT_CHARS, True),
    ],
)
def test_checkin_free_text_is_cut_after_the_cap(
    free_text_chars: int, rendered_chars: int, is_cut: bool
) -> None:
    checkin = ContextCheckinRow("2026-10-26", 5, 0, None, None, "x" * free_text_chars)

    free_text_line = checkin_text(checkin).splitlines()[1]

    shown = free_text_line.removeprefix("Texto livre: ")
    assert shown.endswith(CHECKIN_TEXT_CUT_SUFFIX) is is_cut
    assert shown.removesuffix(CHECKIN_TEXT_CUT_SUFFIX) == "x" * rendered_chars


@pytest.mark.parametrize(
    ("location_chars", "is_cut"),
    [(MAX_PAIN_LOCATION_CHARS, False), (MAX_PAIN_LOCATION_CHARS + 1, True)],
)
def test_pain_location_is_cut_after_the_cap(location_chars: int, is_cut: bool) -> None:
    checkin = ContextCheckinRow("2026-10-26", 5, 1, "y" * location_chars, 4, None)

    shown = checkin_text(checkin).split("local ", 1)[1].split(", nível ")[0]

    assert shown.endswith(CHECKIN_TEXT_CUT_SUFFIX) is is_cut
    assert shown.removesuffix(CHECKIN_TEXT_CUT_SUFFIX) == "y" * MAX_PAIN_LOCATION_CHARS


def test_checkin_text_drops_control_characters_and_keeps_one_line_each() -> None:
    location = "jo\x1b[2Jelho\n## Perfil"
    free_text = "doeu\x07 na\r\nsubida\n## Perfil\tfim\x9b"
    checkin = ContextCheckinRow("2026-10-26", 5, 1, location, 4, free_text)

    pain = "dor: sim, local jo[2Jelho ## Perfil, nível 4/10"
    assert checkin_text(checkin).splitlines() == [
        f"Último check-in: semana 2026-10-26, fadiga 5/10, {pain}.",
        "Texto livre: doeu na subida ## Perfil\tfim",
    ]


def test_context_repr_hides_the_section_bodies(
    migrated_connection: sqlite3.Connection,
) -> None:
    insert_summary(migrated_connection, "segredo-do-resumo")

    context = assemble_coach_context(migrated_connection, _TARGET)

    assert repr(context) == "CoachContext(target_week_start=datetime.date(2026, 11, 2))"


def test_context_size_stays_flat_from_week_5_to_week_30(
    migrated_connection: sqlite3.Connection,
) -> None:
    seed_training_block(migrated_connection, 30)
    changes_before = migrated_connection.total_changes

    week_5 = assemble_coach_context(migrated_connection, week_of_block(5))
    week_30 = assemble_coach_context(migrated_connection, week_of_block(30))

    assert len(week_30.text) <= FLAT_CONTEXT_RATIO * len(week_5.text)
    assert len(week_30.recent_weeks) == len(week_5.recent_weeks)
    assert assemble_coach_context(migrated_connection, week_of_block(30)) == week_30
    assert migrated_connection.total_changes == changes_before
    assert _statement_count(migrated_connection, week_of_block(6)) == (
        _statement_count(migrated_connection, week_of_block(30))
    )
