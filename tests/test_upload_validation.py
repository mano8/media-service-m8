"""Tests for Phase 11: upload integrity validation."""

import hashlib
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.core.config import settings
from media_service.core.validation import (
    max_size_for_category,
    mime_consistent,
    sniff_mime,
    verify_sha256,
)
from media_service.db_models.media_objects import MediaObject, MediaObjectStatus
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus


# ── Magic-byte samples ────────────────────────────────────────────────────────

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 504
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 508
_PDF_BYTES = b"%PDF-1.4" + b"\x00" * 504


# ── Test helper ───────────────────────────────────────────────────────────────


def _make_session(
    session: Session,
    owner_id: uuid.UUID,
    *,
    mime_type: str = "application/pdf",
    expected_size_bytes: int = 2048,
) -> UploadSession:
    sid = uuid.uuid4()
    us = UploadSession(
        id=sid,
        owner_user_id=owner_id,
        category="document",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{sid}/original/file",
        expected_mime_type=mime_type,
        expected_size_bytes=expected_size_bytes,
        expires_at=datetime.utcnow() + timedelta(seconds=300),
    )
    session.add(us)
    session.commit()
    session.refresh(us)
    return us


def _stat(size: int = 2048, etag: str = "etag-test") -> MagicMock:
    m = MagicMock()
    m.size = size
    m.etag = etag
    return m


# ── sniff_mime ────────────────────────────────────────────────────────────────


def test_sniff_mime_png():
    assert sniff_mime(_PNG_BYTES) == "image/png"


def test_sniff_mime_jpeg():
    assert sniff_mime(_JPEG_BYTES) == "image/jpeg"


def test_sniff_mime_empty_bytes_returns_none():
    assert sniff_mime(b"") is None


def test_sniff_mime_short_bytes_returns_none():
    assert sniff_mime(b"\x00\x01") is None


def test_sniff_mime_non_bytes_returns_none():
    assert sniff_mime("not-bytes") is None  # type: ignore[arg-type]


def test_sniff_mime_non_bytes_object_returns_none():
    assert sniff_mime(MagicMock()) is None  # type: ignore[arg-type]


# ── mime_consistent ───────────────────────────────────────────────────────────


def test_mime_consistent_exact_match():
    assert mime_consistent("image/png", "image/png") is True


def test_mime_consistent_sniffable_sniffed_none_rejected():
    # PDF is a binary, sniffable format: an unidentified payload must fail closed.
    assert mime_consistent("application/pdf", None) is False


def test_mime_consistent_image_sniffed_none_rejected():
    # The SVG-as-image bypass: image/* declared but the sniffer can't see magic bytes.
    assert mime_consistent("image/png", None) is False


def test_mime_consistent_unsniffable_text_sniffed_none_allowed():
    assert mime_consistent("text/plain", None) is True


def test_mime_consistent_disallowed_declared_type_rejected():
    assert mime_consistent("image/svg+xml", "image/png") is False
    assert mime_consistent("text/html", None) is False


def test_mime_consistent_same_major_image():
    assert mime_consistent("image/png", "image/jpeg") is True


def test_mime_consistent_same_major_video():
    assert mime_consistent("video/mp4", "video/webm") is True


def test_mime_consistent_same_major_audio():
    assert mime_consistent("audio/mpeg", "audio/ogg") is True


def test_mime_consistent_different_major_types():
    assert mime_consistent("application/pdf", "image/png") is False


def test_mime_consistent_application_types_differ():
    assert mime_consistent("application/pdf", "application/zip") is False


def test_mime_consistent_text_vs_image():
    assert mime_consistent("text/plain", "image/png") is False


# ── verify_sha256 ─────────────────────────────────────────────────────────────


def test_verify_sha256_correct():
    data = b"hello world"
    digest = hashlib.sha256(data).hexdigest()
    assert verify_sha256(data, digest) is True


def test_verify_sha256_wrong_digest():
    assert verify_sha256(b"hello", "a" * 64) is False


def test_verify_sha256_case_insensitive():
    data = b"test"
    digest = hashlib.sha256(data).hexdigest().upper()
    assert verify_sha256(data, digest) is True


# ── max_size_for_category ─────────────────────────────────────────────────────


def test_max_size_for_category_default():
    assert max_size_for_category("document") == settings.MEDIA_MAX_UPLOAD_SIZE_BYTES


