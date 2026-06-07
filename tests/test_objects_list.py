"""Tests for GET /media/v1/objects (listing, filtering, cursor pagination)."""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_object(
    session: Session,
    owner_id: uuid.UUID,
    *,
    category: MediaCategory = MediaCategory.DOCUMENT,
    visibility: MediaVisibility = MediaVisibility.PRIVATE,
    status: MediaObjectStatus = MediaObjectStatus.UPLOADED,
    mime_type: str = "application/pdf",
    size_bytes: int = 1024,
    filename: str = "file.pdf",
    created_at: datetime | None = None,
    deleted: bool = False,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category=category,
        visibility=visibility,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/{category}/{oid}/original/{filename}",
        original_filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        status=status,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )
    if created_at is not None:
        obj.created_at = created_at
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


# ── empty / populated ─────────────────────────────────────────────────────────


def test_list_empty(client: TestClient):
    resp = client.get("/media/v1/objects")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "next_cursor": None, "count": 0}


def test_list_returns_owned_objects(client: TestClient, session: Session, current_user):
    _make_object(session, current_user.id)
    _make_object(session, current_user.id)
    resp = client.get("/media/v1/objects")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["next_cursor"] is None


def test_list_excludes_other_owners(
    client: TestClient, session: Session, current_user, superuser
):
    _make_object(session, current_user.id)
    _make_object(session, superuser.id)
    resp = client.get("/media/v1/objects")
    assert resp.json()["count"] == 1


def test_list_excludes_soft_deleted(client: TestClient, session: Session, current_user):
    _make_object(session, current_user.id)
    _make_object(session, current_user.id, deleted=True)
    resp = client.get("/media/v1/objects")
    assert resp.json()["count"] == 1


# ── pagination ────────────────────────────────────────────────────────────────


def test_list_pagination_cursor_round_trip(
    client: TestClient, session: Session, current_user
):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        _make_object(session, current_user.id, created_at=base + timedelta(minutes=i))

    first = client.get("/media/v1/objects?limit=2&sort_by=created_at&order=desc")
    assert first.status_code == 200
    page1 = first.json()
    assert page1["count"] == 2
    assert page1["next_cursor"] is not None

    second = client.get(f"/media/v1/objects?limit=2&cursor={page1['next_cursor']}")
    page2 = second.json()
    assert page2["count"] == 2

    third = client.get(f"/media/v1/objects?limit=2&cursor={page2['next_cursor']}")
    page3 = third.json()
    assert page3["count"] == 1
    assert page3["next_cursor"] is None

    seen = {o["id"] for o in page1["items"] + page2["items"] + page3["items"]}
    assert len(seen) == 5


def test_list_invalid_cursor_returns_400(client: TestClient):
    resp = client.get("/media/v1/objects?cursor=not-a-valid-cursor")
    assert resp.status_code == 400


def test_list_limit_validation(client: TestClient):
    assert client.get("/media/v1/objects?limit=0").status_code == 422
    assert client.get("/media/v1/objects?limit=101").status_code == 422


# ── filters ───────────────────────────────────────────────────────────────────


def test_list_filter_category(client: TestClient, session: Session, current_user):
    _make_object(session, current_user.id, category=MediaCategory.DOCUMENT)
    _make_object(session, current_user.id, category=MediaCategory.AVATAR)
    resp = client.get("/media/v1/objects?category=avatar")
    assert resp.json()["count"] == 1
    assert resp.json()["items"][0]["category"] == "avatar"


def test_list_filter_visibility(client: TestClient, session: Session, current_user):
    _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    _make_object(session, current_user.id, visibility=MediaVisibility.PUBLIC)
    resp = client.get("/media/v1/objects?visibility=public")
    assert resp.json()["count"] == 1


def test_list_filter_status(client: TestClient, session: Session, current_user):
    _make_object(session, current_user.id, status=MediaObjectStatus.UPLOADED)
    _make_object(session, current_user.id, status=MediaObjectStatus.READY)
    resp = client.get("/media/v1/objects?status=ready")
    assert resp.json()["count"] == 1


