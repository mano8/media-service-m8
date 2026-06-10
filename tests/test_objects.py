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


def test_update_object_visibility_relocates_bytes(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    resp = client.patch(
        f"/media/v1/objects/{obj.id}",
        json={"visibility": "public"},
    )
    assert resp.status_code == 200
    assert resp.json()["visibility"] == "public"
    assert resp.json()["storage_bucket"] == "public-media"
    mock_storage.copy_object.assert_called_once_with(
        src_bucket="private-media",
        src_object_key=obj.object_key,
        dest_bucket="public-media",
        dest_object_key=obj.object_key,
    )
    # old copy is deleted only after the metadata commit succeeds
    mock_storage.remove_object.assert_called_once_with(
        bucket="private-media", object_key=obj.object_key
    )


def test_update_object_same_bucket_visibility_skips_move(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # PRIVATE and TENANT share the private bucket: no byte movement needed.
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    resp = client.patch(
        f"/media/v1/objects/{obj.id}",
        json={"visibility": "tenant"},
    )
    assert resp.status_code == 200
    assert resp.json()["visibility"] == "tenant"
    assert resp.json()["storage_bucket"] == "private-media"
    mock_storage.copy_object.assert_not_called()
    mock_storage.remove_object.assert_not_called()


def test_update_object_relocation_failure_leaves_metadata_unchanged(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    mock_storage.copy_object.side_effect = RuntimeError("minio down")
    resp = client.patch(
        f"/media/v1/objects/{obj.id}",
        json={"visibility": "public"},
    )
    assert resp.status_code == 502
    mock_storage.remove_object.assert_not_called()
    session.refresh(obj)
    assert obj.visibility == MediaVisibility.PRIVATE
    assert obj.storage_bucket == "private-media"


def test_update_object_stale_copy_delete_failure_is_tolerated(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    mock_storage.remove_object.side_effect = RuntimeError("delete failed")
    resp = client.patch(
        f"/media/v1/objects/{obj.id}",
        json={"visibility": "public"},
    )
    # The move is committed even if cleanup of the old copy fails.
    assert resp.status_code == 200
    assert resp.json()["storage_bucket"] == "public-media"


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


def test_delete_object_soft_deletes(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    session.refresh(obj)
    assert obj.deleted_at is not None
    assert obj.status == MediaObjectStatus.DELETED
    # PRIVATE bytes are reachable only via presigned URLs; metadata soft-delete
    # is sufficient, so the stored object is left in place.
    mock_storage.remove_object.assert_not_called()


def test_delete_object_public_removes_bytes(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # A PUBLIC object is world-readable at a known URL; a soft-delete must also
    # remove the bytes so "deleted" content stops being served.
    obj = _make_object(
        session, current_user.id, visibility=MediaVisibility.PUBLIC
    )
    obj.storage_bucket = "public-media"
    session.add(obj)
    session.commit()
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    session.refresh(obj)
    assert obj.deleted_at is not None
    mock_storage.remove_object.assert_called_once_with(
        bucket="public-media", object_key=obj.object_key
    )


def test_delete_object_public_tolerates_remove_failure(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # Byte cleanup is best-effort: a storage error must not fail the delete or
    # leave the metadata un-soft-deleted.
    obj = _make_object(
        session, current_user.id, visibility=MediaVisibility.PUBLIC
    )
    obj.storage_bucket = "public-media"
    session.add(obj)
    session.commit()
    mock_storage.remove_object.side_effect = RuntimeError("delete failed")
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    session.refresh(obj)
    assert obj.deleted_at is not None


def test_delete_object_idempotent(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    obj = _make_object(session, current_user.id, deleted=True)
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    # Already deleted: no second attempt to remove bytes.
    mock_storage.remove_object.assert_not_called()


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
