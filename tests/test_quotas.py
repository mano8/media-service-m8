"""Tests for Phase 13 — storage quotas & accounting.

Covers the pure accounting helper (``core/quotas.py``), enforcement at upload
initiation, usage maintenance across complete/delete, and the admin quota
endpoints (``app/routes/admin.py`` + ``controllers/admin.py``).
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.core import quotas
from media_service.core.config import settings
from media_service.db_models.storage_usage import StorageUsage
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus


# ── helpers ───────────────────────────────────────────────────────────────────

_INITIATE_BODY = {
    "category": "document",
    "visibility": "private",
    "original_filename": "report.pdf",
    "mime_type": "application/pdf",
    "expected_size_bytes": 2048,
}

# Leading bytes the sniffer recognises as application/pdf (matches the session).
_PDF_BYTES = b"%PDF-1.4" + b"\x00" * 504


def _make_session(session: Session, owner_id: uuid.UUID) -> UploadSession:
    sid = uuid.uuid4()
    us = UploadSession(
        id=sid,
        owner_user_id=owner_id,
        category="document",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{sid}/original/report.pdf",
        expected_mime_type="application/pdf",
        expected_size_bytes=2048,
        status=UploadSessionStatus.INITIATED,
        expires_at=datetime.utcnow() + timedelta(seconds=300),
    )
    session.add(us)
    session.commit()
    session.refresh(us)
    return us


def _stat_mock(size: int = 2048, etag: str = "etag123") -> MagicMock:
    stat = MagicMock()
    stat.size = size
    stat.etag = etag
    return stat


def _make_usage(
    session: Session,
    owner_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    total_bytes: int = 0,
    object_count: int = 0,
    quota_bytes: int | None = None,
    quota_objects: int | None = None,
) -> StorageUsage:
    usage = StorageUsage(
        owner_user_id=owner_id,
        tenant_id=tenant_id,
        total_bytes=total_bytes,
        object_count=object_count,
        quota_bytes=quota_bytes,
        quota_objects=quota_objects,
    )
    session.add(usage)
    session.commit()
    session.refresh(usage)
    return usage


# ── core/quotas.py — get_or_create / find ─────────────────────────────────────


def test_get_or_create_usage_creates_then_reuses(session: Session):
    owner = uuid.uuid4()
    first = quotas.get_or_create_usage(session, owner_user_id=owner, tenant_id=None)
    session.commit()
    second = quotas.get_or_create_usage(session, owner_user_id=owner, tenant_id=None)
    assert first.id == second.id


def test_find_usage_scopes_by_tenant(session: Session):
    owner = uuid.uuid4()
    tenant = uuid.uuid4()
    _make_usage(session, owner, tenant_id=None, total_bytes=10)
    _make_usage(session, owner, tenant_id=tenant, total_bytes=20)
    global_row = quotas._find_usage(session, owner_user_id=owner, tenant_id=None)
    tenant_row = quotas._find_usage(session, owner_user_id=owner, tenant_id=tenant)
    assert global_row.total_bytes == 10
    assert tenant_row.total_bytes == 20


# ── core/quotas.py — effective quota resolution ───────────────────────────────


def test_effective_quota_prefers_override(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_DEFAULT_QUOTA_BYTES", 100)
    monkeypatch.setattr(settings, "MEDIA_DEFAULT_QUOTA_OBJECTS", 5)
    usage = StorageUsage(owner_user_id=uuid.uuid4(), quota_bytes=999, quota_objects=9)
    assert quotas.effective_quota_bytes(usage) == 999
    assert quotas.effective_quota_objects(usage) == 9


def test_effective_quota_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_DEFAULT_QUOTA_BYTES", 100)
    monkeypatch.setattr(settings, "MEDIA_DEFAULT_QUOTA_OBJECTS", 5)
    assert quotas.effective_quota_bytes(None) == 100
    assert quotas.effective_quota_objects(None) == 5
    usage = StorageUsage(owner_user_id=uuid.uuid4())  # no overrides
    assert quotas.effective_quota_bytes(usage) == 100
    assert quotas.effective_quota_objects(usage) == 5


# ── core/quotas.py — check_quota ──────────────────────────────────────────────


def test_check_quota_passes_when_unlimited(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_DEFAULT_QUOTA_BYTES", None)
    monkeypatch.setattr(settings, "MEDIA_DEFAULT_QUOTA_OBJECTS", None)
    # No usage row and no quotas → never raises.
    quotas.check_quota(
        session, owner_user_id=uuid.uuid4(), tenant_id=None, additional_bytes=10**9
    )


def test_check_quota_passes_under_limits(session: Session):
    owner = uuid.uuid4()
    _make_usage(session, owner, total_bytes=100, quota_bytes=1000, quota_objects=10)
    quotas.check_quota(
        session, owner_user_id=owner, tenant_id=None, additional_bytes=500
    )


def test_check_quota_rejects_over_byte_quota(session: Session):
    owner = uuid.uuid4()
    _make_usage(session, owner, total_bytes=900, quota_bytes=1000)
    with pytest.raises(HTTPException) as exc:
        quotas.check_quota(
            session, owner_user_id=owner, tenant_id=None, additional_bytes=200
        )
    assert exc.value.status_code == 413


def test_check_quota_rejects_over_object_quota(session: Session):
    owner = uuid.uuid4()
    _make_usage(session, owner, object_count=3, quota_objects=3)
    with pytest.raises(HTTPException) as exc:
        quotas.check_quota(
            session, owner_user_id=owner, tenant_id=None, additional_bytes=1
        )
    assert exc.value.status_code == 409


# ── core/quotas.py — record add / remove ──────────────────────────────────────


def test_record_object_added_increments(session: Session):
    owner = uuid.uuid4()
    quotas.record_object_added(
        session, owner_user_id=owner, tenant_id=None, size_bytes=512
    )
    session.commit()
    usage = quotas._find_usage(session, owner_user_id=owner, tenant_id=None)
    assert usage.total_bytes == 512
    assert usage.object_count == 1


def test_record_object_removed_clamps_at_zero(session: Session):
    owner = uuid.uuid4()
    _make_usage(session, owner, total_bytes=100, object_count=1)
    quotas.record_object_removed(
        session, owner_user_id=owner, tenant_id=None, size_bytes=999
    )
    session.commit()
    usage = quotas._find_usage(session, owner_user_id=owner, tenant_id=None)
    assert usage.total_bytes == 0
    assert usage.object_count == 0


# ── initiate enforcement (API) ────────────────────────────────────────────────


def test_initiate_rejected_over_default_byte_quota(
    client: TestClient, mock_storage: MagicMock, monkeypatch
):
    monkeypatch.setattr(settings, "MEDIA_DEFAULT_QUOTA_BYTES", 1000)
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    resp = client.post("/media/v1/uploads/initiate", json=_INITIATE_BODY)
    assert resp.status_code == 413


def test_initiate_rejected_over_default_object_quota(
    client: TestClient, mock_storage: MagicMock, monkeypatch
):
    monkeypatch.setattr(settings, "MEDIA_DEFAULT_QUOTA_OBJECTS", 0)
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    resp = client.post("/media/v1/uploads/initiate", json=_INITIATE_BODY)
    assert resp.status_code == 409


def test_initiate_allowed_when_override_raises_ceiling(
    client: TestClient,
    mock_storage: MagicMock,
    session: Session,
    current_user,
    monkeypatch,
):
    monkeypatch.setattr(settings, "MEDIA_DEFAULT_QUOTA_BYTES", 1000)
    _make_usage(session, current_user.id, quota_bytes=10**9)
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    resp = client.post("/media/v1/uploads/initiate", json=_INITIATE_BODY)
    assert resp.status_code == 200


# ── usage stays consistent across complete → delete ───────────────────────────


def test_complete_then_delete_keeps_usage_consistent(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat_mock(size=2048)
    mock_storage.get_object_head.return_value = _PDF_BYTES

    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200
    usage = quotas._find_usage(session, owner_user_id=current_user.id, tenant_id=None)
    assert usage.total_bytes == 2048
    assert usage.object_count == 1

    object_id = resp.json()["media_object"]["id"]
    del_resp = client.delete(f"/media/v1/objects/{object_id}")
    assert del_resp.status_code == 204
    session.refresh(usage)
    assert usage.total_bytes == 0
    assert usage.object_count == 0


def test_rejected_upload_does_not_count_toward_usage(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    # Oversized object: stat reports far beyond the per-category max → rejected.
    mock_storage.stat_object.return_value = _stat_mock(size=10**12)
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 422
    usage = quotas._find_usage(session, owner_user_id=current_user.id, tenant_id=None)
    assert usage is None


# ── admin quota endpoints ─────────────────────────────────────────────────────


def test_admin_get_quota_creates_zeroed_scope(
    superuser_client: TestClient, session: Session
):
    owner = uuid.uuid4()
    resp = superuser_client.get(f"/media/v1/admin/quotas/{owner}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_bytes"] == 0
    assert data["object_count"] == 0
    assert data["quota_bytes"] is None
    # A concrete row now exists for subsequent overrides.
    assert quotas._find_usage(session, owner_user_id=owner, tenant_id=None) is not None


def test_admin_set_quota_applies_override(
    superuser_client: TestClient, session: Session
):
    owner = uuid.uuid4()
    resp = superuser_client.put(
        f"/media/v1/admin/quotas/{owner}",
        json={"quota_bytes": 4096, "quota_objects": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["quota_bytes"] == 4096
    assert data["effective_quota_bytes"] == 4096
    assert data["effective_quota_objects"] == 7


def test_admin_set_quota_partial_update_preserves_other_field(
    superuser_client: TestClient, session: Session
):
    owner = uuid.uuid4()
    _make_usage(session, owner, quota_bytes=100, quota_objects=9)
    resp = superuser_client.put(
        f"/media/v1/admin/quotas/{owner}", json={"quota_bytes": 500}
    )
    data = resp.json()
    assert data["quota_bytes"] == 500
    assert data["quota_objects"] == 9  # untouched


def test_admin_get_quota_scoped_by_tenant(
    superuser_client: TestClient, session: Session
):
    owner = uuid.uuid4()
    tenant = uuid.uuid4()
    _make_usage(session, owner, tenant_id=tenant, total_bytes=42, quota_bytes=99)
    resp = superuser_client.get(
        f"/media/v1/admin/quotas/{owner}", params={"tenant_id": str(tenant)}
    )
    data = resp.json()
    assert data["total_bytes"] == 42
    assert data["tenant_id"] == str(tenant)


def test_admin_quota_endpoints_require_superuser(client: TestClient):
    owner = uuid.uuid4()
    assert client.get(f"/media/v1/admin/quotas/{owner}").status_code == 403
    assert (
        client.put(
            f"/media/v1/admin/quotas/{owner}", json={"quota_bytes": 1}
        ).status_code
        == 403
    )


def test_storage_stats_includes_usage(superuser_client: TestClient, session: Session):
    owner = uuid.uuid4()
    _make_usage(session, owner, total_bytes=2048, object_count=2, quota_bytes=4096)
    resp = superuser_client.get("/media/v1/admin/storage/stats")
    assert resp.status_code == 200
    usage = resp.json()["usage"]
    assert len(usage) == 1
    assert usage[0]["owner_user_id"] == str(owner)
    assert usage[0]["total_bytes"] == 2048
    assert usage[0]["effective_quota_bytes"] == 4096
