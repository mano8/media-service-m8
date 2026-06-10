"""Per-user Redis rate limiter for the media service."""

import logging
from typing import Annotated

import redis as redis_lib
from fastapi import Depends, HTTPException, Request, status

from media_service.core.deps import CurrentUser
from media_service.core.media_redis import get_media_redis_config

_logger = logging.getLogger(__name__)
_config = get_media_redis_config()

# Coarse per-IP cap = per-user limit × this factor. Bounds a single source
# spraying across many accounts while staying generous enough not to throttle
# legitimate shared-NAT traffic (and, when the service sits behind a proxy,
# not to collapse the whole tenant onto the proxy IP).
_IP_LIMIT_FACTOR = 10
# IP key length cap — prevents Redis key namespace pollution from a forged
# X-Forwarded-For / oversized peer string.
_MAX_IP_LEN = 45


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
    """FastAPI callable dependency — enforces per-user + per-IP rate limits.

    Two fixed-window counters (INCR + EXPIRE) are checked per request:

    * a per-user window (the primary limit), and
    * a coarse per-source-IP window (``limit × _IP_LIMIT_FACTOR``) that catches
      a single source spraying the endpoint across many accounts.

    Fails open on Redis errors so a cache outage never blocks media traffic.
    """

    def __init__(self, action: str, limit: int, window_seconds: int = 60) -> None:
        self.action = action
        self.limit = limit
        self.window_seconds = window_seconds

    def _check(self, redis_client: redis_lib.Redis, key: str, limit: int) -> None:
        """Increment one window and raise 429 if it exceeds *limit*."""
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, self.window_seconds)
        if count > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded. "
                    f"Max {self.limit} requests per {self.window_seconds}s."
                ),
                headers={"Retry-After": str(self.window_seconds)},
            )

    def __call__(
        self,
        request: Request,
        current_user: CurrentUser,
        redis_client: RedisDep,
    ) -> None:
        """Increment the counters and raise HTTP 429 if a limit is exceeded."""
        user_key = _config.key(f"ratelimit:{self.action}", str(current_user.id))
        peer = request.client.host if request.client else "unknown"
        ip = "".join(c for c in peer if c.isprintable())[:_MAX_IP_LEN]
        ip_key = _config.key(f"ratelimit:ip:{self.action}", ip)
        try:
            self._check(redis_client, user_key, self.limit)
            self._check(redis_client, ip_key, self.limit * _IP_LIMIT_FACTOR)
        except HTTPException:
            raise
        except Exception:
            _logger.warning("rate_limit.redis_error action=%s", self.action)
