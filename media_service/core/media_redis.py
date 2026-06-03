"""Media-owned Redis configuration.

The generic REDIS_* settings remain reserved for auth-sdk-m8 token revocation
checks. Media features such as queues, locks, rate limits, and caches should
use MEDIA_REDIS_* and the media:* key namespace.
"""

from dataclasses import dataclass

from media_service.core.config import settings


@dataclass(frozen=True)
class MediaRedisConfig:
    """Connection settings for media-owned Redis state."""

    host: str
    port: int
    username: str
    password: str | None
    namespace: str

    def key(self, *parts: str) -> str:
        """Build a namespaced Redis key."""
        clean_parts = [part.strip(":") for part in parts if part]
        return ":".join([self.namespace, *clean_parts])


def get_media_redis_config() -> MediaRedisConfig:
    """Return media-owned Redis settings."""
    return MediaRedisConfig(
        host=settings.MEDIA_REDIS_HOST,
        port=settings.MEDIA_REDIS_PORT,
        username=settings.MEDIA_REDIS_USER,
        password=(
            settings.MEDIA_REDIS_PASSWORD.get_secret_value()
            if settings.MEDIA_REDIS_PASSWORD
            else None
        ),
        namespace=settings.MEDIA_REDIS_NAMESPACE.strip(":"),
    )
