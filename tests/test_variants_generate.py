"""Tests for POST /v1/objects/{id}/variants:generate (producer side)."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from media_service.core.arq import VARIANTS_TASK
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    ScanStatus,
    utcnow,
)
from media_service.db_models.variant_jobs import VariantJob, VariantJobStatus


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    mime: str = "image/png",
    status: MediaObjectStatus = MediaObjectStatus.READY,
    scan_status: ScanStatus = ScanStatus.CLEAN,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category="asset",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/asset/{oid}/original/pic",
        original_filename="pic.png",
        mime_type=mime,
        size_bytes=2048,
        status=status,
        scan_status=scan_status,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _gen(client: TestClient, object_id: uuid.UUID, presets):
    return client.post(
        f"/media/v1/objects/{object_id}/variants:generate",
        json={"presets": presets},
    )


def test_generate_creates_job_and_enqueues(
    client: TestClient, session: Session, current_user, fake_arq_pool
):
    obj = _make_object(session, uuid.UUID(str(current_user.id)))
    resp = _gen(client, obj.id, ["thumb", "large"])
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == VariantJobStatus.QUEUED
    assert body["variants_expected"] == 2
    assert body["requested_presets"] == ["thumb", "large"]

    job = session.get(VariantJob, uuid.UUID(body["id"]))
    assert job is not None
    assert job.media_object_id == obj.id

    fake_arq_pool.enqueue_job.assert_awaited_once()
    args, kwargs = fake_arq_pool.enqueue_job.await_args
    assert args[0] == VARIANTS_TASK
    assert kwargs["_job_id"] == body["id"]
    assert len(args[1].specs) == 2


@pytest.mark.parametrize(
    ("obj_status", "scan_status"),
    [
        (MediaObjectStatus.UPLOADED, ScanStatus.PENDING),
        (MediaObjectStatus.UPLOADED, ScanStatus.CLEAN),
        (MediaObjectStatus.READY, ScanStatus.PENDING),
        (MediaObjectStatus.READY, ScanStatus.QUARANTINED),
        (MediaObjectStatus.READY, ScanStatus.INFECTED),
        (MediaObjectStatus.PROCESSING, ScanStatus.CLEAN),
        (MediaObjectStatus.FAILED, ScanStatus.CLEAN),
        (MediaObjectStatus.REJECTED, ScanStatus.QUARANTINED),
    ],
)
def test_generate_rejects_unready_or_unscanned_object(
    client: TestClient,
    session: Session,
    current_user,
    fake_arq_pool,
    obj_status: MediaObjectStatus,
    scan_status: ScanStatus,
):
    obj = _make_object(
        session,
        uuid.UUID(str(current_user.id)),
        status=obj_status,
        scan_status=scan_status,
    )
    resp = _gen(client, obj.id, ["thumb"])
    assert resp.status_code == 409
    assert session.exec(select(VariantJob)).all() == []
    fake_arq_pool.enqueue_job.assert_not_awaited()


def test_generate_rejects_deleted_object(
    client: TestClient, session: Session, current_user, fake_arq_pool
):
    obj = _make_object(session, uuid.UUID(str(current_user.id)))
    obj.deleted_at = utcnow()
    obj.status = MediaObjectStatus.DELETED
    session.add(obj)
    session.commit()
    resp = _gen(client, obj.id, ["thumb"])
    assert resp.status_code == 404
    assert session.exec(select(VariantJob)).all() == []
    fake_arq_pool.enqueue_job.assert_not_awaited()


def test_generate_rejects_non_image(client: TestClient, session: Session, current_user):
    obj = _make_object(session, uuid.UUID(str(current_user.id)), mime="application/pdf")
    resp = _gen(client, obj.id, ["thumb"])
    assert resp.status_code == 422


def test_generate_unknown_preset_returns_422(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, uuid.UUID(str(current_user.id)))
    resp = _gen(client, obj.id, ["does-not-exist"])
    assert resp.status_code == 422


def test_generate_missing_object_returns_404(client: TestClient):
    resp = _gen(client, uuid.uuid4(), ["thumb"])
    assert resp.status_code == 404


def test_generate_forbidden_for_non_owner(
    client: TestClient, session: Session, superuser
):
    obj = _make_object(session, uuid.UUID(str(superuser.id)))
    resp = _gen(client, obj.id, ["thumb"])
    assert resp.status_code == 403


def test_generate_no_job_created_on_validation_failure(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, uuid.UUID(str(current_user.id)), mime="application/pdf")
    _gen(client, obj.id, ["thumb"])
    rows = session.exec(select(VariantJob)).all()
    assert rows == []
