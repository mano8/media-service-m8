"""Tests for variant query routes: list, get job, delete."""

import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
)
from media_service.db_models.media_variants import MediaVariant
from media_service.db_models.variant_jobs import VariantJob, VariantJobStatus


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    visibility: MediaVisibility = MediaVisibility.PRIVATE,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category="asset",
        visibility=visibility,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/asset/{oid}/original/pic.png",
        mime_type="image/png",
        size_bytes=2048,
        status=MediaObjectStatus.UPLOADED,
        scan_status=ScanStatus.CLEAN,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _make_variant(
    session: Session, object_id: uuid.UUID, name: str = "thumb_webp"
) -> MediaVariant:
    variant = MediaVariant(
        media_object_id=object_id,
        variant_name=name,
        storage_bucket="private-media",
        object_key=f"variants/{name}/x.webp",
        size_bytes=512,
        format="WEBP",
    )
    session.add(variant)
    session.commit()
    session.refresh(variant)
    return variant


def _make_job(
    session: Session, object_id: uuid.UUID, owner_id: uuid.UUID
) -> VariantJob:
    job = VariantJob(
        media_object_id=object_id,
        owner_user_id=owner_id,
        status=VariantJobStatus.PROCESSING,
        requested_presets=["thumb"],
        variants_expected=1,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def test_list_variants_returns_rows(client: TestClient, session: Session, current_user):
    obj = _make_object(session, uuid.UUID(str(current_user.id)))
    _make_variant(session, obj.id, "thumb_webp")
    _make_variant(session, obj.id, "large_webp")
    resp = client.get(f"/media/v1/objects/{obj.id}/variants")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert {v["variant_name"] for v in body["items"]} == {"thumb_webp", "large_webp"}


def test_list_variants_allowed_for_public_object_of_other_owner(
    client: TestClient, session: Session
):
    obj = _make_object(session, uuid.uuid4(), visibility=MediaVisibility.PUBLIC)
    _make_variant(session, obj.id)
    resp = client.get(f"/media/v1/objects/{obj.id}/variants")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_get_job_returns_progress(client: TestClient, session: Session, current_user):
    owner = uuid.UUID(str(current_user.id))
    obj = _make_object(session, owner)
    job = _make_job(session, obj.id, owner)
    resp = client.get(f"/media/v1/objects/{obj.id}/variants/jobs/{job.id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == VariantJobStatus.PROCESSING


def test_get_job_unknown_returns_404(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, uuid.UUID(str(current_user.id)))
    resp = client.get(f"/media/v1/objects/{obj.id}/variants/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_job_mismatched_object_returns_404(
    client: TestClient, session: Session, current_user
):
    owner = uuid.UUID(str(current_user.id))
    obj_a = _make_object(session, owner)
    obj_b = _make_object(session, owner)
    job = _make_job(session, obj_b.id, owner)
    resp = client.get(f"/media/v1/objects/{obj_a.id}/variants/jobs/{job.id}")
    assert resp.status_code == 404


def test_delete_variant_removes_row_and_bytes(
    client: TestClient, mock_storage, session: Session, current_user
):
    obj = _make_object(session, uuid.UUID(str(current_user.id)))
    variant = _make_variant(session, obj.id)
    resp = client.delete(f"/media/v1/objects/{obj.id}/variants/{variant.id}")
    assert resp.status_code == 204
    assert session.get(MediaVariant, variant.id) is None
    mock_storage.remove_object.assert_called_once_with(
        bucket="private-media", object_key=variant.object_key
    )


def test_delete_variant_unknown_returns_404(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, uuid.UUID(str(current_user.id)))
    resp = client.delete(f"/media/v1/objects/{obj.id}/variants/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_variant_mismatched_object_returns_404(
    client: TestClient, session: Session, current_user
):
    owner = uuid.UUID(str(current_user.id))
    obj_a = _make_object(session, owner)
    obj_b = _make_object(session, owner)
    variant = _make_variant(session, obj_b.id)
    resp = client.delete(f"/media/v1/objects/{obj_a.id}/variants/{variant.id}")
    assert resp.status_code == 404
