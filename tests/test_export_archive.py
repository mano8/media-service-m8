"""Delegated archive export producer, callback, and collection (`P2 U11`)."""

import json
import uuid
from datetime import timedelta

from fastapi.testclient import TestClient
from media_sdk_m8 import ExportArchiveJobPayload
from sqlmodel import Session, select

from media_service.controllers.export_archive import archive_entry_name
from media_service.controllers.transfer import TransferController
from media_service.core.config import settings
from media_service.db_models.export_jobs import ExportJob, ExportJobStatus
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
    utcnow,
)

EXPORT_URL = "/media/v1/export"


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    filename: str = "file.pdf",
    size_bytes: int = 6,
    scan_status: ScanStatus = ScanStatus.CLEAN,
) -> MediaObject:
    object_id = uuid.uuid4()
    obj = MediaObject(
        id=object_id,
        owner_user_id=owner_id,
        category=MediaCategory.DOCUMENT,
        visibility=MediaVisibility.PRIVATE,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{object_id}/original/{filename}",
        original_filename=filename,
        mime_type="application/pdf",
        size_bytes=size_bytes,
        sha256="a" * 64,
        status=MediaObjectStatus.READY,
        scan_status=scan_status,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _job(
    session: Session,
    owner_id: uuid.UUID,
    *,
    status: ExportJobStatus = ExportJobStatus.QUEUED,
) -> ExportJob:
    job = ExportJob(owner_user_id=owner_id, status=status)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _completion(job: ExportJob, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "status": "completed",
        "storage_bucket": settings.MINIO_BUCKET_TEMP,
        "object_key": f"users/{job.owner_user_id}/exports/{job.id}.zip",
        "size_bytes": 512,
        "download_url": "https://media.example/export.zip?signature=hidden",
    }
    body.update(overrides)
    return body


def test_archive_export_enqueues_the_shared_payload(
    client: TestClient, session: Session, current_user, fake_arq_pool
):
    first = _make_object(session, current_user.id, filename="a.pdf", size_bytes=3)
    second = _make_object(session, current_user.id, filename="../b.pdf", size_bytes=4)

    response = client.post(EXPORT_URL, json={"format": "archive"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    job = session.get(ExportJob, uuid.UUID(body["id"]))
    assert job is not None
    args, kwargs = fake_arq_pool.enqueue_job.await_args
    assert args[0] == "build_export_archive"
    payload = args[1]
    assert isinstance(payload, ExportArchiveJobPayload)
    assert payload.job_id == job.id
    assert kwargs == {"_job_id": str(job.id)}
    assert payload.target_bucket == settings.MINIO_BUCKET_TEMP
    assert payload.target_object_key.endswith(f"/exports/{job.id}.zip")
    assert payload.stream_chunk_size == settings.MEDIA_EXPORT_STREAM_CHUNK_SIZE
    assert [entry.object_id for entry in payload.objects] == sorted(
        [first.id, second.id]
    )
    assert {entry.archive_path for entry in payload.objects} == {
        f"files/{first.id}/a.pdf",
        f"files/{second.id}/b.pdf",
    }
    manifest = json.loads(payload.manifest_json)
    assert {row["id"] for row in manifest["objects"]} == {
        str(first.id),
        str(second.id),
    }


def test_archive_payload_manifests_unsafe_objects_without_their_bytes(
    client: TestClient, session: Session, current_user, fake_arq_pool
):
    clean = _make_object(session, current_user.id, filename="clean.pdf")
    infected = _make_object(
        session,
        current_user.id,
        filename="infected.pdf",
        scan_status=ScanStatus.INFECTED,
    )

    assert client.post(EXPORT_URL, json={"format": "archive"}).status_code == 202

    payload = fake_arq_pool.enqueue_job.await_args.args[1]
    assert [entry.object_id for entry in payload.objects] == [clean.id]
    manifest_ids = {row["id"] for row in json.loads(payload.manifest_json)["objects"]}
    assert manifest_ids == {str(clean.id), str(infected.id)}


def test_archive_export_records_the_authorized_scope(
    client: TestClient, session: Session, current_user
):
    response = client.post(
        EXPORT_URL, json={"format": "archive", "filters": {"uncategorized": True}}
    )

    job = session.get(ExportJob, uuid.UUID(response.json()["id"]))
    assert job is not None
    assert job.owner_user_id == uuid.UUID(str(current_user.id))
    assert job.is_superuser is False
    assert job.filters["uncategorized"] is True


def test_archive_export_refuses_a_second_in_flight_job(
    client: TestClient, session: Session, current_user
):
    _job(session, uuid.UUID(str(current_user.id)), status=ExportJobStatus.PROCESSING)

    response = client.post(EXPORT_URL, json={"format": "archive"})

    assert response.status_code == 409


def test_archive_export_refuses_more_objects_than_the_ceiling(
    client: TestClient, session: Session, current_user, monkeypatch
):
    _make_object(session, current_user.id)
    monkeypatch.setattr(settings, "MEDIA_EXPORT_MAX_OBJECTS", 0)

    response = client.post(EXPORT_URL, json={"format": "archive"})

    assert response.status_code == 422
    assert session.exec(select(ExportJob)).all() == []


def test_archive_export_refuses_more_bytes_than_the_ceiling(
    client: TestClient, session: Session, current_user, monkeypatch
):
    _make_object(session, current_user.id, size_bytes=2)
    monkeypatch.setattr(settings, "MEDIA_EXPORT_MAX_TOTAL_BYTES", 1)

    response = client.post(EXPORT_URL, json={"format": "archive"})

    assert response.status_code == 422
    assert session.exec(select(ExportJob)).all() == []


def test_archive_export_ignores_a_foreign_owner_filter(
    client: TestClient, session: Session, current_user, fake_arq_pool
):
    mine = _make_object(session, current_user.id, filename="mine.pdf")
    _make_object(session, uuid.uuid4(), filename="theirs.pdf")

    response = client.post(
        EXPORT_URL,
        json={"format": "archive", "filters": {"owner_user_id": str(uuid.uuid4())}},
    )

    assert response.status_code == 202
    payload = fake_arq_pool.enqueue_job.await_args.args[1]
    assert [entry.object_id for entry in payload.objects] == [mine.id]


def test_an_export_that_cannot_be_queued_is_failed(
    client: TestClient, session: Session, fake_arq_pool
):
    fake_arq_pool.enqueue_job.side_effect = RuntimeError("redis is down")

    response = client.post(EXPORT_URL, json={"format": "archive"})

    assert response.status_code == 503
    jobs = session.exec(select(ExportJob)).all()
    assert [job.status for job in jobs] == [ExportJobStatus.FAILED]


def test_failing_an_unqueued_export_ignores_missing_or_terminal_jobs(
    session: Session,
):
    TransferController.fail_unqueued_export(session=session, job_id=uuid.uuid4())
    completed = _job(session, uuid.uuid4(), status=ExportJobStatus.COMPLETED)

    TransferController.fail_unqueued_export(session=session, job_id=completed.id)

    session.refresh(completed)
    assert completed.status == ExportJobStatus.COMPLETED


def test_archive_entry_names_are_traversal_safe(session: Session, current_user):
    obj = _make_object(session, current_user.id, filename="../../passwd")
    assert archive_entry_name(obj) == f"files/{obj.id}/passwd"
    obj.original_filename = ".."
    assert archive_entry_name(obj) == f"files/{obj.id}/file"


def test_worker_callback_advances_queued_to_processing(
    service_client: TestClient, session: Session
):
    job = _job(session, uuid.uuid4())

    first = service_client.patch(
        f"/media/v1/internal/export-jobs/{job.id}", json={"status": "processing"}
    )
    second = service_client.patch(
        f"/media/v1/internal/export-jobs/{job.id}", json={"status": "processing"}
    )

    assert first.status_code == second.status_code == 200
    session.refresh(job)
    assert job.status == ExportJobStatus.PROCESSING


def test_worker_callback_completes_and_is_idempotent(
    service_client: TestClient, session: Session
):
    job = _job(session, uuid.uuid4(), status=ExportJobStatus.PROCESSING)
    body = _completion(job)

    first = service_client.patch(f"/media/v1/internal/export-jobs/{job.id}", json=body)
    second = service_client.patch(f"/media/v1/internal/export-jobs/{job.id}", json=body)

    assert first.status_code == second.status_code == 200
    session.refresh(job)
    assert job.status == ExportJobStatus.COMPLETED
    assert job.storage_bucket == settings.MINIO_BUCKET_TEMP
    assert job.object_key == body["object_key"]
    assert job.size_bytes == 512
    assert job.expires_at is not None


def test_worker_callback_refuses_a_poisoned_result_location(
    service_client: TestClient, session: Session
):
    job = _job(session, uuid.uuid4(), status=ExportJobStatus.PROCESSING)

    response = service_client.patch(
        f"/media/v1/internal/export-jobs/{job.id}",
        json=_completion(job, object_key="someone-elses/archive.zip"),
    )

    assert response.status_code == 422
    session.refresh(job)
    assert job.status == ExportJobStatus.PROCESSING
    assert job.object_key is None


def test_worker_callback_records_a_generic_failure(
    service_client: TestClient, session: Session
):
    job = _job(session, uuid.uuid4(), status=ExportJobStatus.PROCESSING)
    body = {"status": "failed", "error": "Archive assembly failed: OSError"}

    first = service_client.patch(f"/media/v1/internal/export-jobs/{job.id}", json=body)
    second = service_client.patch(f"/media/v1/internal/export-jobs/{job.id}", json=body)

    assert first.status_code == second.status_code == 200
    session.refresh(job)
    assert job.status == ExportJobStatus.FAILED
    assert job.error == body["error"]
    assert job.storage_bucket is None


def test_worker_callback_rejects_invalid_terminal_transitions(
    service_client: TestClient, session: Session
):
    completed = _job(session, uuid.uuid4(), status=ExportJobStatus.COMPLETED)
    failed = _job(session, uuid.uuid4(), status=ExportJobStatus.FAILED)

    assert (
        service_client.patch(
            f"/media/v1/internal/export-jobs/{completed.id}",
            json={"status": "processing"},
        ).status_code
        == 409
    )
    assert (
        service_client.patch(
            f"/media/v1/internal/export-jobs/{completed.id}",
            json={"status": "failed", "error": "failed"},
        ).status_code
        == 409
    )
    assert (
        service_client.patch(
            f"/media/v1/internal/export-jobs/{failed.id}", json=_completion(failed)
        ).status_code
        == 409
    )


def test_worker_callback_validates_body_and_unknown_job(
    service_client: TestClient, session: Session
):
    job = _job(session, uuid.uuid4())

    assert (
        service_client.patch(
            f"/media/v1/internal/export-jobs/{job.id}", json={"status": "completed"}
        ).status_code
        == 422
    )
    assert (
        service_client.patch(
            f"/media/v1/internal/export-jobs/{job.id}", json={"status": "failed"}
        ).status_code
        == 422
    )
    assert (
        service_client.patch(
            f"/media/v1/internal/export-jobs/{uuid.uuid4()}",
            json={"status": "processing"},
        ).status_code
        == 404
    )


def test_internal_export_callback_requires_the_service_token(
    service_client: TestClient, session: Session
):
    job = _job(session, uuid.uuid4())
    response = service_client.patch(
        f"/media/v1/internal/export-jobs/{job.id}",
        json={"status": "processing"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 403


def test_export_status_reports_queued_without_a_download(
    client: TestClient, session: Session, current_user
):
    job = _job(session, uuid.UUID(str(current_user.id)))

    response = client.get(f"{EXPORT_URL}/{job.id}")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["download_url"] is None


def test_export_status_presigns_a_completed_archive(
    client: TestClient, session: Session, current_user, mock_storage
):
    job = _job(
        session, uuid.UUID(str(current_user.id)), status=ExportJobStatus.COMPLETED
    )
    job.storage_bucket = settings.MINIO_BUCKET_TEMP
    job.object_key = f"users/{job.owner_user_id}/exports/{job.id}.zip"
    job.size_bytes = 512
    job.expires_at = utcnow() + timedelta(minutes=5)
    session.add(job)
    session.commit()
    mock_storage.presigned_get_object.return_value = "https://media.example/download"

    response = client.get(f"{EXPORT_URL}/{job.id}")

    assert response.status_code == 200
    assert response.json()["download_url"] == "https://media.example/download"
    mock_storage.presigned_get_object.assert_called_once()


def test_export_status_refuses_an_expired_archive(
    client: TestClient, session: Session, current_user
):
    job = _job(
        session, uuid.UUID(str(current_user.id)), status=ExportJobStatus.COMPLETED
    )
    job.storage_bucket = settings.MINIO_BUCKET_TEMP
    job.object_key = f"users/{job.owner_user_id}/exports/{job.id}.zip"
    job.expires_at = utcnow() - timedelta(seconds=1)
    session.add(job)
    session.commit()

    assert client.get(f"{EXPORT_URL}/{job.id}").status_code == 410


def test_export_status_enforces_existence_and_ownership(
    client: TestClient, session: Session
):
    assert client.get(f"{EXPORT_URL}/{uuid.uuid4()}").status_code == 404
    theirs = _job(session, uuid.uuid4())
    assert client.get(f"{EXPORT_URL}/{theirs.id}").status_code == 403


def test_superuser_may_collect_another_owners_export(
    superuser_client: TestClient, session: Session
):
    theirs = _job(session, uuid.uuid4())
    assert superuser_client.get(f"{EXPORT_URL}/{theirs.id}").status_code == 200
