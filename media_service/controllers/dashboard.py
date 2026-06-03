"""
Dashboard Controller
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, and_
from sqlmodel import Session, func, select

from auth_sdk_m8.controllers.base import BaseController
from media_service.core.deps import CurrentUser
from media_service.db_models.categories import Category
from media_service.schemas.dashboard import (
    ActivityCounter,
    ActivityStats,
    RangeActivityType,
    UsersActivity,
)
# pylint: disable=broad-exception-caught


class DashboardController:
    """
    Dashboard Controller
    """

    @staticmethod
    def get_range_activity(time_range: RangeActivityType) -> tuple[datetime, datetime]:
        """
        Calculate the start and end datetime for a given time range.
        Args:
            time_range (RangeActivityType):
            The type of time range to calculate.
                It can be one of the following:
                - RangeActivityType.HOUR: The current hour.
                - RangeActivityType.DAY: The current day.
                - RangeActivityType.MONTH: The current month.
                - RangeActivityType.YEAR: The current year.
        Returns:
            tuple[Optional[int], Optional[int]]:
                A tuple containing the start and end datetime objects
                for the specified time range.
        Raises:
            ValueError: If an invalid time_range is provided.
        """
        now = datetime.now()
        if time_range == RangeActivityType.HOUR:
            start = now.replace(minute=0, second=0, microsecond=0)
            end = start + timedelta(hours=1)
        elif time_range == RangeActivityType.DAY:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        elif time_range == RangeActivityType.MONTH:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            # Compute first day of next month
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)
        elif time_range == RangeActivityType.YEAR:
            start = now.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
            end = start.replace(year=start.year + 1)
        else:
            raise ValueError(
                "Invalid time_range provided. Use 'hour', 'day', 'month', or 'year'."
            )
        return start, end

    @staticmethod
    def get_activity_count_by_model(
        *,
        session: Session,
        current_user: CurrentUser,
        time_range: RangeActivityType,
        is_current: bool = False,
    ) -> ActivityStats:
        """Count, per model, the rows updated and added within a time range.

        Returns:
            ActivityStats: a dict with keys "min", "max", and "activity" (list
            of dicts, each with "model", "updated", "added").
        """
        start, end = DashboardController.get_range_activity(time_range)

        models = [
            (Category, "Category"),
        ]
        activity: list[ActivityCounter] = []
        min_val = 0
        max_val = 0
        for model, model_name in models:
            stmt = select(
                func.sum(
                    case(
                        (and_(model.updated_at >= start, model.updated_at < end), 1),
                        else_=0,
                    )
                ).label("updated"),
                func.sum(
                    case(
                        (and_(model.created_at >= start, model.created_at < end), 1),
                        else_=0,
                    )
                ).label("added"),
            ).select_from(model)
            if not current_user.is_superuser or is_current is True:
                if model_name == "User":  # pragma: no cover
                    stmt = stmt.where(model.id == current_user.id)  # type: ignore[arg-type]
                else:
                    stmt = stmt.where(model.owner_id == current_user.id)  # type: ignore[arg-type]
            row: Any = session.exec(stmt).first()
            updated_count = int(row.updated) if row and row.updated is not None else 0
            added_count = int(row.added) if row and row.added is not None else 0
            activity.append(
                ActivityCounter(
                    model=model_name, updated=updated_count, added=added_count
                )
            )
            min_val = min(min_val, updated_count, added_count)
            max_val = max(max_val, updated_count, added_count)
        return ActivityStats(min=min_val, max=max_val, activity=activity)

    @staticmethod
    def get_dash_users_stats(
        session: Session,
        current_user: CurrentUser,
        time_range: RangeActivityType,
        is_current: bool = False,
    ) -> UsersActivity:
        """
        Retrieves dashboard user statistics.

        Args:
            session (Session): The database session to use for queries.
            current_user (CurrentUser): The current authenticated user.
            is_current (bool, optional):
                Flag to determine if the current time range should be used.
                Defaults to False.

        Returns:
            ResponseModelBase:
                A response model containing the success status, data,
                and any error messages.

        Raises:
            HTTPException:
            If an unexpected error occurs, an HTTP 500 error is raised.

        Notes:
            - If the current user is a superuser,
              the total number of users is included in the response data.
            - The activity count by model is always included
              in the response data.
            - Handles various exceptions such as IntegrityError,
              ValidationError, ValueError, TypeError, and IOError.
        """
        try:
            activity = DashboardController.get_activity_count_by_model(
                session=session,
                current_user=current_user,
                time_range=time_range,
                is_current=is_current,
            )
            return UsersActivity(
                nb_users=0,
                activity=activity,
            )
        except Exception as ex:
            return BaseController.handle_exception(ex=ex, session=session)
