"""Tests for core/rate_limit.py RateLimiter."""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from auth_sdk_m8.schemas.user import UserModel
from media_service.core.rate_limit import _IP_LIMIT_FACTOR, RateLimiter


def _make_user() -> UserModel:
    return UserModel(
        id=str(uuid.uuid4()),
        email="rl@test.com",
        is_active=True,
        is_superuser=False,
        role="user",
    )


def _make_request(ip: str = "1.2.3.4") -> MagicMock:
    req = MagicMock()
    req.client.host = ip
    return req


def _make_redis(count: int = 1) -> MagicMock:
    mock = MagicMock()
    mock.incr.return_value = count
    return mock


def test_rate_limiter_allows_under_limit():
    limiter = RateLimiter("test:action", limit=5)
    limiter(
        request=_make_request(),
        current_user=_make_user(),
        redis_client=_make_redis(count=3),
    )


def test_rate_limiter_expire_set_on_first_request():
    redis = _make_redis(count=1)
    limiter = RateLimiter("test:action", limit=5, window_seconds=30)
    limiter(request=_make_request(), current_user=_make_user(), redis_client=redis)
    # Both the per-user and per-IP windows are first-touch → expiry on each.
    assert redis.expire.call_count == 2
    for call in redis.expire.call_args_list:
        _, ttl = call.args
        assert ttl == 30


def test_rate_limiter_expire_not_set_on_subsequent_requests():
    redis = _make_redis(count=2)
    limiter = RateLimiter("test:action", limit=5)
    limiter(request=_make_request(), current_user=_make_user(), redis_client=redis)
    redis.expire.assert_not_called()


def test_rate_limiter_raises_429_when_over_limit():
    limiter = RateLimiter("test:action", limit=5)
    with pytest.raises(HTTPException) as exc_info:
        limiter(
            request=_make_request(),
            current_user=_make_user(),
            redis_client=_make_redis(count=6),
        )
    assert exc_info.value.status_code == 429
    assert "Retry-After" in exc_info.value.headers


def test_rate_limiter_retry_after_header_matches_window():
    limiter = RateLimiter("test:action", limit=5, window_seconds=45)
    with pytest.raises(HTTPException) as exc_info:
        limiter(
            request=_make_request(),
            current_user=_make_user(),
            redis_client=_make_redis(count=6),
        )
    assert exc_info.value.headers["Retry-After"] == "45"


def test_rate_limiter_raises_429_at_limit_plus_one():
    limiter = RateLimiter("uploads:initiate", limit=20)
    with pytest.raises(HTTPException) as exc_info:
        limiter(
            request=_make_request(),
            current_user=_make_user(),
            redis_client=_make_redis(count=21),
        )
    assert exc_info.value.status_code == 429


def test_rate_limiter_allows_at_exact_limit():
    limiter = RateLimiter("uploads:initiate", limit=20)
    limiter(
        request=_make_request(),
        current_user=_make_user(),
        redis_client=_make_redis(count=20),
    )


def test_rate_limiter_fails_open_on_redis_connection_error():
    redis = MagicMock()
    redis.incr.side_effect = ConnectionError("Redis down")
    limiter = RateLimiter("test:action", limit=5, failure_mode="fail_open")
    limiter(request=_make_request(), current_user=_make_user(), redis_client=redis)


def test_rate_limiter_fail_closed_raises_503_on_redis_error():
    redis = MagicMock()
    redis.incr.side_effect = ConnectionError("Redis down")
    limiter = RateLimiter("test:action", limit=5, failure_mode="fail_closed")
    with pytest.raises(HTTPException) as exc_info:
        limiter(request=_make_request(), current_user=_make_user(), redis_client=redis)
    assert exc_info.value.status_code == 503


def test_rate_limiter_fail_open_emits_metric_on_redis_error():
    redis = MagicMock()
    redis.incr.side_effect = ConnectionError("Redis down")
    limiter = RateLimiter("test:action", limit=5, failure_mode="fail_open")
    with patch("media_service.core.rate_limit._metrics") as mock_metrics:
        limiter(request=_make_request(), current_user=_make_user(), redis_client=redis)
    mock_metrics.inc_rate_limit_redis_error.assert_called_once_with("fail_open")


