"""Tests for media_service.maintenance_worker — the service-owned arq worker."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from arq.connections import RedisSettings
from sqlmodel import Session, select

import media_service.maintenance_worker as mw
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    utcnow,
)
from media_service.db_models.outbox import OutboxEvent, OutboxStatus, Subscription
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus
from media_service.storage.client import ObjectStorage


def _fake_engine(session: Session) -> MagicMock:
    """A stand-in DbEngine whose ``session()`` context yields the test session."""
    engine = MagicMock()
    engine.session.return_value.__enter__.return_value = session
    engine.session.return_value.__exit__.return_value = False
    return engine


def _deleted_object(session: Session, *, days_ago: int) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=uuid.uuid4(),
        category="document",
        visibility=MediaVisibility.PRIVATE,
        storage_bucket="private-media",
        object_key=f"key/{oid}",
        mime_type="application/pdf",
        size_bytes=1024,
        status=MediaObjectStatus.DELETED,
        deleted_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    session.add(obj)
    session.commit()
    return obj


# ── module-level helpers / class wiring ──────────────────────────────────────


def test_all_buckets_lists_every_configured_bucket():
    assert mw._all_buckets() == [
        "public-media",
        "private-media",
        "sensitive-media",
        "temp-media",
        "archive-media",
    ]


def test_worker_settings_wiring():
    assert isinstance(mw.WorkerSettings.redis_settings, RedisSettings)
    assert mw.WorkerSettings.on_startup is mw.startup
    assert mw.WorkerSettings.on_shutdown is mw.shutdown
    assert mw.WorkerSettings.functions == [
        mw.hard_purge_expired,
        mw.expire_stale_uploads,
        mw.reconcile_orphans,
        mw.deliver_outbox,
    ]
    # One scheduler, four crons (single replica prevents double-fire).
    assert len(mw.WorkerSettings.cron_jobs) == 4


# ── startup / shutdown ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_startup_builds_storage_client():
    ctx: dict = {}
    await mw.startup(ctx)
    assert isinstance(ctx["storage"], ObjectStorage)


@pytest.mark.anyio
async def test_shutdown_disposes_engine(monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr(mw, "engine", engine)
    await mw.shutdown({})
    engine.dispose.assert_called_once()


# ── cron bodies ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_hard_purge_cron_purges_expired(
    session: Session, mock_storage: MagicMock, monkeypatch
):
    monkeypatch.setattr(mw, "engine", _fake_engine(session))
    obj = _deleted_object(session, days_ago=40)

    purged = await mw.hard_purge_expired({"storage": mock_storage})

    assert purged == 1
    assert session.get(MediaObject, obj.id) is None


@pytest.mark.anyio
async def test_expire_stale_cron_expires_sessions(
    session: Session, mock_storage: MagicMock, monkeypatch
):
    monkeypatch.setattr(mw, "engine", _fake_engine(session))
    sid = uuid.uuid4()
    us = UploadSession(
        id=sid,
        owner_user_id=uuid.uuid4(),
        category="document",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"stale/{sid}",
        expected_mime_type="application/pdf",
        expected_size_bytes=1024,
        expires_at=datetime.utcnow() - timedelta(seconds=10),
    )
    session.add(us)
    session.commit()

    expired = await mw.expire_stale_uploads({"storage": mock_storage})

    assert expired == 1
    session.refresh(us)
    assert us.status == UploadSessionStatus.EXPIRED


@pytest.mark.anyio
async def test_reconcile_cron_reports_orphans(
    session: Session, mock_storage: MagicMock, monkeypatch
):
    monkeypatch.setattr(mw, "engine", _fake_engine(session))
    mock_storage.list_object_keys.return_value = ["orphan-key"]

    total = await mw.reconcile_orphans({"storage": mock_storage})

    # One storage-orphan per swept bucket; report-only (never deleted).
    assert total == len(mw._all_buckets())
    mock_storage.remove_object.assert_not_called()


@pytest.mark.anyio
async def test_deliver_outbox_cron_delivers_pending(session: Session, monkeypatch):
    monkeypatch.setattr(mw, "engine", _fake_engine(session))
    session.add(
        Subscription(
            url="https://hook.example.com/h",
            secret="a-strong-subscriber-secret-123456",
            event_types=[],
            active=True,
        )
    )
    session.add(
        OutboxEvent(
            event_type="object.ready",
            object_id=uuid.uuid4(),
            payload={"status": "ready"},
            next_attempt_at=utcnow(),
        )
    )
    session.commit()

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    # Route the cron's real httpx.Client through the in-process fake subscriber.
    # Capture the real class first so the replacement does not recurse into itself.
    real_client_cls = httpx.Client
    monkeypatch.setattr(
        mw.httpx,
        "Client",
        lambda **_kw: real_client_cls(transport=httpx.MockTransport(handler)),
    )

    delivered = await mw.deliver_outbox({})

    assert delivered == 1
    assert len(calls) == 1
    event = session.exec(select(OutboxEvent)).first()
    assert event is not None
    assert event.status == OutboxStatus.DELIVERED
