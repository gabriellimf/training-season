"""Writes and reads for the macro plan: onboarding, revisions and history."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from training_season.athlete_profile import RevisionReason
from training_season.database import transaction
from training_season.garmin_pull_base import canonical_json
from training_season.onboarding_store import insert_exchange, mark_session_completed
from training_season.push_store import active_macro_plan_id
from training_season.timeformat import utc_now
from training_season.workout_validation import WorkoutValidationError

if TYPE_CHECKING:
    import sqlite3

    from training_season.athlete_profile import AthleteProfile, OnboardingResult
    from training_season.macro_schema import MacroPlan
    from training_season.onboarding_store import SessionExchange

_UPSERT_PROFILE = """
INSERT INTO profile (id, data_json, created_at, updated_at) VALUES (1, ?, ?, ?)
ON CONFLICT (id) DO UPDATE SET
    data_json = excluded.data_json,
    updated_at = excluded.updated_at
"""
_ARCHIVE_ACTIVE_MACRO = """
UPDATE macro_plan SET status = 'archived', archived_at = ? WHERE status = 'active'
"""
_INSERT_ACTIVE_MACRO = """
INSERT INTO macro_plan (status, start_date, race_date, plan_json, created_at)
VALUES ('active', ?, ?, ?, ?)
RETURNING id
"""
_UPDATE_MACRO_PLAN = """
UPDATE macro_plan SET start_date = ?, race_date = ?, plan_json = ? WHERE id = ?
"""
_INSERT_REVISION = """
INSERT INTO macro_revision
    (macro_plan_id, revision_number, reason, plan_json, created_at)
VALUES (?, ?, ?, ?, ?)
"""
_NEXT_REVISION_NUMBER = """
SELECT COALESCE(MAX(revision_number), 0) + 1 FROM macro_revision
WHERE macro_plan_id = ?
"""
_MACRO_HISTORY = """
SELECT id, status, start_date, race_date, created_at, archived_at
FROM macro_plan ORDER BY id
"""
_REVISION_HISTORY = """
SELECT macro_plan_id, revision_number, reason, created_at
FROM macro_revision ORDER BY macro_plan_id, revision_number
"""
_REASON_ADAPTER: TypeAdapter[str] = TypeAdapter(RevisionReason)


@dataclass(frozen=True, slots=True)
class MacroRevisionEntry:
    """One recorded revision of a macro plan and its reason."""

    revision_number: int
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class MacroHistoryEntry:
    """One macro plan, active or archived, with its revisions in order."""

    macro_plan_id: int
    status: str
    start_date: str
    race_date: str
    created_at: str
    archived_at: str | None
    revisions: tuple[MacroRevisionEntry, ...]


def _plan_json(plan: MacroPlan) -> str:
    return canonical_json(plan.model_dump(mode="json"))


def _upsert_profile(
    connection: sqlite3.Connection, profile: AthleteProfile, now: str
) -> None:
    profile_json = canonical_json(profile.model_dump(mode="json"))
    connection.execute(_UPSERT_PROFILE, (profile_json, now, now))


def _insert_active_macro(
    connection: sqlite3.Connection, plan: MacroPlan, now: str
) -> int:
    row = connection.execute(
        _INSERT_ACTIVE_MACRO,
        (
            plan.start_date.isoformat(),
            plan.race_date.isoformat(),
            _plan_json(plan),
            now,
        ),
    ).fetchone()
    return int(row[0])


@dataclass(frozen=True, slots=True)
class _Revision:
    macro_plan_id: int
    revision_number: int
    reason: str
    created_at: str


def _insert_revision(
    connection: sqlite3.Connection, revision: _Revision, plan: MacroPlan
) -> None:
    connection.execute(
        _INSERT_REVISION,
        (
            revision.macro_plan_id,
            revision.revision_number,
            revision.reason,
            _plan_json(plan),
            revision.created_at,
        ),
    )


def store_onboarding_outcome(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    result: OnboardingResult,
    plan: MacroPlan,
    final_exchange: SessionExchange,
) -> int:
    """
    Save the profile, replace the active macro and close the session at once.

    The old active macro is archived first, so one macro stays active.
    Used as `store_onboarding_outcome(connection, session_id=4, result=r, plan=p,
    final_exchange=SessionExchange("Pode montar.", "Plano salvo."))`
    """
    now = utc_now()
    with transaction(connection):
        insert_exchange(connection, session_id, final_exchange)
        _upsert_profile(connection, result.profile, now)
        connection.execute(_ARCHIVE_ACTIVE_MACRO, (now,))
        macro_plan_id = _insert_active_macro(connection, plan, now)
        _insert_revision(
            connection, _Revision(macro_plan_id, 1, result.reason, now), plan
        )
        mark_session_completed(connection, session_id, macro_plan_id, now)
    return macro_plan_id


class RevisionReasonError(ValueError):
    """The revision reason breaks the rules of `OnboardingResult.reason`."""


def _validated_reason(reason: str) -> str:
    try:
        return _REASON_ADAPTER.validate_python(reason)
    except ValidationError as error:
        lines = WorkoutValidationError.from_pydantic(error).errors
        raise RevisionReasonError(
            f"revision reason of {len(reason)} characters is invalid: "
            f"{'; '.join(lines)}"
        ) from error


def revise_active_macro(
    connection: sqlite3.Connection, plan: MacroPlan, reason: str
) -> int:
    """
    Replace the active macro plan and record why. Returns the revision number.

    Used as `revise_active_macro(connection, plan, "Prova mudou para maio")`
    """
    valid_reason = _validated_reason(reason)
    with transaction(connection):
        macro_plan_id = active_macro_plan_id(connection)
        connection.execute(
            _UPDATE_MACRO_PLAN,
            (
                plan.start_date.isoformat(),
                plan.race_date.isoformat(),
                _plan_json(plan),
                macro_plan_id,
            ),
        )
        number = int(
            connection.execute(_NEXT_REVISION_NUMBER, (macro_plan_id,)).fetchone()[0]
        )
        revision = _Revision(macro_plan_id, number, valid_reason, utc_now())
        _insert_revision(connection, revision, plan)
    return number


def _revisions_by_macro(
    connection: sqlite3.Connection,
) -> dict[int, tuple[MacroRevisionEntry, ...]]:
    grouped: defaultdict[int, list[MacroRevisionEntry]] = defaultdict(list)
    for macro_plan_id, *revision in connection.execute(_REVISION_HISTORY):
        grouped[macro_plan_id].append(MacroRevisionEntry(*revision))
    return {macro_plan_id: tuple(entries) for macro_plan_id, entries in grouped.items()}


def read_macro_history(
    connection: sqlite3.Connection,
) -> tuple[MacroHistoryEntry, ...]:
    """
    Return every macro plan in id order, archived ones included, with revisions.

    Used as `for entry in read_macro_history(connection): print(entry.status)`
    """
    revisions = _revisions_by_macro(connection)
    return tuple(
        MacroHistoryEntry(
            macro_plan_id,
            status,
            start,
            race,
            created,
            archived,
            revisions.get(macro_plan_id, ()),
        )
        for macro_plan_id, status, start, race, created, archived in connection.execute(
            _MACRO_HISTORY
        )
    )


def read_macro_plan_json(
    connection: sqlite3.Connection, macro_plan_id: int
) -> str | None:
    """
    Return one macro plan's JSON as stored, or None for an unknown id.

    Used as `read_macro_plan_json(connection, 1)`
    """
    row = connection.execute(
        "SELECT plan_json FROM macro_plan WHERE id = ?", (macro_plan_id,)
    ).fetchone()
    return None if row is None else str(row[0])
