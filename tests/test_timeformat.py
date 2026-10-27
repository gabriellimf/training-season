"""Tests for timestamp and training-date formatting."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from training_season.timeformat import training_date, utc_timestamp


def test_utc_timestamp_converts_aware_datetime() -> None:
    moment = datetime(
        2026,
        10,
        26,
        15,
        33,
        33,
        tzinfo=ZoneInfo("America/Sao_Paulo"),
    )

    assert utc_timestamp(moment) == "2026-10-26T18:33:33Z"


def test_utc_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_timestamp(datetime(2026, 10, 26, 15, 33, 33))  # noqa: DTZ001


def test_utc_timestamp_drops_microseconds() -> None:
    moment = datetime(2026, 10, 26, 18, 33, 33, 123456, tzinfo=UTC)

    assert utc_timestamp(moment) == "2026-10-26T18:33:33Z"


def test_training_date_uses_given_zone() -> None:
    moment = datetime(2026, 10, 27, 1, 30, tzinfo=UTC)

    assert training_date(moment, ZoneInfo("America/Sao_Paulo")) == "2026-10-26"


def test_training_date_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        training_date(
            datetime(2026, 10, 27, 1, 30),  # noqa: DTZ001
            ZoneInfo("America/Sao_Paulo"),
        )
