"""Tests for app/routes/category.py and controllers/category.py."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, col, select

from auth_sdk_m8.schemas.user import UserModel
from media_service.controllers.category import CategoryController
from media_service.db_models.categories import (
    Category,
    CategoryCreate,
    CategoryGenerators,
    CategoryUpdate,
)
from media_service.db_models.media_object_categories import MediaObjectCategoryLink
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObject,
    MediaVisibility,
)


# ── slug auto-generation validator ────────────────────────────────────────────


def test_generate_slug_from_name():
    values = CategoryGenerators.generate_slug({"name": "My Category"})
    assert values["slug"] == "my-category"


def test_generate_slug_skips_when_name_missing():
    """Falsy/absent name leaves values untouched (no slug generated)."""
    values = CategoryGenerators.generate_slug({})
    assert "slug" not in values


def _make_category(
    session: Session,
    owner_id: uuid.UUID,
    name: str = "TestCat",
    *,
    parent_id: int | None = None,
) -> Category:
    """Insert a category owned by the given user."""
    cat = Category(
        name=name,
        slug=name.lower().replace(" ", "-"),
        owner_id=owner_id,
        parent_id=parent_id,
    )
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _make_object(session: Session, owner_id: uuid.UUID) -> MediaObject:
    """Insert a minimal media object, for filing into categories."""
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


def _file_into(session: Session, obj: MediaObject, category: Category) -> None:
    """File a media object into a user category via the M2M link table."""
    session.add(
        MediaObjectCategoryLink(media_object_id=obj.id, category_id=category.id)
    )
    session.commit()


# ── GET /media/category/ ──────────────────────────────────────────────────────


def test_list_categories_empty(client: TestClient):
    resp = client.get("/media/category/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["data"] == []


def test_list_categories_returns_own(
    client: TestClient, session: Session, current_user
):
    _make_category(session, current_user.id, "MyDoc")
    resp = client.get("/media/category/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert [c["name"] for c in body["data"]] == ["MyDoc"]


def test_list_categories_excludes_another_owner(
    client: TestClient, session: Session, current_user, superuser
):
    _make_category(session, current_user.id, "Mine")
    _make_category(session, superuser.id, "Theirs")
    resp = client.get("/media/category/")
    assert resp.status_code == 200
    assert [c["name"] for c in resp.json()["data"]] == ["Mine"]


def test_list_categories_superuser_sees_all(
    superuser_client: TestClient, session: Session, current_user, superuser
):
    _make_category(session, current_user.id, "OwnerCat")
    _make_category(session, superuser.id, "SuperCat")
    resp = superuser_client.get("/media/category/")
    assert resp.status_code == 200
    assert resp.json()["count"] >= 2


def test_list_categories_counts_the_scope_not_the_page(
    client: TestClient, session: Session, current_user
):
    """``count`` is the in-scope total so a client can page against it."""
    for index in range(3):
        _make_category(session, current_user.id, f"Cat{index}")
    resp = client.get("/media/category/", params={"limit": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert (len(body["data"]), body["count"]) == (1, 3)


# ── GET /media/category/tree/ ─────────────────────────────────────────────────


def test_get_tree_empty(client: TestClient):
    resp = client.get("/media/category/tree/")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["data"], body["count"]) == ([], 0)


def test_get_tree_nests_children_and_rolls_up_counts(
    client: TestClient, session: Session, current_user
):
    root = _make_category(session, current_user.id, "Root")
    child = _make_category(session, current_user.id, "Child", parent_id=root.id)
    grandchild = _make_category(
        session, current_user.id, "Grandchild", parent_id=child.id
    )
    empty_sibling = _make_category(session, current_user.id, "Empty", parent_id=root.id)

    obj_on_root = _make_object(session, current_user.id)
    _file_into(session, obj_on_root, root)
    obj_on_grandchild = _make_object(session, current_user.id)
    _file_into(session, obj_on_grandchild, grandchild)

    resp = client.get("/media/category/tree/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 4

    [root_node] = body["data"]
    assert root_node["id"] == root.id
    assert (root_node["object_count"], root_node["total_object_count"]) == (1, 2)

    by_id = {node["id"]: node for node in root_node["children"]}
    assert (by_id[child.id]["object_count"], by_id[child.id]["total_object_count"]) == (
        0,
        1,
    )
    assert (
        by_id[empty_sibling.id]["object_count"],
        by_id[empty_sibling.id]["total_object_count"],
    ) == (
        0,
        0,
    )
    [grandchild_node] = by_id[child.id]["children"]
    assert grandchild_node["id"] == grandchild.id
    assert (
        grandchild_node["object_count"],
        grandchild_node["total_object_count"],
    ) == (1, 1)


def test_get_tree_excludes_another_owner(
    client: TestClient, session: Session, current_user, superuser
):
    _make_category(session, current_user.id, "Mine")
    _make_category(session, superuser.id, "Theirs")
    resp = client.get("/media/category/tree/")
    assert resp.status_code == 200
    body = resp.json()
    assert [node["name"] for node in body["data"]] == ["Mine"]


def test_get_tree_shares_within_the_same_tenant(session: Session):
    tenant = uuid.uuid4()
    creator = _tenanted_user(tenant)
    CategoryController.create_category(
        session=session, current_user=creator, req=CategoryCreate(name="TeamDocs")
    )
    other_member = _tenanted_user(tenant)
    result = CategoryController.get_category_tree(
        session=session, current_user=other_member
    )
    assert [node.name for node in result.data] == ["TeamDocs"]
    assert result.count == 1


def test_get_tree_excludes_a_different_tenant(session: Session):
    CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(uuid.uuid4()),
        req=CategoryCreate(name="TenantA"),
    )
    result = CategoryController.get_category_tree(
        session=session, current_user=_tenanted_user(uuid.uuid4())
    )
    assert (result.data, result.count) == ([], 0)


def test_get_tree_superuser_sees_across_tenants(
    session: Session, current_user, superuser
):
    _make_category(session, current_user.id, "OwnerCat")
    CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(uuid.uuid4()),
        req=CategoryCreate(name="TenantCat"),
    )
    result = CategoryController.get_category_tree(
        session=session, current_user=superuser
    )
    assert {node.name for node in result.data} >= {"OwnerCat", "TenantCat"}


# ── GET /media/category/get/{id}/ ─────────────────────────────────────────────


def test_get_category_found(client: TestClient, session: Session, current_user):
    cat = _make_category(session, current_user.id)
    resp = client.get(f"/media/category/get/{cat.id}/")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["id"], body["name"], body["slug"]) == (cat.id, "TestCat", "testcat")
    assert body["parent_id"] is None


def test_get_category_not_found(client: TestClient):
    """A missing category is a typed 404, not a 200 carrying a false success."""
    resp = client.get("/media/category/get/99999/")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Category not found."


def test_get_category_forbidden_other_owner(
    client: TestClient, session: Session, superuser
):
    cat = _make_category(session, superuser.id, "OtherCat")
    resp = client.get(f"/media/category/get/{cat.id}/")
    assert resp.status_code == 403


# ── POST /media/category/add/ ─────────────────────────────────────────────────


def test_create_category(client: TestClient, current_user):
    resp = client.post("/media/category/add/", json={"name": "NewCat"})
    assert resp.status_code == 201
    body = resp.json()
    assert (body["name"], body["slug"]) == ("NewCat", "newcat")
    assert body["owner_id"] == str(current_user.id)


# ── PUT /media/category/edit/{id}/ ───────────────────────────────────────────


def test_update_category(client: TestClient, session: Session, current_user):
    cat = _make_category(session, current_user.id, "OldName")
    resp = client.put(
        f"/media/category/edit/{cat.id}/",
        json={"name": "NewName"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert (body["id"], body["name"], body["slug"]) == (cat.id, "NewName", "newname")


def test_update_category_not_found(client: TestClient):
    resp = client.put("/media/category/edit/99999/", json={"name": "X"})
    assert resp.status_code == 404


def test_update_category_forbidden(client: TestClient, session: Session, superuser):
    cat = _make_category(session, superuser.id, "NotMine")
    resp = client.put(f"/media/category/edit/{cat.id}/", json={"name": "Hacked"})
    assert resp.status_code == 403


# ── DELETE /media/category/delete/{id}/ ──────────────────────────────────────


def test_delete_category(client: TestClient, session: Session, current_user):
    cat = _make_category(session, current_user.id, "ToDelete")
    resp = client.delete(f"/media/category/delete/{cat.id}/")
    assert resp.status_code == 204
    assert resp.content == b""


def test_delete_category_not_found(client: TestClient):
    resp = client.delete("/media/category/delete/99999/")
    assert resp.status_code == 404


def test_delete_category_forbidden(client: TestClient, session: Session, superuser):
    cat = _make_category(session, superuser.id, "Protected")
    resp = client.delete(f"/media/category/delete/{cat.id}/")
    assert resp.status_code == 403


# ── Controller: failures surface instead of being swallowed ──────────────────

_ERR_USER = UserModel(
    id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    email="err@test.com",
    is_active=True,
    is_superuser=False,
    role="user",
)


def test_a_storage_failure_is_not_masked_as_a_success():
    """The broad except is gone: a DB error propagates, it is not swallowed.

    The old route answered 200 with a false success flag (or a 500 JSONResponse)
    for any exception, which hid a failed read or commit from the caller. A real
    error now reaches the app error handling untouched.
    """
    bad_session = MagicMock()
    bad_session.exec.side_effect = RuntimeError("DB error")
    bad_session.scalar.side_effect = RuntimeError("DB error")

    with pytest.raises(RuntimeError):
        CategoryController.list_categories(session=bad_session, current_user=_ERR_USER)


def test_a_commit_failure_on_create_is_not_masked():
    bad_session = MagicMock()
    # The sibling-slug pre-check runs first; leave it finding nothing so the
    # commit is what fails.
    bad_session.exec.return_value.first.return_value = None
    bad_session.commit.side_effect = RuntimeError("DB error")

    with pytest.raises(RuntimeError):
        CategoryController.create_category(
            session=bad_session,
            current_user=_ERR_USER,
            req=CategoryCreate(name="ErrCat"),
        )


def test_controller_refusals_are_typed_http_exceptions():
    """Missing gives 404 and a foreign row gives 403, both as HTTPException."""
    missing_session = MagicMock()
    missing_session.get.return_value = None
    with pytest.raises(HTTPException) as missing:
        CategoryController.get_category(
            session=missing_session, current_user=_ERR_USER, category_id=1
        )
    assert missing.value.status_code == 404

    foreign_session = MagicMock()
    foreign_session.get.return_value = Category(
        id=1, name="Theirs", slug="theirs", owner_id=uuid.uuid4()
    )
    with pytest.raises(HTTPException) as foreign:
        CategoryController.get_category(
            session=foreign_session, current_user=_ERR_USER, category_id=1
        )
    assert foreign.value.status_code == 403


# ── Tenant scoping (`D4`) ──────────────────────────────────────────────────────
# UserModel does not (yet) carry a tenant claim (`D1`/`D4`), so these exercise
# the controller directly with a ``SimpleNamespace`` principal, mirroring
# ``tests/test_presets.py``'s ``_tenanted_user`` pattern.


def _tenanted_user(tenant_id: uuid.UUID, user_id: uuid.UUID | None = None):
    return SimpleNamespace(
        id=user_id or uuid.uuid4(), is_superuser=False, tenant_id=tenant_id
    )


def test_create_category_stamps_the_callers_tenant(session: Session):
    tenant = uuid.uuid4()
    user = _tenanted_user(tenant)
    out = CategoryController.create_category(
        session=session, current_user=user, req=CategoryCreate(name="Shared")
    )
    row = session.get(Category, out.id)
    assert row is not None
    assert row.tenant_id == tenant


def test_list_categories_shares_within_the_same_tenant(session: Session):
    """Two different owners in the same tenant both see the shared category."""
    tenant = uuid.uuid4()
    creator = _tenanted_user(tenant)
    CategoryController.create_category(
        session=session, current_user=creator, req=CategoryCreate(name="TeamDocs")
    )
    other_member = _tenanted_user(tenant)
    result = CategoryController.list_categories(
        session=session, current_user=other_member
    )
    assert [c.name for c in result.data] == ["TeamDocs"]


def test_list_categories_excludes_a_different_tenant(session: Session):
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(tenant_a),
        req=CategoryCreate(name="TenantA"),
    )
    result = CategoryController.list_categories(
        session=session, current_user=_tenanted_user(tenant_b)
    )
    assert result.data == []


def test_list_categories_untenanted_owner_excludes_tenant_rows(
    session: Session, current_user
):
    """An untenanted caller never sees a row filed under someone's tenant, even
    from the same ``owner_id`` — the fallback scope is restricted to
    untenanted rows (`_scoped_query`)."""
    owner_id = uuid.UUID(str(current_user.id))
    CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(uuid.uuid4(), user_id=owner_id),
        req=CategoryCreate(name="TenantOwned"),
    )
    result = CategoryController.list_categories(
        session=session, current_user=current_user
    )
    assert result.data == []


def test_get_category_within_same_tenant_is_allowed(session: Session):
    tenant = uuid.uuid4()
    creator = _tenanted_user(tenant)
    created = CategoryController.create_category(
        session=session, current_user=creator, req=CategoryCreate(name="Shared")
    )
    other_member = _tenanted_user(tenant)
    fetched = CategoryController.get_category(
        session=session, current_user=other_member, category_id=created.id
    )
    assert fetched.id == created.id


def test_get_category_across_tenants_is_forbidden(session: Session):
    creator = _tenanted_user(uuid.uuid4())
    created = CategoryController.create_category(
        session=session, current_user=creator, req=CategoryCreate(name="Theirs")
    )
    outsider = _tenanted_user(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        CategoryController.get_category(
            session=session, current_user=outsider, category_id=created.id
        )
    assert exc.value.status_code == 403


def test_get_category_superuser_bypasses_tenant_scope(session: Session, superuser):
    """A superuser reads any tenant's category without a scope check."""
    created = CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(uuid.uuid4()),
        req=CategoryCreate(name="ForeignTenant"),
    )
    fetched = CategoryController.get_category(
        session=session, current_user=superuser, category_id=created.id
    )
    assert fetched.id == created.id


