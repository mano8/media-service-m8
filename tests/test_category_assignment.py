"""Filing media into user categories via upload initiate/complete and PATCH (`U4`).

The three surfaces share one trust boundary —
``controllers.category.resolve_category_ids`` — so the refusal cases are proved
once per surface and the resolver's own edges (de-duplication, the lenient
replay, path projection) are driven directly.
"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import JSON
from sqlmodel import Session, SQLModel, col, select

from media_service.controllers.category import (
    assigned_category_refs,
    categories_in_scope,
    resolve_category_ids,
)
from media_service.core.db_models import prefixed_tables
from media_service.db_models.categories import MAX_CATEGORY_ASSIGNMENTS, Category
from media_service.db_models.media_object_categories import MediaObjectCategoryLink
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObject,
    MediaVisibility,
)
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus


# ── helpers ───────────────────────────────────────────────────────────────────

_INITIATE_BODY = {
    "category": "document",
    "visibility": "private",
    "original_filename": "report.pdf",
    "mime_type": "application/pdf",
    "expected_size_bytes": 2048,
}

# Leading bytes the sniffer recognises as application/pdf, matching the declared
# type on the sessions below so content validation passes.
_PDF_BYTES = b"%PDF-1.4" + b"\x00" * 504


def _tenanted_user(tenant_id: uuid.UUID, user_id: uuid.UUID | None = None):
    """Principal carrying a tenant claim (`D1`/`D4`: ``UserModel`` has none)."""
    return SimpleNamespace(
        id=user_id or uuid.uuid4(), is_superuser=False, tenant_id=tenant_id
    )


def _make_category(
    session: Session,
    owner_id: uuid.UUID,
    name: str = "Invoices",
    *,
    parent_id: int | None = None,
    tenant_id: uuid.UUID | None = None,
) -> Category:
    """Insert a category owned by the given user."""
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


def _make_object(session: Session, owner_id: uuid.UUID) -> MediaObject:
    """Insert a minimal media object to file into categories."""
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category=MediaCategory.DOCUMENT,
        visibility=MediaVisibility.PRIVATE,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{oid}/original/file.pdf",
        original_filename="file.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _make_session(
    session: Session,
    owner_id: uuid.UUID,
    *,
    category_ids: list[int] | None = None,
) -> UploadSession:
    """Insert an INITIATED upload session carrying a declared filing."""
    sid = uuid.uuid4()
    us = UploadSession(
        id=sid,
        owner_user_id=owner_id,
        category="document",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{sid}/original/report.pdf",
        expected_mime_type="application/pdf",
        expected_size_bytes=2048,
        expires_at=datetime.utcnow() + timedelta(seconds=300),
        category_ids=category_ids,
    )
    session.add(us)
    session.commit()
    session.refresh(us)
    return us


def _stat_mock(size: int = 2048, etag: str = "etag123") -> MagicMock:
    stat = MagicMock()
    stat.size = size
    stat.etag = etag
    return stat


def _ready_storage(mock_storage: MagicMock) -> None:
    """Make a staged object pass every completion check."""
    mock_storage.stat_object.return_value = _stat_mock()
    mock_storage.get_object_head.return_value = _PDF_BYTES


def _filed_ids(session: Session, object_id: uuid.UUID) -> set[int]:
    """Category ids the link table records for an object."""
    rows = session.exec(
        select(MediaObjectCategoryLink).where(
            col(MediaObjectCategoryLink.media_object_id) == object_id
        )
    ).all()
    return {row.category_id for row in rows}


# ── POST /media/v1/uploads/initiate ──────────────────────────────────────────


def test_initiate_stores_the_declared_filing_on_the_session(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    cat = _make_category(session, current_user.id)
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    resp = client.post(
        "/media/v1/uploads/initiate",
        json={**_INITIATE_BODY, "category_ids": [cat.id]},
    )
    assert resp.status_code == 200
    stored = session.get(UploadSession, uuid.UUID(resp.json()["session_id"]))
    assert stored is not None
    assert stored.category_ids == [cat.id]


def test_initiate_without_category_ids_declares_no_filing(
    client: TestClient, mock_storage: MagicMock, session: Session
):
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    resp = client.post("/media/v1/uploads/initiate", json=_INITIATE_BODY)
    assert resp.status_code == 200
    stored = session.get(UploadSession, uuid.UUID(resp.json()["session_id"]))
    assert stored is not None
    assert stored.category_ids == []


def test_initiate_collapses_a_repeated_category_id(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    """A filing is a set — naming the same category twice is not an error."""
    cat = _make_category(session, current_user.id)
    mock_storage.presigned_post_object.return_value = ("https://minio/b", {})
    resp = client.post(
        "/media/v1/uploads/initiate",
        json={**_INITIATE_BODY, "category_ids": [cat.id, cat.id]},
    )
    assert resp.status_code == 200
    stored = session.get(UploadSession, uuid.UUID(resp.json()["session_id"]))
    assert stored is not None
    assert stored.category_ids == [cat.id]


def test_initiate_with_an_unknown_category_is_404_and_issues_no_url(
    client: TestClient, mock_storage: MagicMock, session: Session
):
    resp = client.post(
        "/media/v1/uploads/initiate",
        json={**_INITIATE_BODY, "category_ids": [4242]},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Category not found."
    mock_storage.presigned_post_object.assert_not_called()
    assert session.exec(select(UploadSession)).all() == []


def test_initiate_with_another_owners_category_is_forbidden(
    client: TestClient, mock_storage: MagicMock, session: Session, superuser
):
    foreign = _make_category(session, superuser.id, "Theirs")
    resp = client.post(
        "/media/v1/uploads/initiate",
        json={**_INITIATE_BODY, "category_ids": [foreign.id]},
    )
    assert resp.status_code == 403
    mock_storage.presigned_post_object.assert_not_called()
    assert session.exec(select(UploadSession)).all() == []


def test_initiate_rejects_more_ids_than_the_assignment_cap(
    client: TestClient, mock_storage: MagicMock
):
    resp = client.post(
        "/media/v1/uploads/initiate",
        json={
            **_INITIATE_BODY,
            "category_ids": list(range(1, MAX_CATEGORY_ASSIGNMENTS + 2)),
        },
    )
    assert resp.status_code == 422
    mock_storage.presigned_post_object.assert_not_called()


# ── POST /media/v1/uploads/{id}/complete ─────────────────────────────────────


def test_complete_applies_the_filing_declared_at_initiate(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    parent = _make_category(session, current_user.id, "Documents")
    child = _make_category(session, current_user.id, "Invoices", parent_id=parent.id)
    us = _make_session(session, current_user.id, category_ids=[child.id])
    _ready_storage(mock_storage)
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200
    assert _filed_ids(session, us.id) == {child.id}
    # The response echoes the filing it just made, path resolved from the root.
    assert resp.json()["media_object"]["categories"] == [
        {"id": child.id, "name": "Invoices", "path": "documents/invoices"}
    ]


def test_complete_with_category_ids_replaces_the_declared_filing(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    declared = _make_category(session, current_user.id, "Declared")
    chosen = _make_category(session, current_user.id, "Chosen")
    us = _make_session(session, current_user.id, category_ids=[declared.id])
    _ready_storage(mock_storage)
    resp = client.post(
        f"/media/v1/uploads/{us.id}/complete", json={"category_ids": [chosen.id]}
    )
    assert resp.status_code == 200
    assert _filed_ids(session, us.id) == {chosen.id}


def test_complete_with_an_empty_list_completes_the_object_unfiled(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    declared = _make_category(session, current_user.id, "Declared")
    us = _make_session(session, current_user.id, category_ids=[declared.id])
    _ready_storage(mock_storage)
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={"category_ids": []})
    assert resp.status_code == 200
    assert _filed_ids(session, us.id) == set()
    assert resp.json()["media_object"]["categories"] == []


def test_complete_drops_a_category_deleted_since_initiate(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    """The bytes are already stored, so a stale declaration must not strand them."""
    kept = _make_category(session, current_user.id, "Kept")
    doomed = _make_category(session, current_user.id, "Doomed")
    us = _make_session(session, current_user.id, category_ids=[kept.id, doomed.id])
    session.delete(doomed)
    session.commit()
    _ready_storage(mock_storage)
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200
    assert _filed_ids(session, us.id) == {kept.id}


def test_complete_with_a_foreign_category_is_refused_and_stays_completable(
    client: TestClient,
    mock_storage: MagicMock,
    session: Session,
    current_user,
    superuser,
):
    foreign = _make_category(session, superuser.id, "Theirs")
    us = _make_session(session, current_user.id)
    _ready_storage(mock_storage)
    resp = client.post(
        f"/media/v1/uploads/{us.id}/complete", json={"category_ids": [foreign.id]}
    )
    assert resp.status_code == 403
    session.refresh(us)
    assert us.status == UploadSessionStatus.INITIATED
    assert session.get(MediaObject, us.id) is None


def test_complete_with_an_unknown_category_is_404(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    us = _make_session(session, current_user.id)
    _ready_storage(mock_storage)
    resp = client.post(
        f"/media/v1/uploads/{us.id}/complete", json={"category_ids": [4242]}
    )
    assert resp.status_code == 404
    session.refresh(us)
    assert us.status == UploadSessionStatus.INITIATED


def test_complete_of_a_session_predating_the_column_files_nothing(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    """A null ``category_ids`` reads as "no filing declared", not as an error."""
    us = _make_session(session, current_user.id, category_ids=None)
    _ready_storage(mock_storage)
    resp = client.post(f"/media/v1/uploads/{us.id}/complete", json={})
    assert resp.status_code == 200
    assert _filed_ids(session, us.id) == set()
    assert resp.json()["media_object"]["categories"] == []


# ── PATCH /media/v1/objects/{id} ─────────────────────────────────────────────


def test_get_object_includes_existing_category_assignments(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    parent = _make_category(session, current_user.id, "Documents")
    child = _make_category(session, current_user.id, "Invoices", parent_id=parent.id)
    session.add(MediaObjectCategoryLink(media_object_id=obj.id, category_id=child.id))
    session.commit()

    resp = client.get(f"/media/v1/objects/{obj.id}")

    assert resp.status_code == 200
    assert resp.json()["categories"] == [
        {"id": child.id, "name": "Invoices", "path": "documents/invoices"}
    ]


def test_patch_category_ids_replaces_prior_assignments(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    old = _make_category(session, current_user.id, "Old")
    new = _make_category(session, current_user.id, "New")
    other = _make_category(session, current_user.id, "Other")
    session.add(MediaObjectCategoryLink(media_object_id=obj.id, category_id=old.id))
    session.commit()
    resp = client.patch(
        f"/media/v1/objects/{obj.id}", json={"category_ids": [new.id, other.id]}
    )
    assert resp.status_code == 200
    assert _filed_ids(session, obj.id) == {new.id, other.id}
    assert {c["id"] for c in resp.json()["categories"]} == {new.id, other.id}


def test_patch_with_an_empty_list_unfiles_the_object(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    cat = _make_category(session, current_user.id)
    session.add(MediaObjectCategoryLink(media_object_id=obj.id, category_id=cat.id))
    session.commit()
    resp = client.patch(f"/media/v1/objects/{obj.id}", json={"category_ids": []})
    assert resp.status_code == 200
    assert _filed_ids(session, obj.id) == set()
    assert resp.json()["categories"] == []


def test_patch_without_category_ids_leaves_the_filing_untouched(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    cat = _make_category(session, current_user.id)
    session.add(MediaObjectCategoryLink(media_object_id=obj.id, category_id=cat.id))
    session.commit()
    resp = client.patch(
        f"/media/v1/objects/{obj.id}", json={"original_filename": "renamed.pdf"}
    )
    assert resp.status_code == 200
    assert resp.json()["original_filename"] == "renamed.pdf"
    assert _filed_ids(session, obj.id) == {cat.id}
    assert [c["id"] for c in resp.json()["categories"]] == [cat.id]


def test_patch_with_another_owners_category_is_forbidden(
    client: TestClient,
    mock_storage: MagicMock,
    session: Session,
    current_user,
    superuser,
):
    """The refusal lands before any byte movement, so nothing is half-applied."""
    obj = _make_object(session, current_user.id)
    foreign = _make_category(session, superuser.id, "Theirs")
    resp = client.patch(
        f"/media/v1/objects/{obj.id}",
        json={"visibility": "public", "category_ids": [foreign.id]},
    )
    assert resp.status_code == 403
    mock_storage.copy_object.assert_not_called()
    session.refresh(obj)
    assert obj.visibility == MediaVisibility.PRIVATE
    assert _filed_ids(session, obj.id) == set()


def test_patch_with_an_unknown_category_is_404(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    resp = client.patch(f"/media/v1/objects/{obj.id}", json={"category_ids": [4242]})
    assert resp.status_code == 404


def test_patch_rejects_more_ids_than_the_assignment_cap(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    resp = client.patch(
        f"/media/v1/objects/{obj.id}",
        json={"category_ids": list(range(1, MAX_CATEGORY_ASSIGNMENTS + 2))},
    )
    assert resp.status_code == 422


# ── the shared resolver (`D4` scope) ─────────────────────────────────────────


def test_resolve_accepts_a_category_shared_within_the_callers_tenant(
    session: Session,
):
    """Two owners in one tenant share the tree, so either may file into it."""
    tenant = uuid.uuid4()
    creator = _tenanted_user(tenant)
    shared = _make_category(
        session, uuid.UUID(str(creator.id)), "TeamDocs", tenant_id=tenant
    )
    other_member = _tenanted_user(tenant)
    resolved = resolve_category_ids(session, other_member, [shared.id])
    assert [row.id for row in resolved] == [shared.id]


def test_resolve_refuses_another_tenants_category(session: Session):
    tenant = uuid.uuid4()
    owner = _tenanted_user(tenant)
    theirs = _make_category(
        session, uuid.UUID(str(owner.id)), "TenantA", tenant_id=tenant
    )
    outsider = _tenanted_user(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        resolve_category_ids(session, outsider, [theirs.id])
    assert exc.value.status_code == 403


def test_resolve_of_an_empty_list_queries_nothing(session: Session, current_user):
    assert resolve_category_ids(session, current_user, []) == []
    assert categories_in_scope(session, current_user, []) == []


def test_resolve_preserves_the_order_the_ids_were_given_in(
    session: Session, current_user
):
    first = _make_category(session, current_user.id, "Alpha")
    second = _make_category(session, current_user.id, "Beta")
    resolved = resolve_category_ids(session, current_user, [second.id, first.id])
    assert [row.id for row in resolved] == [second.id, first.id]


def test_lenient_replay_drops_a_foreign_id_instead_of_refusing(
    session: Session, current_user, superuser
):
    mine = _make_category(session, current_user.id, "Mine")
    theirs = _make_category(session, superuser.id, "Theirs")
    kept = categories_in_scope(session, current_user, [mine.id, theirs.id])
    assert [row.id for row in kept] == [mine.id]


def test_refs_of_an_unfiled_object_are_empty(session: Session, current_user):
    assert assigned_category_refs(session, current_user, []) == []


# ── schema metadata (`D6`: revisions are autogenerated, never hand-written) ───


def test_upload_session_carries_the_declared_filing_column():
    """Alembic autogenerates against `SQLModel.metadata`, so assert it there.

    Nullable on purpose: a session created before this column existed must
    complete unfiled, and adding a NOT NULL column to a table that already has
    rows needs a server default the autogenerated revision would not supply.
    """
    table = SQLModel.metadata.tables[prefixed_tables("upload_session")]
    assert "category_ids" in table.c
    assert table.c.category_ids.nullable
    assert isinstance(table.c.category_ids.type, JSON)
