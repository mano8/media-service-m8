"""Tests for the superuser maintenance admin routes (app/routes/admin.py)."""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
)

_BASE = "/media/v1/admin/maintenance"


def _make_object(
    session: Session,
    *,
    status: MediaObjectStatus = MediaObjectStatus.UPLOADED,
    deleted_at: datetime | None = None,
    created_at: datetime | None = None,
    object_key: str | None = None,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=uuid.uuid4(),
        category="document",
        visibility=MediaVisibility.PRIVATE,
        storage_bucket="private-media",
        object_key=object_key or f"key/{oid}",
        mime_type="application/pdf",
        size_bytes=1024,
        status=status,
        deleted_at=deleted_at,
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


# ── superuser gating ─────────────────────────────────────────────────────────


def test_orphans_report_requires_superuser(client: TestClient):
    assert client.get(f"{_BASE}/orphans").status_code == 403


def test_repair_requires_superuser(client: TestClient):
    assert client.post(f"{_BASE}/orphans/repair?confirm=true").status_code == 403


def test_purge_expired_requires_superuser(client: TestClient):
    assert client.post(f"{_BASE}/purge-expired").status_code == 403


# ── orphan report (read-only) ────────────────────────────────────────────────


def test_orphans_report_lists_storage_orphans(
    superuser_client: TestClient, mock_storage, session: Session
):
    mock_storage.list_object_keys.return_value = ["ghost-key"]

    resp = superuser_client.get(f"{_BASE}/orphans")

    assert resp.status_code == 200
    body = resp.json()
    # Five buckets are swept; the orphan key surfaces once per bucket.
    assert body["storage_orphan_count"] == 5
    assert body["repaired"] == 0
    mock_storage.remove_object.assert_not_called()


def test_repair_dry_run_does_not_delete(superuser_client: TestClient, mock_storage):
    mock_storage.list_object_keys.return_value = ["ghost-key"]

    resp = superuser_client.post(f"{_BASE}/orphans/repair")  # confirm defaults False

    assert resp.status_code == 200
    assert resp.json()["repaired"] == 0
    mock_storage.remove_object.assert_not_called()


def test_repair_confirmed_deletes_storage_orphans(
    superuser_client: TestClient, mock_storage
):
    mock_storage.list_object_keys.return_value = ["ghost-key"]

    resp = superuser_client.post(f"{_BASE}/orphans/repair?confirm=true")

    assert resp.status_code == 200
    assert resp.json()["repaired"] == 5
    assert mock_storage.remove_object.call_count == 5


# ── operator hard-purge ──────────────────────────────────────────────────────


def test_purge_expired_hard_deletes(
    superuser_client: TestClient, mock_storage, session: Session
):
    obj = _make_object(
        session,
        status=MediaObjectStatus.DELETED,
        deleted_at=datetime.now(timezone.utc) - timedelta(days=40),
    )

    resp = superuser_client.post(f"{_BASE}/purge-expired")

    assert resp.status_code == 200
    assert resp.json()["purged"] == 1
    assert session.get(MediaObject, obj.id) is None
