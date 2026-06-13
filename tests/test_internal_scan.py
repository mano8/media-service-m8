"""Tests for the internal scan-result callback (service-token guarded)."""

import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    ScanStatus,
)


def _make_object(session: Session) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=uuid.uuid4(),
        category="document",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"k/{oid}",
        mime_type="application/pdf",
        size_bytes=10,
        status=MediaObjectStatus.UPLOADED,
        scan_status=ScanStatus.PENDING,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _url(object_id: uuid.UUID) -> str:
    return f"/media/v1/internal/objects/{object_id}/scan-result"


def test_clean_promotes_to_ready(service_client: TestClient, session: Session):
    obj = _make_object(session)
    resp = service_client.post(_url(obj.id), json={"scan_status": "clean"})
    assert resp.status_code == 200
    session.refresh(obj)
    assert obj.scan_status == ScanStatus.CLEAN
    assert obj.status == MediaObjectStatus.READY


def test_infected_quarantines(service_client: TestClient, session: Session):
    obj = _make_object(session)
    resp = service_client.post(_url(obj.id), json={"scan_status": "infected"})
    assert resp.status_code == 200
    session.refresh(obj)
    assert obj.scan_status == ScanStatus.QUARANTINED
    # An infected object stays UPLOADED (never promoted to READY).
    assert obj.status == MediaObjectStatus.UPLOADED


def test_clean_is_idempotent(service_client: TestClient, session: Session):
    obj = _make_object(session)
    first = service_client.post(_url(obj.id), json={"scan_status": "clean"})
    second = service_client.post(_url(obj.id), json={"scan_status": "clean"})
    assert first.status_code == second.status_code == 200
    session.refresh(obj)
    assert obj.status == MediaObjectStatus.READY


def test_missing_object_returns_404(service_client: TestClient):
    resp = service_client.post(_url(uuid.uuid4()), json={"scan_status": "clean"})
    assert resp.status_code == 404


def test_scan_result_without_token_is_forbidden(service_client: TestClient, session):
    obj = _make_object(session)
    resp = service_client.post(
        _url(obj.id),
        json={"scan_status": "clean"},
        headers={"Authorization": "Bearer nope"},
    )
    assert resp.status_code == 403
