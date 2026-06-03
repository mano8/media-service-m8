"""Per-user Redis rate limiter for the media service."""

import logging
from typing import Annotated

import redis as redis_lib
from fastapi import Depends, HTTPException, status

from media_service.core.deps import CurrentUser
from media_service.core.media_redis import get_media_redis_config

_logger = logging.getLogger(__name__)
_config = get_media_redis_config()


def get_redis_client() -> redis_lib.Redis:  # pragma: no cover
    """Provide a sync Redis client using media-owned connection settings."""
    return redis_lib.Redis(
        host=_config.host,
        port=_config.port,
        username=_config.username,
        password=_config.password,
        decode_responses=False,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


RedisDep = Annotated[redis_lib.Redis, Depends(get_redis_client)]


class RateLimiter:
    """FastAPI callable dependency — enforces per-user rate limits via Redis.

    Uses a fixed-window counter (INCR + EXPIRE). Fails open on Redis errors
    so a cache outage never blocks uploads.
    """

    def __init__(self, action: str, limit: int, window_seconds: int = 60) -> None:
        self.action = action
        self.limit = limit
        self.window_seconds = window_seconds

    def __call__(
        self,
        current_user: CurrentUser,
        redis_client: RedisDep,
    ) -> None:
        """Increment the counter and raise HTTP 429 if the limit is exceeded."""
        key = _config.key(f"ratelimit:{self.action}", str(current_user.id))
        try:
            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, self.window_seconds)
            if count > self.limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Rate limit exceeded. "
                        f"Max {self.limit} requests per {self.window_seconds}s."
                    ),
                    headers={"Retry-After": str(self.window_seconds)},
                )
        except HTTPException:
            raise
        except Exception:
            _logger.warning("rate_limit.redis_error action=%s", self.action)
