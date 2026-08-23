"""``POST /media/v1/export`` — manifest export (`U9`).

The ``manifest`` half of the export surface: answered inline, metadata only, no
bytes. The ``archive`` half — the job the request creates, the zip the worker
assembles, and the status route that hands back the download — lives in
``tests/test_export_archive.py``, so each file follows one format end to end.

The manifest is scoped through the exact same helpers the objects list uses
(`_scoped_query`/`_apply_filters`/`_apply_category_filter`), so the tenant/
foreign-filter isolation is proved once, directly against the controller, with
a tenanted ``SimpleNamespace`` principal — matching
``tests/test_category_assignment.py``'s pattern, since ``UserModel`` carries no
tenant claim (`D1`/`D4`).
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.controllers.transfer import (
    TransferController,
    _category_tree_json,
    _manifest_objects,
    stream_manifest,
)
from media_service.controllers.category import category_refs_by_object
from media_service.db_models.categories import Category
from media_service.db_models.media_object_categories import MediaObjectCategoryLink
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
)
from media_service.schemas.objects import ObjectListParams

EXPORT_URL = "/media/v1/export"


def _tenanted_user(tenant_id: uuid.UUID, user_id: uuid.UUID | None = None):
    """Principal carrying a tenant claim (`D1`/`D4`: ``UserModel`` has none)."""
    return SimpleNamespace(
        id=user_id or uuid.uuid4(), is_superuser=False, tenant_id=tenant_id
    )


def _make_category(
    session: Session,
    owner_id: uuid.UUID,
    name: str,
    *,
    parent_id: int | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Category:
    cat = Category(
        name=name,
        slug=name.lower().replace(" ", "-"),
        owner_id=owner_id,
        parent_id=parent_id,
        tenant_id=tenant_id,
    )
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    filename: str = "file.pdf",
    visibility: MediaVisibility = MediaVisibility.PRIVATE,
    deleted: bool = False,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category=MediaCategory.DOCUMENT,
        visibility=visibility,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{oid}/original/{filename}",
        original_filename=filename,
        mime_type="application/pdf",
        size_bytes=1024,
        sha256="a" * 64,
        status=MediaObjectStatus.READY,
        scan_status=ScanStatus.CLEAN,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    if deleted:
        from media_service.db_models.media_objects import utcnow

        obj.deleted_at = utcnow()
        obj.status = MediaObjectStatus.DELETED
        session.add(obj)
        session.commit()
        session.refresh(obj)
    return obj


def _file(session: Session, obj: MediaObject, category: Category) -> None:
    session.add(
        MediaObjectCategoryLink(media_object_id=obj.id, category_id=category.id)
    )
    session.commit()


# ── HTTP surface ──────────────────────────────────────────────────────────────


def test_manifest_export_streams_the_tree_and_every_object(
    client: TestClient, session: Session, current_user
):
    root = _make_category(session, current_user.id, "Docs")
    child = _make_category(session, current_user.id, "Invoices", parent_id=root.id)
    filed = _make_object(session, current_user.id, filename="invoice.pdf")
    unfiled = _make_object(session, current_user.id, filename="loose.pdf")
    _file(session, filed, child)

    resp = client.post(EXPORT_URL, json={"format": "manifest"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()

    tree = body["category_tree"]
    assert [n["name"] for n in tree] == ["Docs"]
    assert tree[0]["children"][0]["name"] == "Invoices"
    assert tree[0]["object_count"] == 0
    assert tree[0]["total_object_count"] == 1
    assert tree[0]["children"][0]["object_count"] == 1

    by_id = {o["id"]: o for o in body["objects"]}
    assert {by_id[str(filed.id)]["filename"], by_id[str(unfiled.id)]["filename"]} == {
        "invoice.pdf",
        "loose.pdf",
    }
    filed_entry = by_id[str(filed.id)]
    assert filed_entry["category_paths"] == ["docs/invoices"]
    assert filed_entry["category"] == "document"
    assert filed_entry["visibility"] == "private"
    assert filed_entry["size_bytes"] == 1024
    assert filed_entry["sha256"] == "a" * 64
    assert filed_entry["mime_type"] == "application/pdf"
    assert filed_entry["status"] == "ready"
    assert filed_entry["scan_status"] == "clean"
    assert "created_at" in filed_entry and "updated_at" in filed_entry

    unfiled_entry = by_id[str(unfiled.id)]
    assert unfiled_entry["category_paths"] == []

    # No bytes, no storage location, ever.
    for entry in body["objects"]:
        assert "storage_bucket" not in entry
        assert "object_key" not in entry
        assert "owner_user_id" not in entry


def test_manifest_export_with_no_categories_or_objects_is_empty(
    client: TestClient,
):
    resp = client.post(EXPORT_URL, json={"format": "manifest"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"category_tree": [], "objects": []}


def test_manifest_export_filters_by_category_branch(
    client: TestClient, session: Session, current_user
):
    root = _make_category(session, current_user.id, "Docs")
    filed = _make_object(session, current_user.id, filename="in-branch.pdf")
    unfiled = _make_object(session, current_user.id, filename="outside.pdf")
    _file(session, filed, root)

    resp = client.post(
        EXPORT_URL,
        json={"format": "manifest", "filters": {"category_id": root.id}},
    )
    assert resp.status_code == 200
    filenames = {o["filename"] for o in resp.json()["objects"]}
    assert filenames == {"in-branch.pdf"}
    assert unfiled.original_filename not in filenames


def test_manifest_export_uncategorized_filter(
    client: TestClient, session: Session, current_user
):
    root = _make_category(session, current_user.id, "Docs")
    filed = _make_object(session, current_user.id, filename="filed.pdf")
    _make_object(session, current_user.id, filename="unfiled.pdf")
    _file(session, filed, root)

    resp = client.post(
        EXPORT_URL,
        json={"format": "manifest", "filters": {"uncategorized": True}},
    )
    assert resp.status_code == 200
    filenames = {o["filename"] for o in resp.json()["objects"]}
    assert filenames == {"unfiled.pdf"}
    assert filed.original_filename not in filenames


def test_manifest_export_excludes_soft_deleted_objects(
    client: TestClient, session: Session, current_user
):
    _make_object(session, current_user.id, filename="gone.pdf", deleted=True)
    live = _make_object(session, current_user.id, filename="live.pdf")

    resp = client.post(EXPORT_URL, json={"format": "manifest"})
    filenames = {o["filename"] for o in resp.json()["objects"]}
    assert filenames == {"live.pdf"}
    assert live.original_filename in filenames


def test_manifest_export_cannot_be_widened_by_a_foreign_category_id(
    client: TestClient, session: Session
):
    """A caller cannot export another owner's media via a foreign ``category_id``."""
    stranger_id = uuid.uuid4()
    theirs = _make_category(session, stranger_id, "TheirDocs")
    resp = client.post(
        EXPORT_URL,
        json={"format": "manifest", "filters": {"category_id": theirs.id}},
    )
    assert resp.status_code in (403, 404)