def test_list_filter_mime_prefix(client: TestClient, session: Session, current_user):
    _make_object(session, current_user.id, mime_type="image/png")
    _make_object(session, current_user.id, mime_type="application/pdf")
    resp = client.get("/media/v1/objects?mime_prefix=image/")
    assert resp.json()["count"] == 1
    assert resp.json()["items"][0]["mime_type"] == "image/png"


def test_list_filter_created_range(client: TestClient, session: Session, current_user):
    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _make_object(session, current_user.id, created_at=old)
    _make_object(session, current_user.id, created_at=new)
    resp = client.get("/media/v1/objects?created_from=2026-01-01T00:00:00")
    assert resp.json()["count"] == 1
    resp = client.get("/media/v1/objects?created_to=2025-06-01T00:00:00")
    assert resp.json()["count"] == 1


def test_list_filter_q_filename(client: TestClient, session: Session, current_user):
    _make_object(session, current_user.id, filename="invoice.pdf")
    _make_object(session, current_user.id, filename="photo.png")
    resp = client.get("/media/v1/objects?q=invoice")
    assert resp.json()["count"] == 1


# ── sorting ───────────────────────────────────────────────────────────────────


def test_list_sort_by_size_asc(client: TestClient, session: Session, current_user):
    _make_object(session, current_user.id, size_bytes=300)
    _make_object(session, current_user.id, size_bytes=100)
    _make_object(session, current_user.id, size_bytes=200)
    resp = client.get("/media/v1/objects?sort_by=size_bytes&order=asc")
    sizes = [o["size_bytes"] for o in resp.json()["items"]]
    assert sizes == [100, 200, 300]


def test_list_sort_by_size_desc_with_cursor(
    client: TestClient, session: Session, current_user
):
    for size in (100, 200, 300):
        _make_object(session, current_user.id, size_bytes=size)
    first = client.get("/media/v1/objects?sort_by=size_bytes&order=desc&limit=1")
    assert first.json()["items"][0]["size_bytes"] == 300
    cursor = first.json()["next_cursor"]
    second = client.get(
        f"/media/v1/objects?sort_by=size_bytes&order=desc&limit=1&cursor={cursor}"
    )
    assert second.json()["items"][0]["size_bytes"] == 200


def test_list_sort_by_size_asc_with_cursor(
    client: TestClient, session: Session, current_user
):
    for size in (100, 200, 300):
        _make_object(session, current_user.id, size_bytes=size)
    first = client.get("/media/v1/objects?sort_by=size_bytes&order=asc&limit=1")
    assert first.json()["items"][0]["size_bytes"] == 100
    cursor = first.json()["next_cursor"]
    second = client.get(
        f"/media/v1/objects?sort_by=size_bytes&order=asc&limit=1&cursor={cursor}"
    )
    assert second.json()["items"][0]["size_bytes"] == 200


# ── superuser scoping ─────────────────────────────────────────────────────────


def test_list_superuser_sees_all(
    superuser_client: TestClient, session: Session, current_user, superuser
):
    _make_object(session, current_user.id)
    _make_object(session, superuser.id)
    resp = superuser_client.get("/media/v1/objects")
    assert resp.json()["count"] == 2


def test_list_superuser_owner_filter(
    superuser_client: TestClient, session: Session, current_user, superuser
):
    _make_object(session, current_user.id)
    _make_object(session, superuser.id)
    resp = superuser_client.get(f"/media/v1/objects?owner_user_id={current_user.id}")
    assert resp.json()["count"] == 1


def test_list_superuser_include_deleted(
    superuser_client: TestClient, session: Session, current_user
):
    _make_object(session, current_user.id)
    _make_object(session, current_user.id, deleted=True)
    default = superuser_client.get("/media/v1/objects")
    assert default.json()["count"] == 1
    with_deleted = superuser_client.get("/media/v1/objects?include_deleted=true")
    assert with_deleted.json()["count"] == 2


# ── rate limit ────────────────────────────────────────────────────────────────


def test_list_rate_limited(client: TestClient, mock_redis):
    mock_redis.incr.return_value = 121
    resp = client.get("/media/v1/objects")
    assert resp.status_code == 429
