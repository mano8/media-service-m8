"""Tests for antivirus download gating and scan enqueue on complete_upload."""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session

from media_sdk_m8 import ScanJobPayload

from media_service.core.arq import SCAN_TASK
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    ScanStatus,
)
from media_service.db_models.upload_sessions import (
    UploadSession,
    UploadSessionStatus,
)

_PDF_BYTES = b"%PDF-1.4" + b"\x00" * 504


def _make_object(
    session: Session, owner_id: uuid.UUID, scan_status: ScanStatus
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category="document",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{oid}/original/file.pdf",
        original_filename="file.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        status=MediaObjectStatus.UPLOADED,
        scan_status=scan_status,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def test_download_blocked_until_clean(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id, ScanStatus.PENDING)
    resp = client.get(f"/media/v1/objects/{obj.id}/download-url")
    assert resp.status_code == 409


def test_download_blocked_when_quarantined(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id, ScanStatus.QUARANTINED)
    resp = client.get(f"/media/v1/objects/{obj.id}/download-url")
    assert resp.status_code == 409


def test_download_allowed_when_clean(
    client: TestClient, mock_storage, session: Session, current_user
):
    obj = _make_object(session, current_user.id, ScanStatus.CLEAN)
    mock_storage.presigned_get_object.return_value = "https://minio/download"
    resp = client.get(f"/media/v1/objects/{obj.id}/download-url")
    assert resp.status_code == 200


def _make_upload_session(session: Session, owner_id: uuid.UUID) -> UploadSession:
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
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=300),
    )
    session.add(us)
    session.commit()
    session.refresh(us)
    return us


def test_complete_upload_enqueues_scan(
    client: TestClient, mock_storage, session: Session, current_user, fake_arq_pool
):
    us = _make_upload_session(session, current_user.id)
    stat = mock_storage.stat_object.return_value
    stat.size = 2048
    stat.etag = "etag123"
    mock_storage.get_object_head.return_value = _PDF_BYTES
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200
    media_id = uuid.UUID(resp.json()["media_object"]["id"])
    fake_arq_pool.enqueue_job.assert_awaited_once()
    args, _ = fake_arq_pool.enqueue_job.await_args
    assert args[0] == SCAN_TASK
    payload = args[1]
    assert isinstance(payload, ScanJobPayload)
    assert payload.object_id == media_id
    assert payload.owner_user_id == uuid.UUID(str(current_user.id))
