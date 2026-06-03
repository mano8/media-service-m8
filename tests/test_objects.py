"""Tests for app/routes/objects.py (get / download-url / update / delete)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    visibility: MediaVisibility = MediaVisibility.PRIVATE,
    status: MediaObjectStatus = MediaObjectStatus.UPLOADED,
    deleted: bool = False,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category="document",
        visibility=visibility,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{oid}/original/file.pdf",
        original_filename="file.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        status=status,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


# ── GET /media/v1/objects/{id} ────────────────────────────────────────────────


def test_get_object_happy_path(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    resp = client.get(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(obj.id)


def test_get_object_not_found(client: TestClient):
    resp = client.get(f"/media/v1/objects/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_object_soft_deleted_returns_404(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id, deleted=True)
    resp = client.get(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 404


def test_get_object_forbidden_different_owner(
    client: TestClient, session: Session, superuser
):
    obj = _make_object(session, superuser.id)
    resp = client.get(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 403


def test_get_object_superuser_sees_any(
    superuser_client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    resp = superuser_client.get(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 200


# ── GET /media/v1/objects/{id}/download-url ───────────────────────────────────


def test_download_url_returns_presigned_url(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    mock_storage.presigned_get_object.return_value = "https://minio/download"
    resp = client.get(f"/media/v1/objects/{obj.id}/download-url")
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://minio/download"
    assert "expires_at" in resp.json()


def test_download_url_not_found(client: TestClient):
    resp = client.get(f"/media/v1/objects/{uuid.uuid4()}/download-url")
    assert resp.status_code == 404


def test_download_url_forbidden(client: TestClient, session: Session, superuser):
    obj = _make_object(session, superuser.id)
    resp = client.get(f"/media/v1/objects/{obj.id}/download-url")
    assert resp.status_code == 403


# ── PATCH /media/v1/objects/{id} ─────────────────────────────────────────────


def test_update_object_visibility(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    resp = client.patch(
        f"/media/v1/objects/{obj.id}",
        json={"visibility": "public"},
    )
    assert resp.status_code == 200
    assert resp.json()["visibility"] == "public"


def test_update_object_filename(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    resp = client.patch(
        f"/media/v1/objects/{obj.id}",
        json={"original_filename": "renamed.pdf"},
    )
    assert resp.status_code == 200
    assert resp.json()["original_filename"] == "renamed.pdf"


def test_update_object_not_found(client: TestClient):
    resp = client.patch(
        f"/media/v1/objects/{uuid.uuid4()}", json={"visibility": "public"}
    )
    assert resp.status_code == 404


def test_update_object_forbidden(client: TestClient, session: Session, superuser):
    obj = _make_object(session, superuser.id)
    resp = client.patch(f"/media/v1/objects/{obj.id}", json={"visibility": "public"})
    assert resp.status_code == 403


# ── DELETE /media/v1/objects/{id} ─────────────────────────────────────────────


def test_delete_object_soft_deletes(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    session.refresh(obj)
    assert obj.deleted_at is not None
    assert obj.status == MediaObjectStatus.DELETED


def test_delete_object_idempotent(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id, deleted=True)
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204


def test_delete_object_not_found(client: TestClient):
    resp = client.delete(f"/media/v1/objects/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_object_forbidden(client: TestClient, session: Session, superuser):
    obj = _make_object(session, superuser.id)
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 403


def test_delete_object_superuser_can_delete_any(
    superuser_client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    resp = superuser_client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204


# ── Rate limit ────────────────────────────────────────────────────────────────


def test_download_url_rate_limited(client: TestClient, mock_redis: MagicMock):
    mock_redis.incr.return_value = 61
    resp = client.get(f"/media/v1/objects/{uuid.uuid4()}/download-url")
    assert resp.status_code == 429
