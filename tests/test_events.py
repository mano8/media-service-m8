"""Tests for media_service/core/events.py — auth event-stream handlers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi_m8 import AuthStreamEvent

from media_service.core.events import (
    handle_auth_event,
    handle_auth_gap,
    make_lifespan_extras,
)


def _make_auth(**overrides):
    auth = MagicMock()
    for k, v in overrides.items():
        setattr(auth, k, v)
    return auth


def _make_event(event_type: str, extra: dict | None = None) -> AuthStreamEvent:
    payload = {"event_type": event_type}
    if extra:
        payload.update(extra)
    return AuthStreamEvent(event_type=event_type, payload=payload, event_id=None)


# ── handle_auth_event ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_session_revoked_with_jti_calls_evict_jti():
    auth = _make_auth()
    event = _make_event("session.revoked", {"user_id": "uid1", "jti": "jti1"})
    await handle_auth_event(event, auth=auth)
    auth.evict_jti.assert_called_once_with("jti1")
    auth.evict_user.assert_not_called()


@pytest.mark.anyio
async def test_session_revoked_without_jti_calls_evict_user():
    auth = _make_auth()
    event = _make_event("session.revoked", {"user_id": "uid2"})
    await handle_auth_event(event, auth=auth)
    auth.evict_user.assert_called_once_with("uid2")
    auth.evict_jti.assert_not_called()


@pytest.mark.anyio
async def test_user_deleted_calls_evict_user():
    auth = _make_auth()
    event = _make_event("user.deleted", {"user_id": "uid3"})
    await handle_auth_event(event, auth=auth)
    auth.evict_user.assert_called_once_with("uid3")
    auth.evict_jti.assert_not_called()


@pytest.mark.anyio
async def test_unknown_event_type_is_ignored():
    auth = _make_auth()
    event = _make_event("other.event")
    await handle_auth_event(event, auth=auth)
    auth.evict_jti.assert_not_called()
    auth.evict_user.assert_not_called()


@pytest.mark.anyio
async def test_handler_exception_is_swallowed():
    auth = _make_auth(evict_jti=MagicMock(side_effect=RuntimeError("boom")))
    event = _make_event("session.revoked", {"user_id": "uid4", "jti": "jti4"})
    await handle_auth_event(event, auth=auth)  # must not raise


# ── handle_auth_gap ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_gap_calls_flush_cache():
    auth = _make_auth()
    await handle_auth_gap(auth=auth)
    auth.flush_cache.assert_called_once()


@pytest.mark.anyio
async def test_gap_exception_is_swallowed():
    auth = _make_auth(flush_cache=MagicMock(side_effect=RuntimeError("boom")))
    await handle_auth_gap(auth=auth)  # must not raise


# ── make_lifespan_extras ─────────────────────────────────────────────────────


def test_returns_none_when_introspection_url_unset():
    settings = MagicMock(spec=["INTROSPECTION_URL"])
    settings.INTROSPECTION_URL = None
    result = make_lifespan_extras(settings, MagicMock())
    assert result is None


@pytest.mark.anyio
async def test_returns_factory_and_starts_stops_client():
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

    # Exercise the captured closures to cover the inner functions.
    event = _make_event("user.deleted", {"user_id": "uid-x"})
    await captured["on_event"](event)
    await captured["on_gap"]()


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