def test_superuser_sees_categories_across_tenants(
    session: Session, current_user, superuser
):
    CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(uuid.uuid4()),
        req=CategoryCreate(name="TenantA"),
    )
    CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(uuid.uuid4()),
        req=CategoryCreate(name="TenantB"),
    )
    result = CategoryController.list_categories(session=session, current_user=superuser)
    assert {c.name for c in result.data} >= {"TenantA", "TenantB"}


# ── Hierarchy guards: parent scope, cycles, sibling slugs, delete ─────────────


def test_create_under_a_parent_nests_the_new_category(
    client: TestClient, session: Session, current_user
):
    parent = _make_category(session, current_user.id, "Docs")
    resp = client.post(
        "/media/category/add/", json={"name": "Invoices", "parent_id": parent.id}
    )
    assert resp.status_code == 201
    assert resp.json()["parent_id"] == parent.id


def test_create_under_a_missing_parent_is_404(client: TestClient):
    resp = client.post(
        "/media/category/add/", json={"name": "Orphan", "parent_id": 99999}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Parent category not found."


def test_create_under_another_owners_parent_is_forbidden(
    client: TestClient, session: Session, superuser
):
    parent = _make_category(session, superuser.id, "Theirs")
    resp = client.post(
        "/media/category/add/", json={"name": "Mine", "parent_id": parent.id}
    )
    assert resp.status_code == 403


def test_create_under_another_tenants_parent_is_rejected(session: Session):
    """The structural guard, reached past every permission check.

    A superuser is in scope everywhere, so only the scope-key comparison stops
    them grafting a foreign tenant's row onto their own tree.
    """
    foreign = CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(uuid.uuid4()),
        req=CategoryCreate(name="TenantRoot"),
    )
    grafter = SimpleNamespace(id=uuid.uuid4(), is_superuser=True, tenant_id=None)
    with pytest.raises(HTTPException) as exc:
        CategoryController.create_category(
            session=session,
            current_user=grafter,
            req=CategoryCreate(name="Graft", parent_id=foreign.id),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "Parent category belongs to another tenant."


def test_reparent_across_tenants_is_rejected(session: Session):
    tenant_a = CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(uuid.uuid4()),
        req=CategoryCreate(name="TenantA"),
    )
    tenant_b = CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(uuid.uuid4()),
        req=CategoryCreate(name="TenantB"),
    )
    grafter = SimpleNamespace(id=uuid.uuid4(), is_superuser=True, tenant_id=None)
    with pytest.raises(HTTPException) as exc:
        CategoryController.update_category(
            session=session,
            current_user=grafter,
            category_id=tenant_b.id,
            req=CategoryUpdate(name="TenantB", parent_id=tenant_a.id),
        )
    assert exc.value.status_code == 409


