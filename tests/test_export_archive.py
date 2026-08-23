"""``archive`` export — request, assembly and collection (`U9`).

One file for the whole asynchronous half of the export surface, because the
three parts only make sense against each other:

* ``POST /media/v1/export`` with ``format="archive"`` resolves and bounds the
  request, records an ``ExportJob``, and enqueues it on the *maintenance*
  queue (the service-owned worker, not the DB-free media-worker-m8).
* :class:`ExportArchiveController` assembles the zip off the request path —
  ``manifest.json`` plus one entry per object whose bytes may actually be
  handed out — streaming both directions and never holding the archive in
  memory.
* ``GET /media/v1/export/{job_id}`` reports progress and mints the presigned
  download, once, while the archive is still live.

Storage is a hand-written double rather than the shared ``mock_storage``
fixture, because these tests assert on the bytes: the double captures whatever
the streaming put writes so the assembled zip can be opened and asserted
against as a real archive, which a ``MagicMock(spec=ObjectStorage)`` cannot do.
Its ``put_object_stream`` mirrors the SDK's — delegating to the underlying
MinIO client, reading the handle from its current position — so the assembly's
declared length and its no-buffering contract are exercised here too.
"""

import io
import uuid
import zipfile
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import media_service.maintenance_worker as mw
from media_service.controllers.shares import _as_aware
from media_service.controllers.export_archive import (
    ExportArchiveController,
    ExportPrincipal,
    archive_entry_name,
)
from media_service.controllers.transfer import TransferController
from media_service.core.config import settings
from media_service.db_models.categories import Category
from media_service.db_models.export_jobs import ExportJob, ExportJobStatus
from media_service.db_models.media_object_categories import MediaObjectCategoryLink
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
    utcnow,
)

EXPORT_URL = "/media/v1/export"
BUCKET = "private-media"


# ── doubles / builders ───────────────────────────────────────────────────────


class _FakeMinioClient:
    """The underlying client ``ObjectStorage.put_object_stream`` delegates to."""

    def __init__(self, storage: "_FakeStorage") -> None:
        self._storage = storage

    def put_object(self, bucket, object_key, data, length, content_type):
        if self._storage.fail_put:
            raise OSError("bucket is unavailable")
        payload = data.read()
        assert len(payload) == length, "declared length must match the bytes written"
        self._storage.uploads.append((bucket, object_key, payload, content_type))


class _FakeStorage:
    """Streams canned object bytes out and captures whatever is written back."""

    def __init__(
        self,
        blobs: dict[str, bytes] | None = None,
        *,
        fail_stream_for: str | None = None,
        fail_put: bool = False,
    ) -> None:
        self.blobs = blobs or {}
        self.fail_stream_for = fail_stream_for
        self.fail_put = fail_put
        self.uploads: list[tuple[str, str, bytes, str]] = []
        self.removed: list[tuple[str, str]] = []
        self.client = _FakeMinioClient(self)

    def put_object_stream(self, *, bucket, object_key, data, length, content_type):
        self.client.put_object(bucket, object_key, data, length, content_type)

    def stream_object(self, *, bucket, object_key, chunk_size=1024):
        if self.fail_stream_for is not None and self.fail_stream_for in object_key:
            raise OSError("storage went away mid-stream")
        data = self.blobs[object_key]
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    def remove_object(self, *, bucket, object_key):
        self.removed.append((bucket, object_key))

    @property
    def archive(self) -> zipfile.ZipFile:
        """The single uploaded archive, opened for reading."""
        assert len(self.uploads) == 1
        return zipfile.ZipFile(io.BytesIO(self.uploads[0][2]))


def _fake_engine(session: Session) -> MagicMock:
    """A stand-in DbEngine whose ``session()`` context yields the test session."""
    engine = MagicMock()
    engine.session.return_value.__enter__.return_value = session
    engine.session.return_value.__exit__.return_value = False
    return engine


