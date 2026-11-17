"""The FastAPI app of the chat page: JSON routes under /api, the page at /."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from training_season.database import initialize_database
from training_season.web_checkin_routes import checkin_router
from training_season.web_garmin_routes import garmin_router
from training_season.web_onboarding_routes import onboarding_router
from training_season.web_plan_routes import plan_router
from training_season.web_request_guard import refuse_foreign_writes
from training_season.web_responses import register_error_handlers, secured_response
from training_season.web_settings import (
    WebContext,
    WebDependencies,
    WebSettings,
    load_web_settings,
    opened_adapter,
)
from training_season.web_state_routes import state_router
from training_season.web_work import CheckinSlot, CoachWork

STATIC_DIR = Path(__file__).parent / "static"


def _checked_settings(dependencies: WebDependencies) -> WebSettings:
    settings = load_web_settings(dependencies)
    initialize_database(settings.environ)
    with opened_adapter(settings):
        pass
    return settings


def create_app(
    dependencies: WebDependencies, *, static_dir: Path = STATIC_DIR
) -> FastAPI:
    """
    Check every setting, migrate the database once, then build the app.

    A bad setting raises here, before the server listens.
    Used as `app = create_app(WebDependencies(os.environ))`
    """
    settings = _checked_settings(dependencies)
    app = FastAPI(
        docs_url=None, redoc_url=None, openapi_url=None, strict_content_type=True
    )
    app.state.web_context = WebContext(
        settings, CoachWork(dependencies.clock), CheckinSlot()
    )
    for router in (
        state_router,
        onboarding_router,
        checkin_router,
        plan_router,
        garmin_router,
    ):
        app.include_router(router)
    register_error_handlers(app)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts)
    )
    app.middleware("http")(refuse_foreign_writes)
    app.middleware("http")(secured_response)
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


def create_default_app() -> FastAPI:
    """
    Build the app from the process environment, for `uvicorn --factory`.

    Used as `uvicorn --factory training_season.web_app:create_default_app`
    """
    return create_app(WebDependencies(os.environ))
