"""Tests for the Garmin auth base module: the config object."""

from __future__ import annotations

from typing import TYPE_CHECKING

from garmin_auth_fakes import _FAKE_EMAIL, _FAKE_PASSWORD

if TYPE_CHECKING:
    from training_season.garmin_auth import GarminAuthConfig


def test_config_repr_hides_credentials(auth_config: GarminAuthConfig) -> None:
    config_repr = repr(auth_config)

    assert _FAKE_EMAIL not in config_repr
    assert _FAKE_PASSWORD not in config_repr
