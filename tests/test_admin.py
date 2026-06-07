"""Tests for app/routes/admin.py and controllers/admin.py."""

import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
)
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    size_bytes: int = 1024,
    status: MediaObjectStatus = MediaObjectStatus.UPLOADED,
    deleted: bool = False,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category="document",
        visibility=MediaVisibility.PRIVATE,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{oid}/original/file.pdf",
        mime_type="application/pdf",
        size_bytes=size_bytes,
        status=status,
        deleted_at=datetime.utcnow() if deleted else None,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _make_stale_session(session: Session, owner_id: uuid.UUID) -> UploadSession:
    sid = uuid.uuid4()
    us = UploadSession(
        id=sid,
        owner_user_id=owner_id,
        category="document",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{sid}/original/report.pdf",
        expected_mime_type="application/pdf",
        expected_size_bytes=1024,
        expires_at=datetime.utcnow() - timedelta(seconds=10),
    )
    session.add(us)
    session.commit()
    session.refresh(us)
    return us


def _make_active_session(session: Session, owner_id: uuid.UUID) -> UploadSession:
    sid = uuid.uuid4()
    us = UploadSession(
        id=sid,
        owner_user_id=owner_id,
        category="document",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{sid}/original/active.pdf",
        expected_mime_type="application/pdf",
        expected_size_bytes=1024,
        expires_at=datetime.utcnow() + timedelta(seconds=300),
    )
    session.add(us)
    session.commit()
    session.refresh(us)
    return us


# ── GET /media/v1/admin/storage/stats ────────────────────────────────────────


def test_storage_stats_empty_db(superuser_client: TestClient):
    resp = superuser_client.get("/media/v1/admin/storage/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_objects"] == 0
    assert data["total_bytes"] == 0
    assert data["deleted_objects"] == 0
    assert data["by_status"] == []
    assert data["by_category"] == []


def test_storage_stats_counts_live_objects(
    superuser_client: TestClient, session: Session, superuser
):
    _make_object(session, superuser.id, size_bytes=2048)
    resp = superuser_client.get("/media/v1/admin/storage/stats")
    data = resp.json()
    assert data["total_objects"] == 1
    assert data["total_bytes"] == 2048
    assert data["deleted_objects"] == 0
    assert len(data["by_status"]) == 1
    assert data["by_status"][0]["status"] == "uploaded"
    assert len(data["by_category"]) == 1
    assert data["by_category"][0]["category"] == "document"


def test_storage_stats_excludes_deleted_from_live(
    superuser_client: TestClient, session: Session, superuser
):
    _make_object(session, superuser.id)
    _make_object(session, superuser.id, deleted=True)
    resp = superuser_client.get("/media/v1/admin/storage/stats")
    data = resp.json()
    assert data["total_objects"] == 1
    assert data["deleted_objects"] == 1


def test_storage_stats_multiple_statuses(
    superuser_client: TestClient, session: Session, superuser
):
    _make_object(session, superuser.id, status=MediaObjectStatus.UPLOADED)
    _make_object(session, superuser.id, status=MediaObjectStatus.READY)
    resp = superuser_client.get("/media/v1/admin/storage/stats")
    data = resp.json()
    assert data["total_objects"] == 2
    assert len(data["by_status"]) == 2


def test_storage_stats_requires_superuser(client: TestClient):
    resp = client.get("/media/v1/admin/storage/stats")
    assert resp.status_code == 403


# ── GET /media/v1/admin/uploads/stale ────────────────────────────────────────


def test_stale_uploads_empty(superuser_client: TestClient):
    resp = superuser_client.get("/media/v1/admin/uploads/stale")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["sessions"] == []


def test_stale_uploads_finds_expired_sessions(
    superuser_client: TestClient, session: Session, superuser
):
    _make_stale_session(session, superuser.id)
    resp = superuser_client.get("/media/v1/admin/uploads/stale")
    data = resp.json()
    assert data["count"] == 1
    assert len(data["sessions"]) == 1
    assert "id" in data["sessions"][0]
    assert "expires_at" in data["sessions"][0]


def test_stale_uploads_excludes_active_sessions(
    superuser_client: TestClient, session: Session, superuser
):
    _make_active_session(session, superuser.id)
    resp = superuser_client.get("/media/v1/admin/uploads/stale")
    assert resp.json()["count"] == 0


def test_stale_uploads_requires_superuser(client: TestClient):
    resp = client.get("/media/v1/admin/uploads/stale")
    assert resp.status_code == 403


# ── POST /media/v1/admin/uploads/purge-stale ─────────────────────────────────


def test_purge_stale_noop_when_none(superuser_client: TestClient):
    resp = superuser_client.post("/media/v1/admin/uploads/purge-stale")
    assert resp.status_code == 200
    assert resp.json()["purged"] == 0


def test_purge_stale_marks_sessions_expired(
    superuser_client: TestClient, session: Session, superuser
):
    us = _make_stale_session(session, superuser.id)
    resp = superuser_client.post("/media/v1/admin/uploads/purge-stale")
    assert resp.json()["purged"] == 1
    session.refresh(us)
    assert us.status == UploadSessionStatus.EXPIRED


def test_purge_stale_skips_active_sessions(
    superuser_client: TestClient, session: Session, superuser
):
    _make_active_session(session, superuser.id)
    resp = superuser_client.post("/media/v1/admin/uploads/purge-stale")
    assert resp.json()["purged"] == 0


def test_purge_stale_requires_superuser(client: TestClient):
    resp = client.post("/media/v1/admin/uploads/purge-stale")
    assert resp.status_code == 403
