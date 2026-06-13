"""Tests for app/routes/uploads.py (initiate / complete / abort)."""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlmodel import Session

from auth_sdk_m8.schemas.user import UserModel

from media_service.controllers.uploads import UploadsController
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus
from media_service.schemas.uploads import UploadInitiateRequest


# ── helpers ───────────────────────────────────────────────────────────────────

_INITIATE_BODY = {
    "category": "document",
    "visibility": "private",
    "original_filename": "report.pdf",
    "mime_type": "application/pdf",
    "expected_size_bytes": 2048,
}

# Leading bytes the sniffer recognises as application/pdf — matches the declared
# type used by `_make_session`, so the content-validation step passes.
_PDF_BYTES = b"%PDF-1.4" + b"\x00" * 504


def _make_session(
    session: Session,
    owner_id: uuid.UUID,
    *,
    status: UploadSessionStatus = UploadSessionStatus.INITIATED,
    expires_offset: int = 300,
) -> UploadSession:
    """Insert an UploadSession with configurable status and expiry."""
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
        status=status,
        expires_at=datetime.utcnow() + timedelta(seconds=expires_offset),
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


# ── POST /media/v1/uploads/initiate ──────────────────────────────────────────


def test_initiate_upload_returns_presigned_post_form(
    client: TestClient, mock_storage: MagicMock
):
    mock_storage.presigned_post_object.return_value = (
        "https://minio/private-media",
        {"key": "k", "Content-Type": "application/pdf", "policy": "p"},
    )
    resp = client.post("/media/v1/uploads/initiate", json=_INITIATE_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert data["upload_url"] == "https://minio/private-media"
    assert data["upload_fields"]["key"] == "k"
    assert "session_id" in data
    assert "expires_at" in data


def test_initiate_upload_constrains_size_and_content_type(
    client: TestClient, mock_storage: MagicMock
):
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    resp = client.post("/media/v1/uploads/initiate", json=_INITIATE_BODY)
    assert resp.status_code == 200
    # The POST policy is signed for the declared type and a finite size cap, so
    # storage rejects oversized/garbage bodies before they land.
    _, kwargs = mock_storage.presigned_post_object.call_args
    assert kwargs["content_type"] == "application/pdf"
    assert kwargs["max_size_bytes"] > 0


def test_initiate_upload_creates_session_in_db(
    client: TestClient, mock_storage: MagicMock, session: Session
):
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    resp = client.post("/media/v1/uploads/initiate", json=_INITIATE_BODY)
    sid = uuid.UUID(resp.json()["session_id"])
    upload_session = session.get(UploadSession, sid)
    assert upload_session is not None
    assert upload_session.status == UploadSessionStatus.INITIATED


def test_initiate_upload_ignores_client_tenant_id(
    client: TestClient, mock_storage: MagicMock, session: Session
):
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    body = {**_INITIATE_BODY, "tenant_id": str(uuid.uuid4())}
    resp = client.post("/media/v1/uploads/initiate", json=body)
    assert resp.status_code == 200
    sid = uuid.UUID(resp.json()["session_id"])
    stored = session.get(UploadSession, sid)
    assert stored is not None
    assert stored.tenant_id is None


def test_initiate_upload_stamps_tenant_from_claim(
    mock_storage: MagicMock, session: Session
):
    # Tenancy comes from the authenticated principal's signed claim (not the
    # request body): a tenanted caller's session is tagged with that tenant, so
    # the promoted object inherits it and TENANT visibility resolves correctly.
    tenant = uuid.uuid4()
    user = UserModel(
        id=uuid.uuid4(), email="tenant@example.com", is_active=True, tenant_id=tenant
    )
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    resp = UploadsController.initiate_upload(
        session=session,
        current_user=user,
        req=UploadInitiateRequest(**_INITIATE_BODY),
        storage=mock_storage,
    )
    stored = session.get(UploadSession, resp.session_id)
    assert stored is not None
    assert stored.tenant_id == tenant


def test_initiate_upload_rejects_disallowed_mime(
    client: TestClient, mock_storage: MagicMock
):
    # image/svg+xml is markup that can carry <script> — never issue a URL for it.
    body = {**_INITIATE_BODY, "mime_type": "image/svg+xml"}
    resp = client.post("/media/v1/uploads/initiate", json=body)
    assert resp.status_code == 422
    mock_storage.presigned_post_object.assert_not_called()


# ── POST /media/v1/uploads/{id}/complete ─────────────────────────────────────


def test_complete_upload_happy_path(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat_mock()
    mock_storage.get_object_head.return_value = _PDF_BYTES
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200
    assert "media_object" in resp.json()
    session.refresh(us)
    assert us.status == UploadSessionStatus.COMPLETED


def test_complete_upload_pins_stored_content_type(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat_mock()
    mock_storage.get_object_head.return_value = _PDF_BYTES
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200
    # The client-chosen PUT Content-Type is overwritten with the validated type.
    mock_storage.set_object_content_type.assert_called_once_with(
        bucket=us.storage_bucket,
        object_key=us.object_key,
        content_type="application/pdf",
    )


def test_complete_upload_fails_when_content_type_pin_fails(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat_mock()
    mock_storage.get_object_head.return_value = _PDF_BYTES
    mock_storage.set_object_content_type.side_effect = Exception("copy failed")
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 422
    # The session is not promoted when the content type cannot be pinned.
    session.refresh(us)
    assert us.status == UploadSessionStatus.INITIATED


def test_complete_upload_not_found(client: TestClient):
    resp = client.post(f"/media/v1/uploads/{uuid.uuid4()}/complete", json={})
    assert resp.status_code == 404


def test_complete_upload_forbidden_different_owner(
    client: TestClient, session: Session, superuser
):
    us = _make_session(session, superuser.id)
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 403


def test_complete_upload_already_completed(
    client: TestClient, session: Session, current_user
):
    us = _make_session(session, current_user.id, status=UploadSessionStatus.COMPLETED)
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 409


def test_complete_upload_expired_session(
    client: TestClient, session: Session, current_user
):
    us = _make_session(session, current_user.id, expires_offset=-1)
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 409
    session.refresh(us)
    assert us.status == UploadSessionStatus.EXPIRED


def test_complete_upload_file_not_in_storage(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.side_effect = Exception("NoSuchKey")
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 422


def test_complete_upload_with_sha256(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    content = b"test-file-content-for-sha256"
    correct_sha256 = hashlib.sha256(content).hexdigest()
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat_mock()
    mock_storage.get_object_head.return_value = _PDF_BYTES
    mock_storage.get_object.return_value = content
    resp = client.post(
        f"/media/v1/uploads/{us.id}/complete",
        json={"sha256": correct_sha256},
    )
    assert resp.status_code == 200
    obj = resp.json()["media_object"]
    assert obj["sha256"] == correct_sha256


def test_complete_upload_sha256_read_failure_returns_422(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat_mock()
    mock_storage.get_object_head.return_value = _PDF_BYTES
    mock_storage.get_object.side_effect = Exception("read failed")
    resp = client.post(
        f"/media/v1/uploads/{us.id}/complete",
        json={"sha256": "a" * 64},
    )
    assert resp.status_code == 422


def test_complete_upload_superuser_can_complete_any(
    superuser_client: TestClient,
    mock_storage: MagicMock,
    session: Session,
    current_user,
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat_mock()
    mock_storage.get_object_head.return_value = _PDF_BYTES
    resp = superuser_client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200


# ── POST /media/v1/uploads/{id}/abort ────────────────────────────────────────


def test_abort_upload_happy_path(client: TestClient, session: Session, current_user):
    us = _make_session(session, current_user.id)
    resp = client.post(f"/media/v1/uploads/{us.id}/abort")
    assert resp.status_code == 204
    session.refresh(us)
    assert us.status == UploadSessionStatus.ABORTED


def test_abort_upload_calls_remove_object(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    resp = client.post(f"/media/v1/uploads/{us.id}/abort")
    assert resp.status_code == 204
    mock_storage.remove_object.assert_called_once()


def test_abort_upload_tolerates_storage_error(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.remove_object.side_effect = Exception("bucket gone")
    resp = client.post(f"/media/v1/uploads/{us.id}/abort")
    assert resp.status_code == 204


def test_abort_upload_not_found(client: TestClient):
    resp = client.post(f"/media/v1/uploads/{uuid.uuid4()}/abort")
    assert resp.status_code == 404


def test_abort_upload_forbidden(client: TestClient, session: Session, superuser):
    us = _make_session(session, superuser.id)
    resp = client.post(f"/media/v1/uploads/{us.id}/abort")
    assert resp.status_code == 403


def test_abort_upload_completed_returns_409(
    client: TestClient, session: Session, current_user
):
    us = _make_session(session, current_user.id, status=UploadSessionStatus.COMPLETED)
    resp = client.post(f"/media/v1/uploads/{us.id}/abort")
    assert resp.status_code == 409


def test_abort_upload_already_aborted_is_idempotent(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id, status=UploadSessionStatus.ABORTED)
    resp = client.post(f"/media/v1/uploads/{us.id}/abort")
    assert resp.status_code == 204
    # remove_object should NOT be called for non-INITIATED sessions
    mock_storage.remove_object.assert_not_called()


# ── Rate limit ────────────────────────────────────────────────────────────────


def test_initiate_upload_rate_limited(client: TestClient, mock_redis: MagicMock):
    mock_redis.incr.return_value = 21
    resp = client.post("/media/v1/uploads/initiate", json=_INITIATE_BODY)
    assert resp.status_code == 429


def test_complete_upload_rate_limited(
    client: TestClient, mock_redis: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_redis.incr.return_value = 21
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 429


# ── Controller unit test: timezone-aware expires_at ──────────────────────────


def test_complete_upload_tz_aware_expires_at(mock_storage: MagicMock, current_user):
    """Branch: expires_at carrying tzinfo is stripped before comparison."""
    from media_service.controllers.uploads import UploadsController
    from media_service.schemas.uploads import UploadCompleteRequest

    session_id = uuid.uuid4()
    owner_id = uuid.UUID(str(current_user.id))

    us = UploadSession(
        id=session_id,
        owner_user_id=owner_id,
        category="document",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{session_id}/f.pdf",
        expected_mime_type="application/pdf",
        expected_size_bytes=1024,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
    )

    mock_sess = MagicMock()
    mock_sess.get.return_value = us

    stat = MagicMock()
    stat.size = 1024
    stat.etag = "etag-tz"
    mock_storage.stat_object.return_value = stat
    mock_storage.get_object_head.return_value = _PDF_BYTES

    result = UploadsController.complete_upload(
        session=mock_sess,
        current_user=current_user,
        session_id=session_id,
        req=UploadCompleteRequest(),
        storage=mock_storage,
    )
    assert result.media_object is not None
