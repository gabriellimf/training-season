"""`GET /api/plan`: the plan cards of the stored weeks that are not over."""

from fastapi import APIRouter

from training_season.plan_view import plan_weeks
from training_season.web_schemas import PlanView, PlanWeekView
from training_season.web_settings import (
    WebContextParam,
    open_connection,
    training_today,
)

plan_router = APIRouter(prefix="/api")


@plan_router.get("/plan")
def read_plan(context: WebContextParam) -> PlanView:
    today = training_today(context.settings.setup)
    with open_connection(context.settings) as connection:
        weeks = plan_weeks(connection, today)
    return PlanView(
        today=today, weeks=[PlanWeekView.model_validate(week) for week in weeks]
    )
