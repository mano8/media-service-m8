"""Tests for media_service/core/events.py — auth event-stream wiring."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from media_service.core.events import make_lifespan_extras


# ── make_lifespan_extras ─────────────────────────────────────────────────────


def test_returns_none_when_introspection_url_unset():
    settings = MagicMock(spec=["INTROSPECTION_URL"])
    settings.INTROSPECTION_URL = None
    result = make_lifespan_extras(settings, MagicMock())
    assert result is None


@pytest.mark.anyio
async def test_returns_factory_and_starts_stops_client():
    """The stream client is built with the SDK's own dispatch, not a local
    re-implementation: ``on_event`` must be ``auth.handle_auth_event`` and
    ``on_gap`` must delegate to ``auth.flush_cache``."""
    settings = MagicMock()
    settings.INTROSPECTION_URL = "http://auth:8000/private/v1/jti-status"

    mock_client = MagicMock()
    mock_client.stop = AsyncMock()
    captured: dict = {}

    def fake_build(s, *, on_event, on_gap, **kw):
        captured["on_event"] = on_event
        captured["on_gap"] = on_gap
        return mock_client

    auth = MagicMock()
    with patch(
        "media_service.core.events.build_event_stream_client", side_effect=fake_build
    ):
        extras = make_lifespan_extras(settings, auth)
        assert extras is not None
        async with extras(MagicMock()):
            pass

    mock_client.start.assert_called_once()
    mock_client.stop.assert_awaited_once()

    # The client must be wired straight to the SDK's own dispatch methods —
    # no locally re-derived handler in between.
    assert captured["on_event"] is auth.handle_auth_event

    await captured["on_gap"]()
    auth.flush_cache.assert_called_once()


# ── isolation guard ──────────────────────────────────────────────────────────


def test_isolation_media_redis_only_no_auth_redis_subscription():
    """Media service must not subscribe to fa-auth's Redis bus."""
    from media_service.core.config import Settings

    # All Redis fields in media-service must be MEDIA_REDIS_* prefixed.
    # Bare REDIS_* fields (e.g. REDIS_HOST used to subscribe to auth events)
    # would indicate a violation of the isolation invariant.
    own_fields = set(Settings.model_fields) - set(
        Settings.__bases__[0].model_fields  # type: ignore[attr-defined]
    )
    bare_redis_own = [f for f in own_fields if f.startswith("REDIS_")]
    assert bare_redis_own == [], (
        f"Media-specific bare Redis fields found: {bare_redis_own}"
    )


def test_isolation_no_deprecated_event_bus_import():
    """Media service must not import the deprecated Redis Pub/Sub bus."""
    import importlib
    import pkgutil

    import media_service

    for importer, modname, ispkg in pkgutil.walk_packages(
        path=media_service.__path__,
        prefix="media_service.",
    ):
        mod = importlib.import_module(modname)
        src = getattr(mod, "__file__", None) or ""
        if not src.endswith(".py"):
            continue
        import pathlib

        content = pathlib.Path(src).read_text()
        assert "redis_events" not in content, (
            f"{modname} imports the deprecated Redis event bus"
        )
        assert "EventBus" not in content, f"{modname} references EventBus"
        assert "EventSubscriber" not in content, f"{modname} references EventSubscriber"
