"""Tests for core/media_redis.py."""

from media_service.core.config import settings
from media_service.core.media_redis import MediaRedisConfig, get_media_redis_config


def test_get_media_redis_config_fields():
    cfg = get_media_redis_config()
    assert cfg.host == settings.MEDIA_REDIS_HOST
    assert cfg.port == settings.MEDIA_REDIS_PORT
    assert cfg.namespace == settings.MEDIA_REDIS_NAMESPACE.strip(":")


def test_media_redis_config_key_basic():
    cfg = MediaRedisConfig(
        host="h", port=6379, username="u", password="p", namespace="ns"
    )
    assert cfg.key("a", "b") == "ns:a:b"


def test_media_redis_config_key_strips_leading_trailing_colons():
    cfg = MediaRedisConfig(
        host="h", port=6379, username="u", password="p", namespace="ns"
    )
    assert cfg.key(":a:", ":b:") == "ns:a:b"


def test_media_redis_config_key_filters_empty_parts():
    cfg = MediaRedisConfig(
        host="h", port=6379, username="u", password="p", namespace="ns"
    )
    assert cfg.key("a", "", "b") == "ns:a:b"
