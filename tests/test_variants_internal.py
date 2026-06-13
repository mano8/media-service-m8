"""Tests for internal variant routes: register variant, update job status."""

import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    ScanStatus,
)
from media_service.db_models.media_variants import MediaVariant
from media_service.db_models.variant_jobs import VariantJob, VariantJobStatus


def _make_object(session: Session) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=uuid.uuid4(),
        category="asset",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"k/{oid}",
        mime_type="image/png",
        size_bytes=10,
        status=MediaObjectStatus.UPLOADED,
        scan_status=ScanStatus.CLEAN,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _make_job(session: Session) -> VariantJob:
    job = VariantJob(
        media_object_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        status=VariantJobStatus.PROCESSING,
        requested_presets=["thumb"],
        variants_expected=2,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _register_body(name: str = "thumb_webp") -> dict:
    return {
        "variant_name": name,
        "storage_bucket": "private-media",
        "object_key": f"variants/{name}/x.webp",
        "width": 150,
        "height": 100,
        "size_bytes": 512,
        "format": "WEBP",
    }


def test_register_variant_creates_row(service_client: TestClient, session: Session):
    obj = _make_object(session)
    resp = service_client.post(
        f"/media/v1/internal/objects/{obj.id}/variants", json=_register_body()
    )
    assert resp.status_code == 200
    rows = session.exec(
        select(MediaVariant).where(MediaVariant.media_object_id == obj.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].variant_name == "thumb_webp"


def test_register_variant_is_idempotent(service_client: TestClient, session: Session):
    obj = _make_object(session)
    url = f"/media/v1/internal/objects/{obj.id}/variants"
    service_client.post(url, json=_register_body())
    body = _register_body()
    body["size_bytes"] = 999
    resp = service_client.post(url, json=body)
    assert resp.status_code == 200
    rows = session.exec(
        select(MediaVariant).where(MediaVariant.media_object_id == obj.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].size_bytes == 999


def test_register_variant_missing_object_returns_404(service_client: TestClient):
    resp = service_client.post(
        f"/media/v1/internal/objects/{uuid.uuid4()}/variants", json=_register_body()
    )
    assert resp.status_code == 404


def test_update_job_to_completed(service_client: TestClient, session: Session):
    job = _make_job(session)
    resp = service_client.patch(
        f"/media/v1/internal/variant-jobs/{job.id}",
        json={"status": "completed", "variants_created": 2},
    )
    assert resp.status_code == 200
    session.refresh(job)
    assert job.status == VariantJobStatus.COMPLETED
    assert job.variants_created == 2


def test_update_job_to_failed_records_error(
    service_client: TestClient, session: Session
):
    job = _make_job(session)
    resp = service_client.patch(
        f"/media/v1/internal/variant-jobs/{job.id}",
        json={"status": "failed", "error": "boom"},
    )
    assert resp.status_code == 200
    session.refresh(job)
    assert job.status == VariantJobStatus.FAILED
    assert job.error == "boom"
    assert job.variants_created == 0


def test_update_job_unknown_returns_404(service_client: TestClient):
    resp = service_client.patch(
        f"/media/v1/internal/variant-jobs/{uuid.uuid4()}",
        json={"status": "completed"},
    )
    assert resp.status_code == 404


def test_internal_variant_routes_require_token(
    service_client: TestClient, session: Session
):
    obj = _make_object(session)
    resp = service_client.post(
        f"/media/v1/internal/objects/{obj.id}/variants",
        json=_register_body(),
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 403
