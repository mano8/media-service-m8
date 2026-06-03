"""Tests for core/rate_limit.py RateLimiter."""

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from auth_sdk_m8.schemas.user import UserModel
from media_service.core.rate_limit import RateLimiter


def _make_user() -> UserModel:
    return UserModel(
        id=str(uuid.uuid4()),
        email="rl@test.com",
        is_active=True,
        is_superuser=False,
        role="user",
    )


def _make_redis(count: int = 1) -> MagicMock:
    mock = MagicMock()
    mock.incr.return_value = count
    return mock


def test_rate_limiter_allows_under_limit():
    limiter = RateLimiter("test:action", limit=5)
    limiter(current_user=_make_user(), redis_client=_make_redis(count=3))


def test_rate_limiter_expire_set_on_first_request():
    redis = _make_redis(count=1)
    limiter = RateLimiter("test:action", limit=5, window_seconds=30)
    limiter(current_user=_make_user(), redis_client=redis)
    redis.expire.assert_called_once()
    key, ttl = redis.expire.call_args.args
    assert ttl == 30
    assert "ratelimit:test:action" in key


def test_rate_limiter_expire_not_set_on_subsequent_requests():
    redis = _make_redis(count=2)
    limiter = RateLimiter("test:action", limit=5)
    limiter(current_user=_make_user(), redis_client=redis)
    redis.expire.assert_not_called()


def test_rate_limiter_raises_429_when_over_limit():
    limiter = RateLimiter("test:action", limit=5)
    with pytest.raises(HTTPException) as exc_info:
        limiter(current_user=_make_user(), redis_client=_make_redis(count=6))
    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers


def test_rate_limiter_retry_after_header_matches_window():
    limiter = RateLimiter("test:action", limit=5, window_seconds=45)
    with pytest.raises(HTTPException) as exc_info:
        limiter(current_user=_make_user(), redis_client=_make_redis(count=6))
    assert exc_info.value.headers["Retry-After"] == "45"


def test_rate_limiter_raises_429_at_limit_plus_one():
    limiter = RateLimiter("uploads:initiate", limit=20)
    with pytest.raises(HTTPException) as exc_info:
        limiter(current_user=_make_user(), redis_client=_make_redis(count=21))
    assert exc_info.value.status_code == 429


def test_rate_limiter_allows_at_exact_limit():
    limiter = RateLimiter("uploads:initiate", limit=20)
    limiter(current_user=_make_user(), redis_client=_make_redis(count=20))


def test_rate_limiter_fails_open_on_redis_connection_error():
    redis = MagicMock()
    redis.incr.side_effect = ConnectionError("Redis down")
    limiter = RateLimiter("test:action", limit=5)
    limiter(current_user=_make_user(), redis_client=redis)


def test_rate_limiter_key_uses_namespace_and_user_id():
    redis = _make_redis(count=1)
    user = _make_user()
    limiter = RateLimiter("uploads:initiate", limit=20)
    limiter(current_user=user, redis_client=redis)
    key = redis.incr.call_args.args[0]
    assert key.startswith("media:ratelimit:uploads:initiate:")
    assert str(user.id) in key


def test_rate_limiter_different_users_get_different_keys():
    redis = _make_redis(count=1)
    limiter = RateLimiter("uploads:initiate", limit=20)
    user_a, user_b = _make_user(), _make_user()
    limiter(current_user=user_a, redis_client=redis)
    limiter(current_user=user_b, redis_client=redis)
    keys = [call.args[0] for call in redis.incr.call_args_list]
    assert keys[0] != keys[1]
