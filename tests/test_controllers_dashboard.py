"""Tests for controllers/dashboard.py DashboardController."""

import pytest
from datetime import datetime
from unittest.mock import MagicMock

from media_service.controllers.dashboard import DashboardController
from media_service.schemas.dashboard import RangeActivityType


def test_get_range_activity_hour():
    start, end = DashboardController.get_range_activity(RangeActivityType.HOUR)
    assert start.minute == 0
    assert start.second == 0
    assert (end - start).seconds == 3600


def test_get_range_activity_day():
    start, end = DashboardController.get_range_activity(RangeActivityType.DAY)
    assert start.hour == 0
    assert (end - start).days == 1


def test_get_range_activity_month():
    start, end = DashboardController.get_range_activity(RangeActivityType.MONTH)
    assert start.day == 1
    assert start.hour == 0


def test_get_range_activity_year():
    start, end = DashboardController.get_range_activity(RangeActivityType.YEAR)
    assert start.month == 1
    assert start.day == 1
    assert end.year == start.year + 1


def test_get_range_activity_invalid_raises():
    with pytest.raises(ValueError, match="Invalid time_range"):
        DashboardController.get_range_activity("invalid")  # type: ignore[arg-type]


def test_get_range_activity_december_month_rolls_to_january():
    """Ensure month-end rollover for December produces January of next year."""
    import unittest.mock as mock

    fixed = datetime(2024, 12, 15, 10, 30)
    with mock.patch("media_service.controllers.dashboard.datetime") as mock_dt:
        mock_dt.now.return_value = fixed
        start, end = DashboardController.get_range_activity(RangeActivityType.MONTH)
    assert start.month == 12
    assert end.month == 1
    assert end.year == 2025


def test_get_activity_count_by_model_returns_structure(session):
    from auth_sdk_m8.schemas.user import UserModel

    user = UserModel(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        email="u@test.com",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    result = DashboardController.get_activity_count_by_model(
        session=session,
        current_user=user,
        time_range=RangeActivityType.DAY,
    )
    assert "activity" in result
    assert "max" in result
    assert "min" in result


def test_get_activity_count_superuser(session):
    from auth_sdk_m8.schemas.user import UserModel

    user = UserModel(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        email="su@test.com",
        is_active=True,
        is_superuser=True,
        role="superadmin",
    )
    result = DashboardController.get_activity_count_by_model(
        session=session,
        current_user=user,
        time_range=RangeActivityType.HOUR,
    )
    assert isinstance(result["activity"], list)


def test_get_dash_users_stats_exception_handler():
    """Exercise the except block in get_dash_users_stats."""

    from auth_sdk_m8.schemas.user import UserModel

    from media_service.controllers.dashboard import DashboardController
    from media_service.schemas.dashboard import RangeActivityType

    bad_session = MagicMock()
    bad_session.exec.side_effect = RuntimeError("DB failure")

    user = UserModel(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        email="u@test.com",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    result = DashboardController.get_dash_users_stats(
        session=bad_session,
        current_user=user,
        time_range=RangeActivityType.DAY,
    )
    assert result is not None


def test_get_dash_users_stats_returns_users_activity(session):
    from auth_sdk_m8.schemas.user import UserModel
    from media_service.schemas.dashboard import UsersActivity

    user = UserModel(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        email="u@test.com",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    result = DashboardController.get_dash_users_stats(
        session=session,
        current_user=user,
        time_range=RangeActivityType.YEAR,
    )
    assert isinstance(result, UsersActivity)
