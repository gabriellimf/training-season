"""Tests for the Garmin activity type resolution."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import sqlite3

    from garmin_auth_fakes import FakeGarminServer

from garmin_pull_fakes import FakeGarminData, _activity, _activity_types, _run_pull
from training_season import garmin_activities
from training_season.garmin_activities import (
    _MAX_ACTIVITY_TYPE_DEPTH,
    _build_type_roots,
    _resolve_sport,
)
from training_season.garmin_auth import GarminAuthConfig, GarminUnavailableError


def test_first_run_stores_running_and_swimming_activities(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[
            _activity(10, "2026-10-24 08:00:00"),
            _activity(11, "2026-10-25 08:00:00", "swimming", type_id=2),
        ]
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    assert result.activities_added == 2
    assert migrated_connection.execute(
        "SELECT sport FROM activity ORDER BY garmin_activity_id"
    ).fetchall() == [("running",), ("swimming",)]


def test_child_types_resolve_to_sport_via_parent_chain(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[
            _activity(
                1,
                type_key="treadmill_running",
                type_id=3,
                parent_type_id=1,
            ),
            _activity(
                2,
                type_key="trail_running",
                type_id=4,
                parent_type_id=3,
            ),
            _activity(
                3,
                type_key="lap_swimming",
                type_id=5,
                parent_type_id=2,
            ),
            _activity(
                4,
                type_key="open_water_swimming",
                type_id=6,
                parent_type_id=5,
            ),
        ]
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    assert result.activities_added == 4
    assert migrated_connection.execute(
        "SELECT sport FROM activity ORDER BY garmin_activity_id"
    ).fetchall() == [
        ("running",),
        ("running",),
        ("swimming",),
        ("swimming",),
    ]


def test_child_type_without_parent_uses_type_map() -> None:
    type_roots = _build_type_roots(_activity_types())
    activity_type: dict[str, Any] = {
        "typeId": 3,
        "typeKey": "treadmill_running",
    }

    assert _resolve_sport(activity_type, type_roots) == "running"


@pytest.mark.parametrize("parent_type_id", ["invalid", 999])
def test_child_type_with_invalid_parent_uses_type_map(parent_type_id: object) -> None:
    type_roots = _build_type_roots(_activity_types())
    activity_type: dict[str, Any] = {
        "typeId": 3,
        "typeKey": "treadmill_running",
        "parentTypeId": parent_type_id,
    }

    assert _resolve_sport(activity_type, type_roots) == "running"


def test_unknown_type_without_parent_is_skipped() -> None:
    type_roots = _build_type_roots(_activity_types())
    activity_type: dict[str, Any] = {
        "typeId": 999,
        "typeKey": "unknown_activity",
    }

    assert _resolve_sport(activity_type, type_roots) is None


def test_parent_cycle_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(garmin_activities, "_MAX_ACTIVITY_TYPE_DEPTH", 1000)

    class CountingTypeRoots(Mapping[int, tuple[str, int | None]]):
        def __init__(self) -> None:
            self.values_by_id = {
                1: ("custom_a", 2),
                2: ("custom_b", 1),
            }
            self.lookups = 0

        def __getitem__(self, key: int) -> tuple[str, int | None]:
            self.lookups += 1
            return self.values_by_id[key]

        def __iter__(self) -> Iterator[int]:
            return iter(self.values_by_id)

        def __len__(self) -> int:
            return len(self.values_by_id)

        def __contains__(self, key: object) -> bool:
            return key in self.values_by_id

    type_roots = CountingTypeRoots()
    activity_type: dict[str, Any] = {
        "typeId": 99,
        "typeKey": "custom",
        "parentTypeId": 1,
    }

    assert _resolve_sport(activity_type, type_roots) is None
    assert type_roots.lookups == 2


def test_parent_chain_at_hop_limit_resolves() -> None:
    type_roots: dict[int, tuple[str, int | None]] = {
        type_id: (f"custom_{type_id}", type_id + 1)
        for type_id in range(1, _MAX_ACTIVITY_TYPE_DEPTH)
    }
    type_roots[_MAX_ACTIVITY_TYPE_DEPTH] = ("running", None)
    activity_type: dict[str, Any] = {
        "typeId": 0,
        "typeKey": "custom",
        "parentTypeId": 1,
    }

    assert _resolve_sport(activity_type, type_roots) == "running"


def test_parent_chain_beyond_hop_limit_is_skipped() -> None:
    type_roots: dict[int, tuple[str, int | None]] = {
        type_id: (f"custom_{type_id}", type_id + 1)
        for type_id in range(1, _MAX_ACTIVITY_TYPE_DEPTH + 1)
    }
    type_roots[_MAX_ACTIVITY_TYPE_DEPTH + 1] = ("running", None)
    activity_type: dict[str, Any] = {
        "typeId": 0,
        "typeKey": "custom",
        "parentTypeId": 1,
    }

    assert _resolve_sport(activity_type, type_roots) is None


def test_other_sports_are_skipped_and_counted(
    migrated_connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    fake_server: FakeGarminServer,
) -> None:
    data = FakeGarminData(
        activities=[
            _activity(1, type_key="cycling", type_id=7),
            _activity(2, type_key="strength_training", type_id=8),
            _activity(3, type_key="walking", type_id=9),
            _activity(4, type_key="multi_sport", type_id=10),
            _activity(
                5,
                type_key="unresolved_activity",
                type_id=100,
                parent_type_id=999,
            ),
        ]
    )

    result, _ = _run_pull(
        migrated_connection,
        auth_config,
        fake_server,
        data,
        history_start="2026-10-24",
    )

    assert result.activities_skipped == 5
    assert result.activities_invalid == 0
    assert migrated_connection.execute("SELECT count(*) FROM activity").fetchone() == (
        0,
    )


@pytest.mark.parametrize(
    "entries",
    [
        ["not-a-dict"],
        [{"typeId": True, "typeKey": "running", "parentTypeId": None}],
        [{"typeId": 1, "typeKey": " ", "parentTypeId": None}],
        [{"typeId": 1, "typeKey": "running", "parentTypeId": True}],
        [{"typeId": 1, "typeKey": "running", "parentTypeId": None}] * 2,
    ],
    ids=["not-dict", "bool-type-id", "blank-type-key", "bool-parent", "duplicate-id"],
)
def test_build_type_roots_rejects_bad_entries(entries: list[object]) -> None:
    with pytest.raises(GarminUnavailableError) as raised:
        _build_type_roots(entries)

    assert str(raised.value) == "Garmin returned an unexpected activity type list"
