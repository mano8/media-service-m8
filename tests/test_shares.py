"""Tests for share links (model, controller, routes) — Phase 15b.

Covers create/list/revoke (owner-only) and public resolution, plus the
cross-cutting rules: signature verification, expiry, ``max_uses`` exhaustion,
revocation, scan-gating on resolve, and ON DELETE CASCADE when the parent
object is hard-purged.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from media_service.controllers.maintenance import MaintenanceController
from media_service.controllers.shares import (
    SharesController,
    _as_aware,
    _sign,
)
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
)
from media_service.db_models.share_tokens import ShareToken
from media_service.schemas.shares import ShareTokenCreate


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    scan_status: ScanStatus = ScanStatus.CLEAN,
    status: MediaObjectStatus = MediaObjectStatus.READY,
    deleted: bool = False,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category="document",
        visibility=MediaVisibility.PRIVATE,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{oid}/original/file.pdf",
        original_filename="file.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        status=status,
        scan_status=scan_status,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _make_share(
    session: Session,
    *,
    media_object_id: uuid.UUID,
    owner_id: uuid.UUID,
    expires_at: datetime | None = None,
    max_uses: int | None = None,
    uses: int = 0,
    revoked: bool = False,
) -> ShareToken:
    share = ShareToken(
        media_object_id=media_object_id,
        owner_user_id=owner_id,
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)),
        max_uses=max_uses,
        uses=uses,
        revoked=revoked,
    )
    session.add(share)
    session.commit()
    session.refresh(share)
    return share


# ── POST /v1/objects/{id}/shares (create) ─────────────────────────────────────


def test_create_share_happy(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    resp = client.post(f"/media/v1/objects/{obj.id}/shares", json={"max_uses": 3})
    assert resp.status_code == 201
    body = resp.json()
    assert body["media_object_id"] == str(obj.id)
    assert body["max_uses"] == 3
    assert body["uses"] == 0
    assert body["revoked"] is False
    # The signed token is "<row-id-hex>.<signature>".
    assert "." in body["token"]
    assert body["token"].split(".")[0] == body["id"].replace("-", "")


def test_create_share_default_expiry(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    resp = client.post(f"/media/v1/objects/{obj.id}/shares", json={})
    assert resp.status_code == 201
    expires_at = _as_aware(datetime.fromisoformat(resp.json()["expires_at"]))
    delta = expires_at - datetime.now(timezone.utc)
    # Default lifetime is 7 days; allow a generous window for clock/IO slack.
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, minutes=1)


def test_create_share_rejects_over_max_expiry(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    # One second past the default 30-day operator ceiling.
    resp = client.post(
        f"/media/v1/objects/{obj.id}/shares", json={"expires_in": 2_592_001}
    )
    assert resp.status_code == 422


def test_create_share_forbidden_non_owner(
    client: TestClient, session: Session, superuser
):
    obj = _make_object(session, superuser.id)
    resp = client.post(f"/media/v1/objects/{obj.id}/shares", json={})
    assert resp.status_code == 403


def test_create_share_object_not_found(client: TestClient):
    resp = client.post(f"/media/v1/objects/{uuid.uuid4()}/shares", json={})
    assert resp.status_code == 404


# ── GET /v1/objects/{id}/shares (list) ────────────────────────────────────────


def test_list_shares(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    _make_share(session, media_object_id=obj.id, owner_id=current_user.id)
    _make_share(session, media_object_id=obj.id, owner_id=current_user.id)
    resp = client.get(f"/media/v1/objects/{obj.id}/shares")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert len(body["items"]) == 2
    assert all("token" in item for item in body["items"])


def test_list_shares_forbidden_non_owner(
    client: TestClient, session: Session, superuser
):
    obj = _make_object(session, superuser.id)
    resp = client.get(f"/media/v1/objects/{obj.id}/shares")
    assert resp.status_code == 403


# ── DELETE /v1/shares/{token_id} (revoke) ─────────────────────────────────────


def test_revoke_share(client: TestClient, mock_storage, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    share = _make_share(session, media_object_id=obj.id, owner_id=current_user.id)
    resp = client.delete(f"/media/v1/shares/{share.id}")
    assert resp.status_code == 204
    # A revoked link no longer resolves.
    resolved = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resolved.status_code == 403


def test_revoke_share_idempotent(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session, media_object_id=obj.id, owner_id=current_user.id, revoked=True
    )
    resp = client.delete(f"/media/v1/shares/{share.id}")
    assert resp.status_code == 204


def test_revoke_share_not_found(client: TestClient):
    resp = client.delete(f"/media/v1/shares/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_revoke_share_forbidden_non_owner(
    client: TestClient, session: Session, superuser
):
    obj = _make_object(session, superuser.id)
    share = _make_share(session, media_object_id=obj.id, owner_id=superuser.id)
    resp = client.delete(f"/media/v1/shares/{share.id}")
    assert resp.status_code == 403


def test_revoke_share_superuser_any_owner(
    superuser_client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    share = _make_share(session, media_object_id=obj.id, owner_id=current_user.id)
    resp = superuser_client.delete(f"/media/v1/shares/{share.id}")
    assert resp.status_code == 204


# ── GET /v1/shares/{token} (public resolve) ───────────────────────────────────


def test_resolve_happy(
    client: TestClient, mock_storage, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    share = _make_share(session, media_object_id=obj.id, owner_id=current_user.id)
    mock_storage.presigned_get_object.return_value = "https://minio/download"
    resp = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://minio/download"
    session.refresh(share)
    assert share.uses == 1


def test_resolve_under_max_uses(
    client: TestClient, mock_storage, session: Session, current_user
):
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session, media_object_id=obj.id, owner_id=current_user.id, max_uses=2, uses=0
    )
    mock_storage.presigned_get_object.return_value = "https://minio/download"
    resp = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resp.status_code == 200


def test_resolve_max_uses_exhausted(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session, media_object_id=obj.id, owner_id=current_user.id, max_uses=1, uses=1
    )
    resp = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resp.status_code == 403


def test_resolve_lost_race_returns_403(
    client: TestClient, mock_storage, session: Session, current_user, monkeypatch
):
    # A link that passes the read-time check but whose last use is consumed by a
    # concurrent resolve (modelled here by ``_consume_use`` returning ``False``)
    # must be rejected with 403 rather than handing out a presigned URL.
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session, media_object_id=obj.id, owner_id=current_user.id, max_uses=1, uses=0
    )
    monkeypatch.setattr(
        SharesController, "_consume_use", staticmethod(lambda *_a, **_kw: False)
    )
    resp = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resp.status_code == 403
    mock_storage.presigned_get_object.assert_not_called()


def test_resolve_expired(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session,
        media_object_id=obj.id,
        owner_id=current_user.id,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    resp = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resp.status_code == 403


def test_resolve_revoked(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session, media_object_id=obj.id, owner_id=current_user.id, revoked=True
    )
    resp = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resp.status_code == 403


def test_resolve_scan_not_clean(client: TestClient, session: Session, current_user):
    obj = _make_object(session, current_user.id, scan_status=ScanStatus.PENDING)
    share = _make_share(session, media_object_id=obj.id, owner_id=current_user.id)
    resp = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resp.status_code == 409


def test_resolve_object_soft_deleted(
    client: TestClient, session: Session, current_user
):
    obj = _make_object(session, current_user.id, deleted=True)
    share = _make_share(session, media_object_id=obj.id, owner_id=current_user.id)
    resp = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resp.status_code == 404


def test_resolve_object_missing(client: TestClient, session: Session, current_user):
    # Share row points at an object that does not exist (FK unenforced here).
    share = _make_share(session, media_object_id=uuid.uuid4(), owner_id=current_user.id)
    resp = client.get(f"/media/v1/shares/{_sign(share.id)}")
    assert resp.status_code == 404


def test_resolve_unknown_token(client: TestClient):
    # Valid signature, but no row for that id.
    resp = client.get(f"/media/v1/shares/{_sign(uuid.uuid4())}")
    assert resp.status_code == 404


@pytest.mark.parametrize("token", ["nodot", "a.b.c", "zz.deadbeef"])
def test_resolve_malformed_token(client: TestClient, token: str):
    resp = client.get(f"/media/v1/shares/{token}")
    assert resp.status_code == 404


def test_resolve_bad_signature(client: TestClient):
    token = f"{uuid.uuid4().hex}.not-the-real-signature"
    resp = client.get(f"/media/v1/shares/{token}")
    assert resp.status_code == 404


# ── ON DELETE CASCADE (hard-purge of parent object) ──────────────────────────


def test_hard_purge_cascades_share_tokens(session: Session, mock_storage, current_user):
    # FK cascade is only enforced under SQLite with this pragma enabled.
    session.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
    obj = _make_object(
        session,
        current_user.id,
        status=MediaObjectStatus.DELETED,
    )
    obj.deleted_at = datetime.now(timezone.utc) - timedelta(days=60)
    session.add(obj)
    session.commit()
    share = _make_share(session, media_object_id=obj.id, owner_id=current_user.id)
    obj_id, share_id = obj.id, share.id

    MaintenanceController.hard_purge_expired(
        session=session,
        storage=mock_storage,
        older_than=timedelta(days=30),
        limit=10,
    )

    assert session.get(MediaObject, obj_id) is None
    remaining = session.exec(
        select(ShareToken).where(ShareToken.id == share_id)  # type: ignore[arg-type]
    ).all()
    assert remaining == []


# ── unit: helpers ─────────────────────────────────────────────────────────────


def test_as_aware_passes_through_aware():
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _as_aware(aware) is aware


def test_as_aware_coerces_naive_to_utc():
    naive = datetime(2026, 1, 1)
    coerced = _as_aware(naive)
    assert coerced.tzinfo is timezone.utc


# ── unit: atomic use consumption (6.x.4) ──────────────────────────────────────


def test_consume_use_concurrent_single_winner(session: Session, current_user):
    # The race fixed by 6.x.4: two callers each run the atomic conditional
    # UPDATE against a ``max_uses=1`` link. Because each call is a single
    # statement, exactly one wins the use regardless of interleaving — never
    # both — and ``uses`` never overshoots ``max_uses``.
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session, media_object_id=obj.id, owner_id=current_user.id, max_uses=1, uses=0
    )
    first = SharesController._consume_use(session, share.id)
    second = SharesController._consume_use(session, share.id)
    assert [first, second] == [True, False]
    session.refresh(share)
    assert share.uses == 1


def test_consume_use_unlimited_always_succeeds(session: Session, current_user):
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session, media_object_id=obj.id, owner_id=current_user.id, max_uses=None
    )
    assert SharesController._consume_use(session, share.id) is True
    assert SharesController._consume_use(session, share.id) is True
    session.refresh(share)
    assert share.uses == 2


def test_consume_use_rejects_revoked(session: Session, current_user):
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session, media_object_id=obj.id, owner_id=current_user.id, revoked=True
    )
    assert SharesController._consume_use(session, share.id) is False
    session.refresh(share)
    assert share.uses == 0


def test_consume_use_rejects_expired(session: Session, current_user):
    obj = _make_object(session, current_user.id)
    share = _make_share(
        session,
        media_object_id=obj.id,
        owner_id=current_user.id,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    assert SharesController._consume_use(session, share.id) is False
    session.refresh(share)
    assert share.uses == 0


def test_create_controller_uses_explicit_expiry(session: Session, current_user):
    obj = _make_object(session, current_user.id)
    out = SharesController.create(
        session=session,
        current_user=current_user,
        object_id=obj.id,
        body=ShareTokenCreate(expires_in=120, max_uses=None),
    )
    delta = _as_aware(out.expires_at) - datetime.now(timezone.utc)
    assert timedelta(seconds=90) < delta < timedelta(seconds=121)