def test_reparent_to_another_owners_category_is_forbidden(
    client: TestClient, session: Session, current_user, superuser
):
    mine = _make_category(session, current_user.id, "Mine")
    theirs = _make_category(session, superuser.id, "Theirs")
    resp = client.put(
        f"/media/category/edit/{mine.id}/",
        json={"name": "Mine", "parent_id": theirs.id},
    )
    assert resp.status_code == 403


def test_self_parenting_is_rejected(client: TestClient, session: Session, current_user):
    cat = _make_category(session, current_user.id, "Loop")
    resp = client.put(
        f"/media/category/edit/{cat.id}/",
        json={"name": "Loop", "parent_id": cat.id},
    )
    assert resp.status_code == 409
    assert (
        resp.json()["detail"]
        == "A category cannot be nested under itself or its descendants."
    )


def test_deeper_cycle_is_rejected(client: TestClient, session: Session, current_user):
    """Root -> child -> grandchild; moving the root under the grandchild loops."""
    root = _make_category(session, current_user.id, "Root")
    child = _make_category(session, current_user.id, "Child", parent_id=root.id)
    grandchild = _make_category(
        session, current_user.id, "Grandchild", parent_id=child.id
    )
    resp = client.put(
        f"/media/category/edit/{root.id}/",
        json={"name": "Root", "parent_id": grandchild.id},
    )
    assert resp.status_code == 409


