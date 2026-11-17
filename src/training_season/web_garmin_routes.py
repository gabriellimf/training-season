"""Garmin login and MFA code from the chat page. The code is never logged or echoed."""

from fastapi import APIRouter

from training_season.garmin_auth import GarminAuthConfig
from training_season.garmin_auth_text import (
    GarminAuthOutcome,
    start_login_outcome,
    submit_mfa_outcome,
)
from training_season.web_schemas import GarminAuthView, MfaCodeBody
from training_season.web_settings import WebContextParam
from training_season.web_state_routes import garmin_state

garmin_router = APIRouter(prefix="/api")


def _auth_view(outcome: GarminAuthOutcome, config: GarminAuthConfig) -> GarminAuthView:
    return GarminAuthView(
        status=outcome.status, message=outcome.message, garmin=garmin_state(config)
    )


@garmin_router.post("/garmin/login")
def start_garmin_login(context: WebContextParam) -> GarminAuthView:
    setup = context.settings.setup
    with context.work.run("garmin_login"):
        outcome = start_login_outcome(setup.auth_config, factory=setup.garmin_factory)
    return _auth_view(outcome, setup.auth_config)


@garmin_router.post("/garmin/mfa")
def submit_garmin_code(body: MfaCodeBody, context: WebContextParam) -> GarminAuthView:
    setup = context.settings.setup
    with context.work.run("garmin_mfa"):
        outcome = submit_mfa_outcome(
            setup.auth_config, body.code, factory=setup.garmin_factory
        )
    return _auth_view(outcome, setup.auth_config)
