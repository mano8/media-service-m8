"""Tests for app/routes/category.py and controllers/category.py."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session

from auth_sdk_m8.schemas.user import UserModel
from media_service.controllers.category import CategoryController
from media_service.db_models.categories import (
    Category,
    CategoryCreate,
    CategoryGenerators,
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
    session: Session, owner_id: uuid.UUID, name: str = "TestCat"
) -> Category:
    """Insert a category owned by the given user."""
    cat = Category(name=name, slug=name.lower().replace(" ", "-"), owner_id=owner_id)
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


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
