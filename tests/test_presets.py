"""Tests for app/routes/presets.py and controllers/presets.py."""

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.controllers.presets import PresetsController
from media_service.core.presets import BUILTIN_PRESETS
from media_service.db_models.image_presets import ImagePreset
from media_service.schemas.presets import ImagePresetCreate, PresetSpec

_SPEC = {
    "image_size": {"fixed_width": 600},
    "formats": [{"ext": "WEBP", "quality": 80}],
    "allow_upscale": False,
    "max_byte_size": None,
}


def _add_row(session: Session, owner_id: uuid.UUID, name: str) -> ImagePreset:
    row = ImagePreset(owner_user_id=owner_id, name=name, spec=_SPEC)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_list_presets_includes_builtins(client: TestClient):
    resp = client.get("/media/v1/presets")
    assert resp.status_code == 200
    body = resp.json()
    names = {p["name"] for p in body if p["builtin"]}
    assert set(BUILTIN_PRESETS).issubset(names)


def test_list_presets_merges_user_rows(
    client: TestClient, session: Session, current_user
):
    _add_row(session, uuid.UUID(str(current_user.id)), "social")
    resp = client.get("/media/v1/presets")
    body = resp.json()
    user_rows = [p for p in body if not p["builtin"]]
    assert [p["name"] for p in user_rows] == ["social"]


def test_list_presets_user_shadows_builtin(
    client: TestClient, session: Session, current_user
):
    _add_row(session, uuid.UUID(str(current_user.id)), "thumb")
    resp = client.get("/media/v1/presets")
    body = resp.json()
    thumbs = [p for p in body if p["name"] == "thumb"]
    # Exactly one "thumb" — the user row, not the built-in.
    assert len(thumbs) == 1
    assert thumbs[0]["builtin"] is False


def test_create_preset(client: TestClient, session: Session, current_user):
    resp = client.post("/media/v1/presets", json={"name": "web", "spec": _SPEC})
    assert resp.status_code == 201
    assert resp.json()["name"] == "web"
    assert resp.json()["builtin"] is False


def test_create_duplicate_returns_409(
    client: TestClient, session: Session, current_user
):
    _add_row(session, uuid.UUID(str(current_user.id)), "web")
    resp = client.post("/media/v1/presets", json={"name": "web", "spec": _SPEC})
    assert resp.status_code == 409


def test_update_preset_replaces_spec(
    client: TestClient, session: Session, current_user
):
    row = _add_row(session, uuid.UUID(str(current_user.id)), "web")
    new_spec = {
        "image_size": {"fixed_height": 240},
        "formats": [{"ext": "JPEG", "quality": 70}],
    }
    resp = client.patch(f"/media/v1/presets/{row.id}", json={"spec": new_spec})
    assert resp.status_code == 200
    assert resp.json()["spec"]["formats"][0]["ext"] == "JPEG"


def test_update_unknown_returns_404(client: TestClient):
    resp = client.patch(f"/media/v1/presets/{uuid.uuid4()}", json={"spec": _SPEC})
    assert resp.status_code == 404


def test_update_foreign_preset_returns_403(client: TestClient, session: Session):
    row = _add_row(session, uuid.uuid4(), "web")
    resp = client.patch(f"/media/v1/presets/{row.id}", json={"spec": _SPEC})
    assert resp.status_code == 403


def test_delete_preset(client: TestClient, session: Session, current_user):
    row = _add_row(session, uuid.UUID(str(current_user.id)), "web")
    resp = client.delete(f"/media/v1/presets/{row.id}")
    assert resp.status_code == 204
    assert session.get(ImagePreset, row.id) is None


def test_delete_unknown_returns_404(client: TestClient):
    resp = client.delete(f"/media/v1/presets/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_superuser_can_update_foreign_preset(
    superuser_client: TestClient, session: Session
):
    row = _add_row(session, uuid.uuid4(), "web")
    resp = superuser_client.patch(f"/media/v1/presets/{row.id}", json={"spec": _SPEC})
    assert resp.status_code == 200


def _tenanted_user(tenant_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), is_superuser=False, tenant_id=tenant_id)


def test_list_presets_scopes_to_tenant(session: Session):
    tenant = uuid.uuid4()
    user = _tenanted_user(tenant)
    owner = uuid.UUID(str(user.id))
    session.add(
        ImagePreset(owner_user_id=owner, tenant_id=tenant, name="web", spec=_SPEC)
    )
    session.commit()
    result = PresetsController.list_presets(session=session, current_user=user)
    assert any(p.name == "web" and not p.builtin for p in result)


def test_create_preset_scopes_to_tenant(session: Session):
    tenant = uuid.uuid4()
    user = _tenanted_user(tenant)
    req = ImagePresetCreate(name="web", spec=PresetSpec.model_validate(_SPEC))
    out = PresetsController.create_preset(session=session, current_user=user, req=req)
    assert out.name == "web"
    row = session.get(ImagePreset, out.id)
    assert row is not None
    assert row.tenant_id == tenant
