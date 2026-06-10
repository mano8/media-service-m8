"""Tests for app/routes/category.py."""

import asyncio
import uuid
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.responses import JSONResponse

from auth_sdk_m8.schemas.user import UserModel
from media_service.app.routes.category import create_item, read_item
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


def _owner_id(client_user_id: str) -> uuid.UUID:
    return uuid.UUID(client_user_id)


# ── GET /media/category/ ──────────────────────────────────────────────────────


def test_list_categories_empty(client: TestClient):
    resp = client.get("/media/category/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0


def test_list_categories_returns_own(
    client: TestClient, session: Session, current_user
):
    _make_category(session, current_user.id, "MyDoc")
    resp = client.get("/media/category/")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


def test_list_categories_superuser_sees_all(
    superuser_client: TestClient, session: Session, current_user, superuser
):
    _make_category(session, current_user.id, "OwnerCat")
    _make_category(session, superuser.id, "SuperCat")
    resp = superuser_client.get("/media/category/")
    assert resp.status_code == 200
    assert resp.json()["count"] >= 2


# ── GET /media/category/get/{id}/ ─────────────────────────────────────────────


def test_get_category_found(client: TestClient, session: Session, current_user):
    cat = _make_category(session, current_user.id)
    resp = client.get(f"/media/category/get/{cat.id}/")
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_get_category_not_found(client: TestClient):
    resp = client.get("/media/category/get/99999/")
    assert resp.status_code == 200
    assert resp.json()["success"] is False


def test_get_category_forbidden_other_owner(
    client: TestClient, session: Session, superuser
):
    cat = _make_category(session, superuser.id, "OtherCat")
    resp = client.get(f"/media/category/get/{cat.id}/")
    assert resp.status_code == 403


# ── POST /media/category/add/ ─────────────────────────────────────────────────


def test_create_category(client: TestClient):
    resp = client.post("/media/category/add/", json={"name": "NewCat"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ── PUT /media/category/edit/{id}/ ───────────────────────────────────────────


def test_update_category(client: TestClient, session: Session, current_user):
    cat = _make_category(session, current_user.id, "OldName")
    resp = client.put(
        f"/media/category/edit/{cat.id}/",
        json={"name": "NewName"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


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
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_delete_category_not_found(client: TestClient):
    resp = client.delete("/media/category/delete/99999/")
    assert resp.status_code == 404


def test_delete_category_forbidden(client: TestClient, session: Session, superuser):
    cat = _make_category(session, superuser.id, "Protected")
    resp = client.delete(f"/media/category/delete/{cat.id}/")
    assert resp.status_code == 403


# ── Exception handler coverage ───────────────────────────────────────────────

_ERR_USER = UserModel(
    id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    email="err@test.com",
    is_active=True,
    is_superuser=False,
    role="user",
)


def test_read_root_exception_handler():
    """Exercise the except block in the async read_root route."""
    from media_service.app.routes.category import read_root

    bad_session = MagicMock()
    bad_session.exec.side_effect = RuntimeError("DB error")

    result = asyncio.run(read_root(session=bad_session, current_user=_ERR_USER))
    assert isinstance(result, JSONResponse)
    assert result.status_code == 500


def test_read_item_exception_handler():
    """Exercise the except block in read_item."""
    bad_session = MagicMock()
    bad_session.get.side_effect = RuntimeError("DB error")

    result = read_item(session=bad_session, current_user=_ERR_USER, item_id=1)
    assert isinstance(result, JSONResponse)
    assert result.status_code == 500


def test_create_item_exception_handler():
    """Exercise the except block in create_item."""
    bad_session = MagicMock()
    bad_session.add.side_effect = RuntimeError("DB error")

    result = create_item(
        session=bad_session,
        current_user=_ERR_USER,
        item_in=CategoryCreate(name="ErrCat"),
    )
    assert isinstance(result, JSONResponse)
    assert result.status_code == 500
