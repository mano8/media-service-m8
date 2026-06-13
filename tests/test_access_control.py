"""Tests for Phase 15a visibility & tenant enforcement on read/download/list.

Covers the per-object ``require_visibility_access`` rule and the matching
listing scope in ``controllers/objects.py``: ``PUBLIC`` is readable by any
authenticated user, ``TENANT`` only within the same (non-null) tenant, and
``PRIVATE``/``SENSITIVE`` only by the owner or a superuser.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.controllers.objects import (
    ObjectsController,
    require_visibility_access,
)
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
)
from media_service.schemas.objects import ObjectListParams


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    visibility: MediaVisibility = MediaVisibility.PRIVATE,
    tenant_id: uuid.UUID | None = None,
    status: MediaObjectStatus = MediaObjectStatus.UPLOADED,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        tenant_id=tenant_id,
        category="document",
        visibility=visibility,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{oid}/original/file.pdf",
        original_filename="file.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        status=status,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _stub_user(
    *, is_superuser: bool = False, tenant_id: uuid.UUID | None = None
) -> SimpleNamespace:
    """A minimal user duck-typed for the access helpers (id/is_superuser/tenant_id).

    ``UserModel`` does not carry a tenant claim, so a stub is the only way to
    exercise the tenant-match branches until tenancy is wired through auth.
    """
    return SimpleNamespace(
        id=uuid.uuid4(), is_superuser=is_superuser, tenant_id=tenant_id
    )


# ── require_visibility_access (unit) ──────────────────────────────────────────


def test_visibility_owner_always_allowed(session: Session):
    owner = _stub_user()
    obj = _make_object(session, owner.id, visibility=MediaVisibility.SENSITIVE)
    # Owner of a SENSITIVE object is allowed (no exception).
    require_visibility_access(obj, owner)


def test_visibility_superuser_always_allowed(session: Session):
    obj = _make_object(session, uuid.uuid4(), visibility=MediaVisibility.PRIVATE)
    require_visibility_access(obj, _stub_user(is_superuser=True))


def test_visibility_public_allows_any_user(session: Session):
    obj = _make_object(session, uuid.uuid4(), visibility=MediaVisibility.PUBLIC)
    require_visibility_access(obj, _stub_user())


@pytest.mark.parametrize(
    "visibility", [MediaVisibility.PRIVATE, MediaVisibility.SENSITIVE]
)
def test_visibility_private_and_sensitive_denied_for_others(
    session: Session, visibility: MediaVisibility
):
    obj = _make_object(session, uuid.uuid4(), visibility=visibility)
    with pytest.raises(HTTPException) as exc:
        require_visibility_access(obj, _stub_user())
    assert exc.value.status_code == 403


def test_visibility_tenant_allows_same_tenant(session: Session):
    tenant = uuid.uuid4()
    obj = _make_object(
        session, uuid.uuid4(), visibility=MediaVisibility.TENANT, tenant_id=tenant
    )
    require_visibility_access(obj, _stub_user(tenant_id=tenant))


def test_visibility_tenant_denied_for_different_tenant(session: Session):
    obj = _make_object(
        session,
        uuid.uuid4(),
        visibility=MediaVisibility.TENANT,
        tenant_id=uuid.uuid4(),
    )
    with pytest.raises(HTTPException) as exc:
        require_visibility_access(obj, _stub_user(tenant_id=uuid.uuid4()))
    assert exc.value.status_code == 403


def test_visibility_tenant_denied_for_untenanted_user(session: Session):
    # A null user tenant must never match a TENANT object (no None == None).
    obj = _make_object(
        session,
        uuid.uuid4(),
        visibility=MediaVisibility.TENANT,
        tenant_id=uuid.uuid4(),
    )
    with pytest.raises(HTTPException) as exc:
        require_visibility_access(obj, _stub_user(tenant_id=None))
    assert exc.value.status_code == 403


# ── GET / download-url enforcement (HTTP) ─────────────────────────────────────


def test_get_public_object_of_other_owner_allowed(client: TestClient, session: Session):
    obj = _make_object(session, uuid.uuid4(), visibility=MediaVisibility.PUBLIC)
    resp = client.get(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(obj.id)


def test_download_public_object_of_other_owner_allowed(
    client: TestClient, session: Session, mock_storage
):
    obj = _make_object(session, uuid.uuid4(), visibility=MediaVisibility.PUBLIC)
    mock_storage.presigned_get_object.return_value = "https://minio/download"
    resp = client.get(f"/media/v1/objects/{obj.id}/download-url")
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://minio/download"


def test_get_sensitive_object_of_other_owner_denied(
    client: TestClient, session: Session
):
    obj = _make_object(session, uuid.uuid4(), visibility=MediaVisibility.SENSITIVE)
    resp = client.get(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 403


def test_get_tenant_object_denied_for_untenanted_caller(
    client: TestClient, session: Session
):
    obj = _make_object(
        session,
        uuid.uuid4(),
        visibility=MediaVisibility.TENANT,
        tenant_id=uuid.uuid4(),
    )
    resp = client.get(f"/media/v1/objects/{obj.id}")
    assert resp.status_code == 403


# ── list scoping (HTTP + controller) ──────────────────────────────────────────


def test_list_includes_other_owner_public(
    client: TestClient, session: Session, current_user
):
    own = _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    public = _make_object(session, uuid.uuid4(), visibility=MediaVisibility.PUBLIC)
    ids = {item["id"] for item in client.get("/media/v1/objects").json()["items"]}
    assert {str(own.id), str(public.id)} <= ids


@pytest.mark.parametrize(
    "visibility", [MediaVisibility.PRIVATE, MediaVisibility.SENSITIVE]
)
def test_list_excludes_other_owner_restricted(
    client: TestClient, session: Session, visibility: MediaVisibility
):
    other = _make_object(session, uuid.uuid4(), visibility=visibility)
    ids = {item["id"] for item in client.get("/media/v1/objects").json()["items"]}
    assert str(other.id) not in ids


def test_list_tenant_scoping_for_tenanted_user(session: Session):
    # Drives _scoped_query's tenant branch directly: a tenanted caller sees a
    # same-tenant TENANT object but not one belonging to a different tenant.
    tenant = uuid.uuid4()
    caller = _stub_user(tenant_id=tenant)
    same = _make_object(
        session, uuid.uuid4(), visibility=MediaVisibility.TENANT, tenant_id=tenant
    )
    other = _make_object(
        session,
        uuid.uuid4(),
        visibility=MediaVisibility.TENANT,
        tenant_id=uuid.uuid4(),
    )
    result = ObjectsController.list_objects(
        session=session, current_user=caller, params=ObjectListParams()
    )
    ids = {item.id for item in result.items}
    assert same.id in ids
    assert other.id not in ids