def test_max_size_for_category_override():
    with patch.object(
        settings, "MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY", {"avatar": 1_000_000}
    ):
        assert max_size_for_category("avatar") == 1_000_000
        assert max_size_for_category("document") == settings.MEDIA_MAX_UPLOAD_SIZE_BYTES


def test_max_size_for_category_empty_override():
    with patch.object(settings, "MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY", {}):
        assert max_size_for_category("asset") == settings.MEDIA_MAX_UPLOAD_SIZE_BYTES


# ── HTTP-level rejection tests ────────────────────────────────────────────────


def test_complete_upload_rejects_oversized(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat(
        size=settings.MEDIA_MAX_UPLOAD_SIZE_BYTES + 1
    )
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 422
    assert "size_exceeded" in resp.json()["detail"]


def test_complete_upload_rejects_mime_mismatch(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id, mime_type="application/pdf")
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = _PNG_BYTES
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 422
    assert "mime_mismatch" in resp.json()["detail"]


def test_complete_upload_rejects_sha256_mismatch(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = _PDF_BYTES
    mock_storage.get_object.return_value = b"real-content"
    wrong_sha256 = "b" * 64
    resp = client.post(
        f"/media/v1/uploads/{us.id}/complete", json={"sha256": wrong_sha256}
    )
    assert resp.status_code == 422
    assert "sha256_mismatch" in resp.json()["detail"]


def test_complete_upload_passes_with_correct_sha256(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    content = b"verified-file-content"
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = _PDF_BYTES
    mock_storage.get_object.return_value = content
    correct_sha256 = hashlib.sha256(content).hexdigest()
    resp = client.post(
        f"/media/v1/uploads/{us.id}/complete", json={"sha256": correct_sha256}
    )
    assert resp.status_code == 200
    assert resp.json()["media_object"]["sha256"] == correct_sha256


def test_complete_upload_skips_sha256_when_not_provided(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = _PDF_BYTES
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200
    mock_storage.get_object.assert_not_called()


def test_complete_upload_passes_when_sniff_unrecognised(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id, mime_type="text/plain")
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = b"just plain text content"
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200


def test_complete_upload_rejects_when_get_object_head_raises(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # Head read failed → the sniffable PDF type can't be verified → fail closed.
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.side_effect = Exception("storage error")
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 422
    assert "mime_mismatch" in resp.json()["detail"]


def test_complete_upload_sha256_get_object_raises_422(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = b""
    mock_storage.get_object.side_effect = Exception("storage read error")
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={"sha256": "a" * 64})
    assert resp.status_code == 422


def test_complete_upload_rejected_object_persisted(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id, mime_type="application/pdf")
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = _PNG_BYTES
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 422
    rejected_obj = session.get(MediaObject, us.id)
    assert rejected_obj is not None
    assert rejected_obj.status == MediaObjectStatus.REJECTED


def test_complete_upload_session_aborted_after_rejection(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id, mime_type="application/pdf")
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = _PNG_BYTES
    client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    session.refresh(us)
    assert us.status == UploadSessionStatus.ABORTED


def test_complete_upload_same_image_type_passes(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id, mime_type="image/png")
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = _JPEG_BYTES
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200


def test_complete_upload_rejects_unsniffable_payload_for_image(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    # Stored-XSS vector: image/* declared but the payload is text (SVG/HTML) the
    # sniffer cannot identify. Must be rejected rather than waved through.
    us = _make_session(session, current_user.id, mime_type="image/png")
    mock_storage.stat_object.return_value = _stat()
    mock_storage.get_object_head.return_value = b"<svg><script>alert(1)</script></svg>"
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 422
    assert "mime_mismatch" in resp.json()["detail"]


def test_complete_upload_category_size_override_rejects(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    with patch.object(
        settings, "MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY", {"document": 1024}
    ):
        us = _make_session(session, current_user.id)
        mock_storage.stat_object.return_value = _stat(size=2048)
        resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 422
    assert "size_exceeded" in resp.json()["detail"]


def test_complete_upload_category_size_override_passes(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    with patch.object(
        settings, "MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY", {"document": 10_000}
    ):
        us = _make_session(session, current_user.id)
        mock_storage.stat_object.return_value = _stat(size=2048)
        mock_storage.get_object_head.return_value = _PDF_BYTES
        resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200