def test_export_rejects_an_unknown_format(client: TestClient):
    resp = client.post(EXPORT_URL, json={"format": "csv"})
    assert resp.status_code == 422


# ── controller-level: tenant isolation and pure assembly ────────────────────


def test_stream_manifest_is_scoped_to_the_callers_tenant(session: Session):
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    owner_a = _tenanted_user(tenant_a)
    owner_b = _tenanted_user(tenant_b)
    cat_a = _make_category(
        session, uuid.UUID(str(owner_a.id)), "TenantA", tenant_id=tenant_a
    )
    obj_a = _make_object(session, uuid.UUID(str(owner_a.id)), filename="a.pdf")
    _file(session, obj_a, cat_a)
    _make_object(session, uuid.UUID(str(owner_b.id)), filename="b.pdf")

    objects = _manifest_objects(session, owner_a, ObjectListParams())
    refs = category_refs_by_object(session, owner_a, [o.id for o in objects])
    tree_json = _category_tree_json(session, owner_a)
    chunks = "".join(stream_manifest(tree_json, objects, refs))
    manifest = json.loads(chunks)
    assert [o["filename"] for o in manifest["objects"]] == ["a.pdf"]
    assert [n["name"] for n in manifest["category_tree"]] == ["TenantA"]


def test_export_of_a_foreign_category_id_is_refused_not_silently_empty(
    session: Session,
):
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    owner_a = _tenanted_user(tenant_a)
    owner_b = _tenanted_user(tenant_b)
    theirs = _make_category(
        session, uuid.UUID(str(owner_b.id)), "NotYours", tenant_id=tenant_b
    )
    with pytest.raises(HTTPException) as exc:
        TransferController.export_manifest(
            session=session,
            current_user=owner_a,
            filters=ObjectListParams(category_id=theirs.id),
        )
    assert exc.value.status_code == 403
