"""Per-user Redis rate limiter for the media service."""

import logging
from typing import Annotated, cast

import redis as redis_lib
from fastapi import Depends, HTTPException, Request, status

import media_service.metrics as _metrics
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

    On Redis errors the behaviour is controlled by *failure_mode*:

    * ``"fail_open"`` (default) — lets traffic through; a Redis outage never
      blocks media traffic, but rate limits are temporarily unenforced.
    * ``"fail_closed"`` — returns HTTP 503; prevents unenforced bursts during
      Redis outages.  Recommended for production (set via
      ``MEDIA_RATE_LIMIT_FAILURE_MODE=fail_closed``).

    When *failure_mode* is ``None`` the value is read at call time from
    ``settings.MEDIA_RATE_LIMIT_FAILURE_MODE``, letting callers pick up runtime
    configuration without needing to pass it at instantiation.
    """

    def __init__(
        self,
        action: str,
        limit: int,
        window_seconds: int = 60,
        *,
        failure_mode: str | None = None,
    ) -> None:
        self.action = action
        self.limit = limit
        self.window_seconds = window_seconds
        self._failure_mode = failure_mode

    def _check(self, redis_client: redis_lib.Redis, key: str, limit: int) -> None:
        """Increment one window and raise 429 if it exceeds *limit*."""
        # The sync client returns an int; redis-py's shared sync/async typing
        # widens INCR to ``Awaitable | int``, so narrow it for the comparisons.
        count = cast(int, redis_client.incr(key))
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
        if self._failure_mode is not None:
            mode = self._failure_mode
        else:
            from media_service.core.config import settings  # lazy — avoids circular import at module load

            mode = settings.MEDIA_RATE_LIMIT_FAILURE_MODE
        try:
            self._check(redis_client, user_key, self.limit)
            self._check(redis_client, ip_key, self.limit * _IP_LIMIT_FACTOR)
        except HTTPException:
            raise
        except Exception:
            _metrics.inc_rate_limit_redis_error(mode)
            if mode == "fail_closed":
                _logger.error(
                    "rate_limit.redis_error action=%s mode=fail_closed → 503",
                    self.action,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Rate limiter temporarily unavailable. Please retry.",
                )
            _logger.warning(
                "rate_limit.redis_error action=%s mode=fail_open → pass",
                self.action,
            )