def test_a_preexisting_cycle_on_disk_does_not_hang_the_walk(
    session: Session, current_user
):
    """A loop written straight to the DB is broken, not followed forever.

    The controller cannot create this, but nothing stops a direct write or a
    row that predates the guard, and a request must not hang on one. The walk
    from ``mover`` goes first -> second -> first and stops, so the unrelated
    reparent still succeeds.
    """
    first = _make_category(session, current_user.id, "First")
    second = _make_category(session, current_user.id, "Second", parent_id=first.id)
    first.parent_id = second.id
    session.add(first)
    session.commit()

    mover = _make_category(session, current_user.id, "Mover")
    out = CategoryController.update_category(
        session=session,
        current_user=current_user,
        category_id=mover.id,
        req=CategoryUpdate(name="Mover", parent_id=first.id),
    )
    assert out.parent_id == first.id


def test_a_dangling_parent_reference_ends_the_cycle_walk(
    session: Session, current_user
):
    """An ancestor row that no longer exists ends the walk instead of raising."""
    orphan = _make_category(session, current_user.id, "Orphan")
    orphan.parent_id = 99999
    session.add(orphan)
    session.commit()

    mover = _make_category(session, current_user.id, "Mover")
    out = CategoryController.update_category(
        session=session,
        current_user=current_user,
        category_id=mover.id,
        req=CategoryUpdate(name="Mover", parent_id=orphan.id),
    )
    assert out.parent_id == orphan.id


