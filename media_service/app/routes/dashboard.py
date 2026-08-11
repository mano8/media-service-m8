"""
DashBoard routes

Both routes are writer-tier (A16 operator decision), matching the decision
``A15`` recorded for ``prompt-engine-m8``'s identical ``/dashboard`` pair: an
operational activity view sits above plain read access, so the two services
answer the same question the same way. The floor is mounted on the router, so a
route added here later inherits it.
"""

from fastapi import APIRouter, Depends
from media_service.app.deps import CurrentWriter, SessionDep, require_writer
from auth_sdk_m8.controllers.base import BaseController
from media_service.controllers.dashboard import DashboardController
from media_service.schemas.dashboard import RangeActivityType, UsersActivity

router = APIRouter(
    prefix="/dashboard",
    tags=["dashboard"],
    dependencies=[Depends(require_writer)],
)
# pylint: disable=broad-exception-caught, unused-argument


@router.get(
    "/users/activity/",
    response_model=UsersActivity,
    responses=BaseController.get_error_responses(),
)
def get_dash_users_stats(
    session: SessionDep, current_user: CurrentWriter
) -> UsersActivity:
    """Get phpfina files list from source."""
    return DashboardController.get_dash_users_stats(
        session=session, current_user=current_user, time_range=RangeActivityType.MONTH
    )


@router.get(
    "/users/activity/current/",
    response_model=UsersActivity,
    responses=BaseController.get_error_responses(),
)
def get_dash_current_user_stats(
    session: SessionDep, current_user: CurrentWriter
) -> UsersActivity:
    """Get phpfina files list from source."""
    return DashboardController.get_dash_users_stats(
        session=session,
        current_user=current_user,
        time_range=RangeActivityType.MONTH,
        is_current=True,
    )
