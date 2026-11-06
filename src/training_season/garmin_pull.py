"""
Pull Garmin history into SQLite using resumable write units.

Each activity and its splits commit together after network calls finish. Wellness
commits one date window at a time. Both cursors include a fixed overlap so a rerun
can resume after an outage without duplicating activities.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from training_season.database import (
    DatabaseConfigurationError,
    MigrationError,
    connect,
    database_path_from_environment,
    initialize_database,
)
from training_season.garmin_activities import pull_activities
from training_season.garmin_auth import (
    LOCK_TIMEOUT_SECONDS,
    GarminAuthConfig,
    GarminConfigurationError,
    GarminError,
    GarminFactory,
    GarminLoginReason,
    GarminMfaRequiredError,
    default_garmin_factory,
    garmin_auth_config_from_environment,
    garmin_session,
)
from training_season.garmin_pull_base import GarminPullContext, parse_iso_date
from training_season.garmin_wellness import pull_wellness
from training_season.timeformat import (
    TrainingTimezoneError,
    training_date,
    training_zone_from_environment,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from zoneinfo import ZoneInfo

    from training_season.garmin_activities import ActivityPhase
    from training_season.garmin_wellness import WellnessPhase

GARMIN_HISTORY_START_ENV = "GARMIN_HISTORY_START"
_HISTORY_START_ERROR = (
    "GARMIN_HISTORY_START must be a valid YYYY-MM-DD date no later than today"
)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GarminPullResult:
    """Counts and date windows from one Garmin pull."""

    login_reason: GarminLoginReason | None
    activity_start: str
    activity_end: str
    activities_added: int
    activities_skipped: int
    activities_invalid: int
    wellness_start: str
    wellness_end: str
    wellness_days_written: int


def _history_start_date(value: str | None) -> date | None:
    if value is None or not value.strip():
        return None
    parsed = parse_iso_date(value)
    if parsed is None:
        raise GarminConfigurationError(_HISTORY_START_ERROR)
    return parsed


def history_start_from_environment(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read an optional, strictly formatted first-pull start date."""
    environment = os.environ if environ is None else environ
    history_start = _history_start_date(environment.get(GARMIN_HISTORY_START_ENV))
    return None if history_start is None else history_start.isoformat()


def _pull_today(now: datetime | None, zone: ZoneInfo) -> date:
    current_time = datetime.now(UTC) if now is None else now
    return date.fromisoformat(training_date(current_time, zone))


def _checked_history_start(history_start: str | None, today: date) -> str | None:
    history_start_date = _history_start_date(history_start)
    if history_start_date is None:
        return None
    if history_start_date > today:
        raise GarminConfigurationError(_HISTORY_START_ERROR)
    return history_start_date.isoformat()


def _pull_result(
    login_reason: GarminLoginReason | None,
    activities: ActivityPhase,
    wellness: WellnessPhase,
) -> GarminPullResult:
    return GarminPullResult(
        login_reason=login_reason,
        activity_start=activities.start,
        activity_end=activities.end,
        activities_added=activities.added,
        activities_skipped=activities.skipped,
        activities_invalid=activities.invalid,
        wellness_start=wellness.start,
        wellness_end=wellness.end,
        wellness_days_written=wellness.written,
    )


def _pull_phases(
    run: GarminPullContext, login_reason: GarminLoginReason | None
) -> GarminPullResult:
    activities = pull_activities(run)
    _LOGGER.info(
        "activities %s..%s: added %d, skipped %d, invalid %d",
        activities.start,
        activities.end,
        activities.added,
        activities.skipped,
        activities.invalid,
    )
    wellness = pull_wellness(run)
    _LOGGER.info(
        "wellness %s..%s: wrote %d days",
        wellness.start,
        wellness.end,
        wellness.written,
    )
    return _pull_result(login_reason, activities, wellness)


def pull_garmin(
    connection: sqlite3.Connection,
    auth_config: GarminAuthConfig,
    zone: ZoneInfo,
    *,
    factory: GarminFactory = default_garmin_factory,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    now: datetime | None = None,
    history_start: str | None = None,
) -> GarminPullResult:
    """Pull activities and wellness data through one locked Garmin session."""
    today = _pull_today(now, zone)
    checked_start = _checked_history_start(history_start, today)
    with garmin_session(
        auth_config, lock_timeout=lock_timeout, factory=factory
    ) as session:
        run = GarminPullContext(session.garmin, connection, zone, today, checked_start)
        return _pull_phases(run, session.login_reason)


def _print_usage() -> None:
    sys.stderr.write("Usage: training-season-pull [--help]\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the manual Garmin pull command."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--help"], ["-h"]):
        _print_usage()
        return 0
    if arguments:
        _print_usage()
        return 2

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        initialize_database()
        auth_config = garmin_auth_config_from_environment()
        zone = training_zone_from_environment()
        history_start = history_start_from_environment()
        path = database_path_from_environment()
        with closing(connect(path)) as connection:
            result = pull_garmin(
                connection,
                auth_config,
                zone,
                history_start=history_start,
            )
        sys.stdout.write(
            f"activities {result.activity_start}..{result.activity_end}: "
            f"added {result.activities_added}, skipped {result.activities_skipped}, "
            f"invalid {result.activities_invalid}; wellness "
            f"{result.wellness_start}..{result.wellness_end}: "
            f"{result.wellness_days_written} days\n"
        )
    except GarminMfaRequiredError as error:
        sys.stderr.write(f"{error}\n")
        return 3
    except (
        DatabaseConfigurationError,
        GarminConfigurationError,
        TrainingTimezoneError,
    ) as error:
        sys.stderr.write(f"{error}\n")
        return 2
    except (GarminError, MigrationError, sqlite3.Error) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
