"""Tests for app/routes/objects.py (get / download-url / update / delete)."""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.core.config import settings
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    visibility: MediaVisibility = MediaVisibility.PRIVATE,
    status: MediaObjectStatus = MediaObjectStatus.UPLOADED,
    scan_status: ScanStatus = ScanStatus.CLEAN,
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
        scan_status=scan_status,
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
    mock_storage.presigned_get_object.return_value = "https://storage/download"
    resp = client.get(f"/media/v1/objects/{obj.id}/download-url")
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://storage/download"
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
    mock_storage.copy_object.side_effect = RuntimeError("storage down")
    resp = client.patch(
        f"/media/v1/objects/{obj.id}",
        json={"visibility": "public"},
    )
    assert resp.status_code == 502
    mock_storage.remove_object.assert_not_called()
    session.refresh(obj)
    assert obj.visibility == MediaVisibility.PRIVATE
    assert obj.storage_bucket == "private-media"


def test_update_object_commit_failure_removes_relocated_copy(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # If the metadata commit fails after a PRIVATE->PUBLIC copy landed, the
    # orphaned (and world-readable) destination copy must be cleaned up before
    # the error surfaces, leaving no bytes the metadata no longer points at.
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    object_key = obj.object_key
    session.commit = MagicMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        client.patch(
            f"/media/v1/objects/{obj.id}",
            json={"visibility": "public"},
        )
    mock_storage.copy_object.assert_called_once()
    mock_storage.remove_object.assert_called_once_with(
        bucket="public-media", object_key=object_key
    )


def test_update_object_commit_failure_same_bucket_no_cleanup(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # PRIVATE→TENANT maps to the same bucket, so no relocation occurs (old_bucket
    # is None). If the commit still fails, the error must surface without any
    # attempt to remove the destination copy (there is none).
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    session.commit = MagicMock(side_effect=RuntimeError("db down"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        client.patch(
            f"/media/v1/objects/{obj.id}",
            json={"visibility": "tenant"},
        )
    mock_storage.copy_object.assert_not_called()
    mock_storage.remove_object.assert_not_called()


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


def test_delete_object_soft_deletes_and_archives_bytes(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # The archive-tier writer: a soft-delete cold-moves the original out of
    # its visibility bucket into S3_BUCKET_ARCHIVE (copy, repoint, then drop
    # the source), where the hard purge later reclaims it.
    obj = _make_object(session, current_user.id)
    key = obj.object_key
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    session.refresh(obj)
    assert obj.deleted_at is not None
    assert obj.status == MediaObjectStatus.DELETED
    assert obj.storage_bucket == settings.S3_BUCKET_ARCHIVE
    mock_storage.copy_object.assert_called_once_with(
        src_bucket="private-media",
        src_object_key=key,
        dest_bucket=settings.S3_BUCKET_ARCHIVE,
        dest_object_key=key,
    )
    mock_storage.remove_object.assert_called_once_with(
        bucket="private-media", object_key=key
    )


def test_delete_object_source_removed_only_after_commit(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # Ordering contract: the archive copy lands before the soft-delete commits
    # and the source copy is dropped after it, so the row never points at
    # bytes that are not there.
    obj = _make_object(session, current_user.id)
    order: list[str] = []
    mock_storage.copy_object.side_effect = lambda **_: order.append("copy")
    mock_storage.remove_object.side_effect = lambda **_: order.append("remove")
    original_commit = session.commit

    def _commit() -> None:
        order.append("commit")
        original_commit()

    session.commit = _commit  # type: ignore[method-assign]
    try:
        resp = client.delete(f"/media/v1/objects/{obj.id}")
    finally:
        session.commit = original_commit  # type: ignore[method-assign]
    assert resp.status_code == 204
    assert order == ["copy", "commit", "remove"]


def test_delete_object_private_tolerates_archive_copy_failure(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # Archival is best-effort: a failed copy must not fail the delete, and the
    # row keeps pointing at the bucket the bytes are really in. PRIVATE bytes
    # are reachable only via presigned URLs, so they are left in place.
    obj = _make_object(session, current_user.id)
    mock_storage.copy_object.side_effect = RuntimeError("copy failed")
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    session.refresh(obj)
    assert obj.deleted_at is not None
    assert obj.storage_bucket == "private-media"
    mock_storage.remove_object.assert_not_called()


def test_delete_object_public_archives_and_removes_public_bytes(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # A PUBLIC object is world-readable at a known URL; after the archive move
    # the public copy is removed so "deleted" content stops being served, while
    # the archived copy stays recoverable for the retention window.
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PUBLIC)
    obj.storage_bucket = "public-media"
    session.add(obj)
    session.commit()
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    session.refresh(obj)
    assert obj.deleted_at is not None
    assert obj.storage_bucket == settings.S3_BUCKET_ARCHIVE
    mock_storage.copy_object.assert_called_once_with(
        src_bucket="public-media",
        src_object_key=obj.object_key,
        dest_bucket=settings.S3_BUCKET_ARCHIVE,
        dest_object_key=obj.object_key,
    )
    mock_storage.remove_object.assert_called_once_with(
        bucket="public-media", object_key=obj.object_key
    )


def test_delete_object_public_removes_bytes_when_archive_copy_fails(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # The pre-archive guarantee survives a storage hiccup: if the archive copy
    # fails, the public bytes are still removed from their known URL.
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PUBLIC)
    obj.storage_bucket = "public-media"
    session.add(obj)
    session.commit()
    mock_storage.copy_object.side_effect = RuntimeError("copy failed")
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    session.refresh(obj)
    assert obj.deleted_at is not None
    assert obj.storage_bucket == "public-media"
    mock_storage.remove_object.assert_called_once_with(
        bucket="public-media", object_key=obj.object_key
    )


def test_delete_object_public_tolerates_remove_failure(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # Byte cleanup is best-effort: a storage error must not fail the delete or
    # leave the metadata un-soft-deleted.
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PUBLIC)
    obj.storage_bucket = "public-media"
    session.add(obj)
    session.commit()
    mock_storage.remove_object.side_effect = RuntimeError("delete failed")
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    session.refresh(obj)
    assert obj.deleted_at is not None
    assert obj.storage_bucket == settings.S3_BUCKET_ARCHIVE


def test_delete_object_already_archived_is_not_copied_again(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # A row already living in the archive bucket has nothing to move.
    obj = _make_object(session, current_user.id)
    obj.storage_bucket = settings.S3_BUCKET_ARCHIVE
    session.add(obj)
    session.commit()
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    mock_storage.copy_object.assert_not_called()
    mock_storage.remove_object.assert_not_called()


def test_delete_object_idempotent(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    obj = _make_object(session, current_user.id, deleted=True)
    resp = client.delete(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 204
    # Already deleted: no second attempt to archive or remove bytes.
    mock_storage.copy_object.assert_not_called()
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
