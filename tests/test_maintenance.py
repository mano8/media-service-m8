"""Tests for controllers/maintenance.py — lifecycle, retention, reconciliation."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from sqlmodel import Session

from media_service.controllers.maintenance import MaintenanceController
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
)
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus

# Frozen "now" used to make the retention cutoff deterministic.
_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
_OLDER_THAN = timedelta(days=30)
_CUTOFF = _NOW - _OLDER_THAN


def _freeze_now(monkeypatch, now: datetime = _NOW) -> None:
    monkeypatch.setattr("media_service.controllers.maintenance.utcnow", lambda: now)


def _make_object(
    session: Session,
    *,
    status: MediaObjectStatus = MediaObjectStatus.UPLOADED,
    deleted_at: datetime | None = None,
    created_at: datetime | None = None,
    bucket: str = "private-media",
    object_key: str | None = None,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=uuid.uuid4(),
        category="document",
        visibility=MediaVisibility.PRIVATE,
        storage_bucket=bucket,
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


def _make_initiated_session(
    session: Session, *, bucket: str, object_key: str
) -> UploadSession:
    sid = uuid.uuid4()
    us = UploadSession(
        id=sid,
        owner_user_id=uuid.uuid4(),
        category="document",
        visibility="private",
        storage_bucket=bucket,
        object_key=object_key,
        expected_mime_type="application/pdf",
        expected_size_bytes=1024,
        expires_at=datetime.utcnow() + timedelta(seconds=300),
    )
    session.add(us)
    session.commit()
    return us


# ── hard_purge_expired ────────────────────────────────────────────────────────


def test_hard_purge_removes_only_expired_deleted_rows(
    session: Session, mock_storage: MagicMock, monkeypatch
):
    _freeze_now(monkeypatch)
    old = _make_object(
        session,
        status=MediaObjectStatus.DELETED,
        deleted_at=_CUTOFF - timedelta(days=1),
        bucket="archive-media",
        object_key="old-key",
    )
    recent = _make_object(
        session,
        status=MediaObjectStatus.DELETED,
        deleted_at=_CUTOFF + timedelta(days=1),
    )
    active = _make_object(session, status=MediaObjectStatus.UPLOADED)

    result = MaintenanceController.hard_purge_expired(
        session=session, storage=mock_storage, older_than=_OLDER_THAN, limit=500
    )

    assert result.purged == 1
    # Bytes removed from the bucket as stored (archive bucket, not re-derived).
    mock_storage.remove_object.assert_called_once_with(
        bucket="archive-media", object_key="old-key"
    )
    assert session.get(MediaObject, old.id) is None
    assert session.get(MediaObject, recent.id) is not None
    assert session.get(MediaObject, active.id) is not None


def test_hard_purge_empty_is_noop(
    session: Session, mock_storage: MagicMock, monkeypatch
):
    _freeze_now(monkeypatch)
    result = MaintenanceController.hard_purge_expired(
        session=session, storage=mock_storage, older_than=_OLDER_THAN, limit=500
    )
    assert result.purged == 0
    mock_storage.remove_object.assert_not_called()


def test_hard_purge_reasserts_invariant_on_concurrent_restore(
    session: Session, mock_storage: MagicMock, monkeypatch
):
    _freeze_now(monkeypatch)
    first = _make_object(
        session,
        status=MediaObjectStatus.DELETED,
        deleted_at=_CUTOFF - timedelta(days=5),
        object_key="first",
    )
    second = _make_object(
        session,
        status=MediaObjectStatus.DELETED,
        deleted_at=_CUTOFF - timedelta(days=1),
        object_key="second",
    )

    # Simulate a restore landing between SELECT and the per-row delete: removing
    # the first object's bytes "restores" the second so its re-check skips it.
    def _restore_second(**_kw):
        second.deleted_at = None

    mock_storage.remove_object.side_effect = _restore_second

    result = MaintenanceController.hard_purge_expired(
        session=session, storage=mock_storage, older_than=_OLDER_THAN, limit=500
    )

    assert result.purged == 1
    assert session.get(MediaObject, first.id) is None
    assert session.get(MediaObject, second.id) is not None


def test_hard_purge_swallows_storage_removal_failure(
    session: Session, mock_storage: MagicMock, monkeypatch
):
    _freeze_now(monkeypatch)
    obj = _make_object(
        session,
        status=MediaObjectStatus.DELETED,
        deleted_at=_CUTOFF - timedelta(days=1),
    )
    mock_storage.remove_object.side_effect = RuntimeError("minio down")

    result = MaintenanceController.hard_purge_expired(
        session=session, storage=mock_storage, older_than=_OLDER_THAN, limit=500
    )

    # Best-effort: the row is still hard-deleted even when byte removal fails.
    assert result.purged == 1
    assert session.get(MediaObject, obj.id) is None


# ── expire_stale_uploads ──────────────────────────────────────────────────────


def test_expire_stale_uploads_delegates_to_admin(
    session: Session, mock_storage: MagicMock
):
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

    result = MaintenanceController.expire_stale_uploads(
        session=session, storage=mock_storage
    )

    assert result.purged == 1
    session.refresh(us)
    assert us.status == UploadSessionStatus.EXPIRED
    mock_storage.remove_object.assert_called_once()


# ── reconcile_orphans ─────────────────────────────────────────────────────────


def test_reconcile_reports_both_orphan_directions(
    session: Session, mock_storage: MagicMock
):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    present = _make_object(session, created_at=past, object_key="present-key")
    missing = _make_object(session, created_at=past, object_key="missing-key")
    _make_initiated_session(session, bucket="private-media", object_key="pending-key")

    def _stat(**kw):
        if kw["object_key"] == "missing-key":
            raise FileNotFoundError("no bytes")
        return MagicMock()

    mock_storage.stat_object.side_effect = _stat
    mock_storage.list_object_keys.return_value = [
        "present-key",  # has a row → not an orphan
        "pending-key",  # still uploading → excluded
        "orphan-1",  # bytes with no row → storage orphan
        "orphan-2",
    ]

    report = MaintenanceController.reconcile_orphans(
        session=session,
        storage=mock_storage,
        buckets=["private-media"],
        grace=timedelta(0),
        limit=1000,
        repair=False,
    )

    assert report.db_orphan_count == 1
    assert report.db_orphans[0].object_key == "missing-key"
    assert report.db_orphans[0].object_id == missing.id
    assert report.storage_orphan_count == 2
    assert {o.object_key for o in report.storage_orphans} == {"orphan-1", "orphan-2"}
    assert report.repaired == 0
    # Report-only must never delete.
    mock_storage.remove_object.assert_not_called()
    # The healthy object is left untouched.
    assert session.get(MediaObject, present.id) is not None


def test_reconcile_excludes_rows_within_grace_window(
    session: Session, mock_storage: MagicMock
):
    # Created "now" → within a 60-minute grace window → never stat'd / flagged.
    _make_object(session, object_key="fresh-key")
    mock_storage.list_object_keys.return_value = []

    report = MaintenanceController.reconcile_orphans(
        session=session,
        storage=mock_storage,
        buckets=["private-media"],
        grace=timedelta(minutes=60),
        limit=1000,
    )

    assert report.db_orphan_count == 0
    mock_storage.stat_object.assert_not_called()


def test_reconcile_repair_deletes_storage_orphans_up_to_limit(
    session: Session, mock_storage: MagicMock
):
    mock_storage.list_object_keys.return_value = ["o1", "o2", "o3"]

    report = MaintenanceController.reconcile_orphans(
        session=session,
        storage=mock_storage,
        buckets=["private-media"],
        grace=timedelta(0),
        limit=2,
        repair=True,
    )

    # limit caps the batch at 2; the third key is never inspected.
    assert report.storage_orphan_count == 2
    assert report.repaired == 2
    assert mock_storage.remove_object.call_count == 2


def test_reconcile_repair_is_best_effort_on_delete_failure(
    session: Session, mock_storage: MagicMock
):
    mock_storage.list_object_keys.return_value = ["o1"]
    mock_storage.remove_object.side_effect = RuntimeError("minio down")

    report = MaintenanceController.reconcile_orphans(
        session=session,
        storage=mock_storage,
        buckets=["private-media"],
        grace=timedelta(0),
        limit=10,
        repair=True,
    )

    assert report.storage_orphan_count == 1
    assert report.repaired == 1  # counted as attempted; failure swallowed