def test_reparenting_to_a_root_is_allowed(
    client: TestClient, session: Session, current_user
):
    root = _make_category(session, current_user.id, "Root")
    child = _make_category(session, current_user.id, "Child", parent_id=root.id)
    resp = client.put(
        f"/media/category/edit/{child.id}/",
        json={"name": "Child", "parent_id": None},
    )
    assert resp.status_code == 200
    assert resp.json()["parent_id"] is None


def test_duplicate_root_slug_is_rejected(
    client: TestClient, session: Session, current_user
):
    """U3's carry-in: two untenanted roots are NULL/NULL, which the composite
    unique constraint cannot separate, so the controller has to."""
    _make_category(session, current_user.id, "Docs")
    resp = client.post("/media/category/add/", json={"name": "Docs"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "A sibling category already uses this name."


def test_duplicate_sibling_slug_under_a_parent_is_rejected(
    client: TestClient, session: Session, current_user
):
    parent = _make_category(session, current_user.id, "Docs")
    _make_category(session, current_user.id, "Invoices", parent_id=parent.id)
    resp = client.post(
        "/media/category/add/", json={"name": "Invoices", "parent_id": parent.id}
    )
    assert resp.status_code == 409


def test_the_same_slug_under_a_different_parent_is_allowed(
    client: TestClient, session: Session, current_user
):
    first = _make_category(session, current_user.id, "First")
    second = _make_category(session, current_user.id, "Second")
    _make_category(session, current_user.id, "Invoices", parent_id=first.id)
    resp = client.post(
        "/media/category/add/", json={"name": "Invoices", "parent_id": second.id}
    )
    assert resp.status_code == 201


def test_another_owners_root_slug_does_not_collide(
    client: TestClient, session: Session, superuser
):
    _make_category(session, superuser.id, "Docs")
    resp = client.post("/media/category/add/", json={"name": "Docs"})
    assert resp.status_code == 201


def test_a_tenant_siblings_slug_collides_across_owners(session: Session):
    """Categories are shared within a tenant, so their uniqueness is too."""
    tenant = uuid.uuid4()
    CategoryController.create_category(
        session=session,
        current_user=_tenanted_user(tenant),
        req=CategoryCreate(name="TeamDocs"),
    )
    with pytest.raises(HTTPException) as exc:
        CategoryController.create_category(
            session=session,
            current_user=_tenanted_user(tenant),
            req=CategoryCreate(name="TeamDocs"),
        )
    assert exc.value.status_code == 409


def test_renaming_onto_a_siblings_slug_is_rejected(
    client: TestClient, session: Session, current_user
):
    _make_category(session, current_user.id, "Docs")
    other = _make_category(session, current_user.id, "Assets")
    resp = client.put(f"/media/category/edit/{other.id}/", json={"name": "Docs"})
    assert resp.status_code == 409


def test_reparenting_onto_a_taken_sibling_slug_is_rejected(
    client: TestClient, session: Session, current_user
):
    parent = _make_category(session, current_user.id, "Docs")
    _make_category(session, current_user.id, "Invoices", parent_id=parent.id)
    loose = _make_category(session, current_user.id, "Invoices2")
    loose.name, loose.slug = "Invoices", "invoices"
    session.add(loose)
    session.commit()

    resp = client.put(
        f"/media/category/edit/{loose.id}/",
        json={"name": "Invoices", "parent_id": parent.id},
    )
    assert resp.status_code == 409


def test_renaming_a_category_to_its_own_name_is_allowed(
    client: TestClient, session: Session, current_user
):
    """The row excludes itself from the sibling check, so a no-op rename works."""
    cat = _make_category(session, current_user.id, "Docs")
    resp = client.put(f"/media/category/edit/{cat.id}/", json={"name": "Docs"})
    assert resp.status_code == 200
    assert resp.json()["slug"] == "docs"


def test_delete_with_children_is_rejected(
    client: TestClient, session: Session, current_user
):
    root = _make_category(session, current_user.id, "Root")
    _make_category(session, current_user.id, "Child", parent_id=root.id)
    resp = client.delete(f"/media/category/delete/{root.id}/")
    assert resp.status_code == 409
    assert (
        resp.json()["detail"]
        == "Category has child categories; reparent or delete them first."
    )
    assert session.get(Category, root.id) is not None


def test_delete_succeeds_once_the_children_are_reparented(
    client: TestClient, session: Session, current_user
):
    root = _make_category(session, current_user.id, "Root")
    child = _make_category(session, current_user.id, "Child", parent_id=root.id)
    reparented = client.put(
        f"/media/category/edit/{child.id}/",
        json={"name": "Child", "parent_id": None},
    )
    assert reparented.status_code == 200
    assert client.delete(f"/media/category/delete/{root.id}/").status_code == 204


def test_delete_with_assigned_media_detaches_the_links_and_keeps_the_media(
    client: TestClient, session: Session, current_user
):
    """Media outlives its filing: the link rows go, the objects stay."""
    cat = _make_category(session, current_user.id, "Filed")
    obj = _make_object(session, current_user.id)
    _file_into(session, obj, cat)

    resp = client.delete(f"/media/category/delete/{cat.id}/")
    assert resp.status_code == 204
    assert session.get(Category, cat.id) is None
    assert session.get(MediaObject, obj.id) is not None
    remaining = session.exec(
        select(MediaObjectCategoryLink).where(
            col(MediaObjectCategoryLink.category_id) == cat.id
        )
    ).all()
    assert remaining == []