def test_rate_limiter_fail_closed_emits_metric_on_redis_error():
    redis = MagicMock()
    redis.incr.side_effect = ConnectionError("Redis down")
    limiter = RateLimiter("test:action", limit=5, failure_mode="fail_closed")
    with patch("media_service.core.rate_limit._metrics") as mock_metrics:
        with pytest.raises(HTTPException):
            limiter(
                request=_make_request(), current_user=_make_user(), redis_client=redis
            )
    mock_metrics.inc_rate_limit_redis_error.assert_called_once_with("fail_closed")


def test_rate_limiter_reads_failure_mode_from_settings_when_not_set():
    redis = MagicMock()
    redis.incr.side_effect = ConnectionError("Redis down")
    limiter = RateLimiter("test:action", limit=5)  # no explicit failure_mode
    mock_settings = MagicMock()
    mock_settings.MEDIA_RATE_LIMIT_FAILURE_MODE = "fail_open"
    with patch(
        "media_service.core.rate_limit._metrics"
    ) as mock_metrics, patch(
        "media_service.core.config.settings", mock_settings
    ):
        limiter(request=_make_request(), current_user=_make_user(), redis_client=redis)
    mock_metrics.inc_rate_limit_redis_error.assert_called_once_with("fail_open")


def test_rate_limiter_no_metric_emitted_on_successful_request():
    redis = _make_redis(count=1)
    limiter = RateLimiter("test:action", limit=5, failure_mode="fail_open")
    with patch("media_service.core.rate_limit._metrics") as mock_metrics:
        limiter(request=_make_request(), current_user=_make_user(), redis_client=redis)
    mock_metrics.inc_rate_limit_redis_error.assert_not_called()


def test_rate_limiter_key_uses_namespace_and_user_id():
    redis = _make_redis(count=1)
    user = _make_user()
    limiter = RateLimiter("uploads:initiate", limit=20)
    limiter(request=_make_request(), current_user=user, redis_client=redis)
    # First incr is the per-user window.
    user_key = redis.incr.call_args_list[0].args[0]
    assert user_key.startswith("media:ratelimit:uploads:initiate:")
    assert str(user.id) in user_key


def test_rate_limiter_per_ip_key_uses_client_host():
    redis = _make_redis(count=1)
    limiter = RateLimiter("uploads:initiate", limit=20)
    limiter(
        request=_make_request("203.0.113.7"),
        current_user=_make_user(),
        redis_client=redis,
    )
    keys = [call.args[0] for call in redis.incr.call_args_list]
    ip_key = next(k for k in keys if ":ip:" in k)
    assert ip_key == "media:ratelimit:ip:uploads:initiate:203.0.113.7"


def test_rate_limiter_per_ip_limit_is_factor_of_user_limit():
    # Per-user limit (5) passed, per-IP window (5 * factor) just exceeded.
    over_ip = 5 * _IP_LIMIT_FACTOR + 1

    def incr_side_effect(key):
        return 1 if ":ip:" not in key else over_ip

    redis = MagicMock()
    redis.incr.side_effect = incr_side_effect
    limiter = RateLimiter("test:action", limit=5)
    with pytest.raises(HTTPException) as exc_info:
        limiter(
            request=_make_request("9.9.9.9"),
            current_user=_make_user(),
            redis_client=redis,
        )
    assert exc_info.value.status_code == 429


def test_rate_limiter_per_ip_key_strips_control_chars():
    redis = _make_redis(count=1)
    limiter = RateLimiter("test:action", limit=5)
    limiter(
        request=_make_request("1.2.3.4\ninjected"),
        current_user=_make_user(),
        redis_client=redis,
    )
    ip_key = next(
        k for k in (c.args[0] for c in redis.incr.call_args_list) if ":ip:" in k
    )
    assert "\n" not in ip_key


def test_rate_limiter_different_users_get_different_keys():
    redis = _make_redis(count=1)
    limiter = RateLimiter("uploads:initiate", limit=20)
    user_a, user_b = _make_user(), _make_user()
    limiter(request=_make_request(), current_user=user_a, redis_client=redis)
    limiter(request=_make_request(), current_user=user_b, redis_client=redis)
    user_keys = [
        call.args[0] for call in redis.incr.call_args_list if ":ip:" not in call.args[0]
    ]
    assert user_keys[0] != user_keys[1]