def _make_object(
    session: Session,
    owner_id,
    *,
    filename: str = "file.pdf",
    scan_status: ScanStatus = ScanStatus.CLEAN,
    status: MediaObjectStatus = MediaObjectStatus.READY,
    tenant_id: uuid.UUID | None = None,
    visibility: MediaVisibility = MediaVisibility.PRIVATE,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        tenant_id=tenant_id,
        category=MediaCategory.DOCUMENT,
        visibility=visibility,
        storage_bucket=BUCKET,
        object_key=f"users/{owner_id}/document/{oid}/original/{filename}",
        original_filename=filename,
        mime_type="application/pdf",
        size_bytes=1024,
        sha256="a" * 64,
        status=status,
        scan_status=scan_status,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _storage_for(*objects: MediaObject, **kwargs) -> _FakeStorage:
    """Canned bytes for every object, sized to match its ``size_bytes``."""
    return _FakeStorage(
        {obj.object_key: b"x" * obj.size_bytes for obj in objects}, **kwargs
    )


def _queued_job(
    session: Session,
    owner_id,
    *,
    tenant_id: uuid.UUID | None = None,
    is_superuser: bool = False,
    filters: dict | None = None,
    status: ExportJobStatus = ExportJobStatus.QUEUED,
) -> ExportJob:
    job = ExportJob(
        owner_user_id=owner_id,
        tenant_id=tenant_id,
        is_superuser=is_superuser,
        status=status,
        filters=filters or {},
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _completed_job(
    session: Session,
    owner_id,
    *,
    expires_in: timedelta = timedelta(hours=1),
) -> ExportJob:
    job = ExportJob(
        owner_user_id=owner_id,
        status=ExportJobStatus.COMPLETED,
        object_count=2,
        total_size_bytes=2048,
        storage_bucket="temp-media",
        object_key=f"users/{owner_id}/exports/{uuid.uuid4()}.zip",
        size_bytes=4096,
        expires_at=utcnow() + expires_in,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# ── 1. starting an export ────────────────────────────────────────────────────


def test_archive_export_creates_a_queued_job_and_enqueues_it(
    client: TestClient, session: Session, current_user, fake_arq_pool
):
    _make_object(session, current_user.id, filename="a.pdf")
    _make_object(session, current_user.id, filename="b.pdf")

    resp = client.post(EXPORT_URL, json={"format": "archive"})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["object_count"] == 2
    assert body["total_size_bytes"] == 2048
    assert body["download_url"] is None
    assert body["size_bytes"] is None

    job = session.get(ExportJob, uuid.UUID(body["id"]))
    assert job is not None
    assert job.owner_user_id == uuid.UUID(str(current_user.id))
    assert job.status == ExportJobStatus.QUEUED
    assert job.storage_bucket is None and job.object_key is None

    # Enqueued on the maintenance queue: the consumer is the service-owned
    # worker, not the media-worker-m8 that drains the default queue.
    fake_arq_pool.enqueue_job.assert_awaited_once()
    args, kwargs = fake_arq_pool.enqueue_job.await_args
    assert args == ("build_export_archive", job.id)
    assert kwargs == {"_job_id": str(job.id), "_queue_name": "arq:maintenance"}


def test_archive_export_records_the_scope_it_was_authorized_for(
    client: TestClient, session: Session, current_user
):
    root = Category(name="Docs", slug="docs", owner_id=current_user.id)
    session.add(root)
    session.commit()
    session.refresh(root)
    filed = _make_object(session, current_user.id, filename="filed.pdf")
    _make_object(session, current_user.id, filename="loose.pdf")
    session.add(MediaObjectCategoryLink(media_object_id=filed.id, category_id=root.id))
    session.commit()

    resp = client.post(
        EXPORT_URL, json={"format": "archive", "filters": {"category_id": root.id}}
    )

    assert resp.status_code == 202
    assert resp.json()["object_count"] == 1
    job = session.get(ExportJob, uuid.UUID(resp.json()["id"]))
    assert job.filters["category_id"] == root.id
    assert job.is_superuser is False


def test_archive_export_weighs_only_the_bytes_it_can_carry(
    client: TestClient, session: Session, current_user
):
    """A quarantined object is manifested but never zipped, so it weighs 0."""
    _make_object(session, current_user.id, filename="clean.pdf")
    _make_object(
        session,
        current_user.id,
        filename="infected.pdf",
        scan_status=ScanStatus.INFECTED,
        status=MediaObjectStatus.REJECTED,
    )

    resp = client.post(EXPORT_URL, json={"format": "archive"})

    assert resp.status_code == 202
    assert resp.json()["object_count"] == 2
    assert resp.json()["total_size_bytes"] == 1024


def test_archive_export_refuses_a_second_in_flight_export(
    client: TestClient, session: Session
):
    assert client.post(EXPORT_URL, json={"format": "archive"}).status_code == 202

    second = client.post(EXPORT_URL, json={"format": "archive"})

    assert second.status_code == 409
    assert len(session.exec(select(ExportJob)).all()) == 1


def test_a_finished_export_does_not_block_the_next_one(
    client: TestClient, session: Session
):
    first = client.post(EXPORT_URL, json={"format": "archive"})
    job = session.get(ExportJob, uuid.UUID(first.json()["id"]))
    job.status = ExportJobStatus.FAILED
    session.add(job)
    session.commit()

    assert client.post(EXPORT_URL, json={"format": "archive"}).status_code == 202


def test_archive_export_refuses_more_objects_than_the_ceiling(
    client: TestClient, session: Session, current_user, monkeypatch
):
    monkeypatch.setattr(settings, "MEDIA_EXPORT_MAX_OBJECTS", 1)
    _make_object(session, current_user.id, filename="a.pdf")
    _make_object(session, current_user.id, filename="b.pdf")

    resp = client.post(EXPORT_URL, json={"format": "archive"})

    assert resp.status_code == 422
    assert "ceiling" in resp.json()["detail"]
    assert session.exec(select(ExportJob)).all() == []


def test_archive_export_refuses_more_bytes_than_the_ceiling(
    client: TestClient, session: Session, current_user, monkeypatch
):
    monkeypatch.setattr(settings, "MEDIA_EXPORT_MAX_TOTAL_BYTES", 512)
    _make_object(session, current_user.id, filename="a.pdf")

    resp = client.post(EXPORT_URL, json={"format": "archive"})

    assert resp.status_code == 422
    assert session.exec(select(ExportJob)).all() == []


def test_archive_export_cannot_be_widened_by_a_foreign_category_id(
    client: TestClient, session: Session
):
    """The filter is refused before a job exists, not by a worker later."""
    theirs = Category(name="TheirDocs", slug="theirdocs", owner_id=uuid.uuid4())
    session.add(theirs)
    session.commit()
    session.refresh(theirs)

    resp = client.post(
        EXPORT_URL, json={"format": "archive", "filters": {"category_id": theirs.id}}
    )

    assert resp.status_code in (403, 404)
    assert session.exec(select(ExportJob)).all() == []


def test_an_export_that_cannot_be_queued_is_failed_not_left_queued(
    client: TestClient, session: Session, fake_arq_pool
):
    """A broker outage must not park a row that blocks every later export."""
    fake_arq_pool.enqueue_job.side_effect = RuntimeError("redis is down")

    resp = client.post(EXPORT_URL, json={"format": "archive"})

    assert resp.status_code == 503
    jobs = session.exec(select(ExportJob)).all()
    assert [job.status for job in jobs] == [ExportJobStatus.FAILED]
    assert jobs[0].error == "Export could not be queued."

    fake_arq_pool.enqueue_job.side_effect = None
    assert client.post(EXPORT_URL, json={"format": "archive"}).status_code == 202


def test_failing_an_unqueued_export_ignores_a_job_that_is_not_queued(
    session: Session, current_user
):
    """The enqueue-failure path must never overwrite a real outcome."""
    done = _completed_job(session, current_user.id)

    TransferController.fail_unqueued_export(session=session, job_id=done.id)

    session.refresh(done)
    assert done.status == ExportJobStatus.COMPLETED
    assert done.error is None


def test_failing_an_unqueued_export_tolerates_a_missing_job(session: Session):
    TransferController.fail_unqueued_export(session=session, job_id=uuid.uuid4())


# ── 2. assembling the archive ────────────────────────────────────────────────


def test_assembly_zips_the_manifest_and_every_object(session: Session, current_user):
    first = _make_object(session, current_user.id, filename="a.pdf")
    second = _make_object(session, current_user.id, filename="b.pdf")
    job = _queued_job(session, current_user.id)
    storage = _storage_for(first, second)

    embedded = ExportArchiveController.build(
        session=session, storage=storage, job_id=job.id
    )

    assert embedded == 2
    archive = storage.archive
    assert set(archive.namelist()) == {
        "manifest.json",
        f"files/{first.id}/a.pdf",
        f"files/{second.id}/b.pdf",
    }
    assert archive.read(f"files/{first.id}/a.pdf") == b"x" * 1024

    manifest = archive.read("manifest.json").decode("utf-8")
    assert '"category_tree"' in manifest
    assert "a.pdf" in manifest and "b.pdf" in manifest

    bucket, object_key, _payload, content_type = storage.uploads[0]
    assert bucket == "temp-media"
    assert object_key.endswith(f"exports/{job.id}.zip")
    assert content_type == "application/zip"

    session.refresh(job)
    assert job.status == ExportJobStatus.COMPLETED
    assert (job.storage_bucket, job.object_key) == (bucket, object_key)
    assert job.size_bytes == len(storage.uploads[0][2])
    assert job.expires_at is not None and _as_aware(job.expires_at) > utcnow()
    assert job.error is None


def test_assembly_manifests_unscannable_objects_without_their_bytes(
    session: Session, current_user
):
    """The download gate is not bypassable by asking for an archive."""
    clean = _make_object(session, current_user.id, filename="clean.pdf")
    infected = _make_object(
        session,
        current_user.id,
        filename="infected.pdf",
        scan_status=ScanStatus.INFECTED,
        status=MediaObjectStatus.REJECTED,
    )
    job = _queued_job(session, current_user.id)
    storage = _storage_for(clean, infected)

    embedded = ExportArchiveController.build(
        session=session, storage=storage, job_id=job.id
    )

    assert embedded == 1
    archive = storage.archive
    assert f"files/{clean.id}/clean.pdf" in archive.namelist()
    assert not [n for n in archive.namelist() if str(infected.id) in n]
    # It is still described, so an import can see *why* the bytes are absent.
    manifest = archive.read("manifest.json").decode("utf-8")
    assert str(infected.id) in manifest
    assert "infected" in manifest


def test_assembly_honours_the_recorded_filters(session: Session, current_user):
    keep = _make_object(session, current_user.id, filename="keep.pdf")
    _make_object(
        session,
        current_user.id,
        filename="skip.png",
        visibility=MediaVisibility.PUBLIC,
    )
    job = _queued_job(session, current_user.id, filters={"visibility": "private"})
    storage = _storage_for(keep)

    embedded = ExportArchiveController.build(
        session=session, storage=storage, job_id=job.id
    )

    assert embedded == 1
    assert f"files/{keep.id}/keep.pdf" in storage.archive.namelist()


def test_assembly_never_widens_past_the_recorded_tenant(session: Session):
    """The worker replays a scope; it does not get to choose a wider one."""
    tenant = uuid.uuid4()
    owner = uuid.uuid4()
    stranger = uuid.uuid4()
    mine = _make_object(session, owner, filename="mine.pdf", tenant_id=tenant)
    theirs = _make_object(session, stranger, filename="theirs.pdf")
    job = _queued_job(session, owner, tenant_id=tenant)
    storage = _storage_for(mine, theirs)

    embedded = ExportArchiveController.build(
        session=session, storage=storage, job_id=job.id
    )

    assert embedded == 1
    names = storage.archive.namelist()
    assert f"files/{mine.id}/mine.pdf" in names
    assert not [n for n in names if str(theirs.id) in n]


def test_assembly_of_an_empty_collection_still_writes_a_manifest(
    session: Session, current_user
):
    job = _queued_job(session, current_user.id)
    storage = _FakeStorage()

    embedded = ExportArchiveController.build(
        session=session, storage=storage, job_id=job.id
    )

    assert embedded == 0
    assert storage.archive.namelist() == ["manifest.json"]
    session.refresh(job)
    assert job.status == ExportJobStatus.COMPLETED


@pytest.mark.parametrize(
    "status", [ExportJobStatus.PROCESSING, ExportJobStatus.COMPLETED]
)
def test_assembly_declines_a_job_it_has_not_been_handed(
    session: Session, current_user, status
):
    """A redelivered job is skipped, never assembled (and charged for) twice."""
    _make_object(session, current_user.id)
    job = _queued_job(session, current_user.id, status=status)
    storage = _FakeStorage()

    assert (
        ExportArchiveController.build(session=session, storage=storage, job_id=job.id)
        == 0
    )
    assert storage.uploads == []
    session.refresh(job)
    assert job.status == status


def test_assembly_of_a_missing_job_is_a_no_op(session: Session):
    storage = _FakeStorage()
    assert (
        ExportArchiveController.build(
            session=session, storage=storage, job_id=uuid.uuid4()
        )
        == 0
    )
    assert storage.uploads == []


def test_a_storage_failure_mid_stream_fails_the_job_with_nothing_to_download(
    session: Session, current_user
):
    obj = _make_object(session, current_user.id, filename="a.pdf")
    job = _queued_job(session, current_user.id)
    storage = _storage_for(obj, fail_stream_for="a.pdf")

    embedded = ExportArchiveController.build(
        session=session, storage=storage, job_id=job.id
    )

    assert embedded == 0
    assert storage.uploads == []
    session.refresh(job)
    assert job.status == ExportJobStatus.FAILED
    # No location at all → the status route can never mint a presigned URL for
    # a truncated archive.
    assert job.storage_bucket is None and job.object_key is None
    assert job.size_bytes is None and job.expires_at is None
    assert job.error == "Archive assembly failed: OSError"
    # Whatever the failed attempt may have left behind is cleaned up.
    assert storage.removed and storage.removed[0][0] == "temp-media"


def test_a_failed_upload_fails_the_job(session: Session, current_user):
    obj = _make_object(session, current_user.id, filename="a.pdf")
    job = _queued_job(session, current_user.id)
    storage = _storage_for(obj, fail_put=True)

    assert (
        ExportArchiveController.build(session=session, storage=storage, job_id=job.id)
        == 0
    )
    session.refresh(job)
    assert job.status == ExportJobStatus.FAILED
    assert job.object_key is None


def test_cleanup_failures_do_not_mask_the_original_failure(
    session: Session, current_user, monkeypatch
):
    obj = _make_object(session, current_user.id, filename="a.pdf")
    job = _queued_job(session, current_user.id)
    storage = _storage_for(obj, fail_put=True)

    def _explode(**_kwargs):
        raise OSError("delete refused")

    monkeypatch.setattr(storage, "remove_object", _explode)

    assert (
        ExportArchiveController.build(session=session, storage=storage, job_id=job.id)
        == 0
    )
    session.refresh(job)
    assert job.status == ExportJobStatus.FAILED


def test_archive_entry_names_cannot_escape_the_archive(session: Session, current_user):
    """A caller-supplied filename is sanitised, or the zip is a path traversal."""
    obj = _make_object(session, current_user.id, filename="../../etc/passwd")
    assert archive_entry_name(obj) == f"files/{obj.id}/passwd"

    obj.original_filename = None
    assert archive_entry_name(obj) == f"files/{obj.id}/file"


def test_the_export_principal_is_only_a_recorded_scope(session: Session):
    tenant = uuid.uuid4()
    owner = uuid.uuid4()
    job = _queued_job(session, owner, tenant_id=tenant, is_superuser=True)

    principal = ExportPrincipal.from_job(job)

    assert (principal.id, principal.tenant_id, principal.is_superuser) == (
        owner,
        tenant,
        True,
    )
    assert principal.as_user is principal


# ── 3. collecting the export ─────────────────────────────────────────────────


def test_export_status_reports_a_queued_job_without_a_download(
    client: TestClient, session: Session
):
    started = client.post(EXPORT_URL, json={"format": "archive"})

    resp = client.get(f"{EXPORT_URL}/{started.json()['id']}")

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert resp.json()["download_url"] is None


def test_export_status_hands_back_a_presigned_download_when_ready(
    client: TestClient, session: Session, current_user, mock_storage
):
    mock_storage.presigned_get_object.return_value = "https://example.invalid/signed"
    job = _completed_job(session, uuid.UUID(str(current_user.id)))

    resp = client.get(f"{EXPORT_URL}/{job.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["size_bytes"] == 4096
    assert body["download_url"] == "https://example.invalid/signed"
    # The location is never disclosed — the caller gets the signed URL only.
    assert "object_key" not in body and "storage_bucket" not in body
    kwargs = mock_storage.presigned_get_object.call_args.kwargs
    assert kwargs["bucket"] == "temp-media"
    disposition = kwargs["response_headers"]["response-content-disposition"]
    assert f"export-{job.id}.zip" in disposition


def test_export_status_refuses_an_archive_that_has_lapsed(
    client: TestClient, session: Session, current_user, mock_storage
):
    job = _completed_job(
        session, uuid.UUID(str(current_user.id)), expires_in=timedelta(seconds=-1)
    )

    resp = client.get(f"{EXPORT_URL}/{job.id}")

    assert resp.status_code == 410
    mock_storage.presigned_get_object.assert_not_called()


def test_export_status_404s_an_unknown_job(client: TestClient):
    assert client.get(f"{EXPORT_URL}/{uuid.uuid4()}").status_code == 404


def test_export_status_403s_another_owners_job(client: TestClient, session: Session):
    job = _completed_job(session, uuid.uuid4())
    assert client.get(f"{EXPORT_URL}/{job.id}").status_code == 403


def test_a_superuser_may_read_another_owners_export_job(
    superuser_client: TestClient, session: Session, mock_storage
):
    mock_storage.presigned_get_object.return_value = "https://example.invalid/signed"
    job = _completed_job(session, uuid.uuid4())

    resp = superuser_client.get(f"{EXPORT_URL}/{job.id}")

    assert resp.status_code == 200
    assert resp.json()["download_url"] == "https://example.invalid/signed"


# ── 4. the worker job body ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_worker_job_assembles_through_the_controller(
    session: Session, current_user, monkeypatch
):
    """The arq entry point is a thin, thread-offloaded call into the controller."""
    monkeypatch.setattr(mw, "engine", _fake_engine(session))
    obj = _make_object(session, current_user.id, filename="a.pdf")
    job = _queued_job(session, current_user.id)
    storage = _storage_for(obj)

    embedded = await mw.build_export_archive({"storage": storage}, job.id)

    assert embedded == 1
    session.refresh(job)
    assert job.status == ExportJobStatus.COMPLETED
