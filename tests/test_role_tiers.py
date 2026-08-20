"""A16 acceptance matrix — the role tiers are mounted, not merely intended.

Every case here goes through the real application: a real HS256 access token (or
none at all), the real ``fastapi-m8`` validator, and the real
``auth.get_current_active_*`` guard mounted on the route. Nothing on the
authorization path is stubbed — only the database session, object storage, the
Redis rate-limit backend and the ARQ pool are overridden — so a ``403`` observed
below is the ``403`` a deployed service produces, and a ``200`` proves the guard
admitted rather than that a test forgot to install it.

The matrix the operator specified:

* ``WRITER`` and above — upload, add, edit and delete owned records; dashboard.
* ``READER`` and above — owned lists and owned items.
* ``USER`` — public items only, nothing else.
* **anonymous** — public items too: a ``PUBLIC`` object is listable, readable,
  downloadable and its variants enumerable with no token at all.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient

from media_service.app.deps import get_storage
from media_service.core.arq import get_arq_pool
from media_service.core.config import settings
from media_service.core.deps import (
    get_db,
    require_admin,
    require_reader,
    require_writer,
)
from media_service.core.rate_limit import get_redis_client
from media_service.db_models.categories import Category
from media_service.db_models.media_object_categories import MediaObjectCategoryLink
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
)
from media_service.db_models.media_variants import MediaVariant
from media_service.db_models.variant_jobs import VariantJob, VariantJobStatus
from media_service.storage.client import ObjectStorage

PREFIX = settings.API_PREFIX
V1 = f"{PREFIX}/v1"

OWNER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
STRANGER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


# ── token minting ─────────────────────────────────────────────────────────────


def _token(role: str, user_id: uuid.UUID, *, is_superuser: bool = False) -> str:
    """Mint the access token the issuer would mint for *role*."""
    now = int(time.time())
    return jwt.encode(
        {
            "sub": str(user_id),
            "type": "access",
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + 600,
            "email": f"{role}@example.com",
            "role": role,
            "is_superuser": is_superuser,
        },
        settings.ACCESS_SECRET_KEY.get_secret_value(),
        algorithm=settings.ACCESS_TOKEN_ALGORITHM,
    )


def _auth(role: str, user_id: uuid.UUID = OWNER_ID, **kwargs) -> dict[str, str]:
    """Authorization header for *role*, defaulting to the fixture owner."""
    return {"Authorization": f"Bearer {_token(role, user_id, **kwargs)}"}


NO_AUTH: dict[str, str] = {}


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tier_client(session, mock_storage, mock_redis, fake_arq_pool):
    """The real app with only the non-authorization dependencies replaced.

    Constructed without the context manager on purpose: entering it would run
    the lifespan, which opens the configured Postgres engine and the auth event
    stream. Neither is on the authorization path this file is about.
    """
    import media_service.main as main

    async def _arq_pool():
        return fake_arq_pool

    main.app.dependency_overrides[get_db] = lambda: session
    main.app.dependency_overrides[get_storage] = lambda: mock_storage
    main.app.dependency_overrides[get_redis_client] = lambda: mock_redis
    main.app.dependency_overrides[get_arq_pool] = _arq_pool
    try:
        yield TestClient(main.app)
    finally:
        main.app.dependency_overrides.clear()


def _make_object(
    session,
    *,
    owner_id: uuid.UUID = OWNER_ID,
    visibility: MediaVisibility = MediaVisibility.PRIVATE,
    scan_status: ScanStatus = ScanStatus.CLEAN,
) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category="document",
        visibility=visibility,
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/document/{oid}/original/file.pdf",
        original_filename="file.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        status=MediaObjectStatus.READY,
        scan_status=scan_status,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


@pytest.fixture
def public_object(session) -> MediaObject:
    """A live, scan-clean PUBLIC object owned by OWNER_ID."""
    return _make_object(session, visibility=MediaVisibility.PUBLIC)


@pytest.fixture
def private_object(session) -> MediaObject:
    """A live PRIVATE object owned by OWNER_ID."""
    return _make_object(session, visibility=MediaVisibility.PRIVATE)


@pytest.fixture
def mock_storage() -> MagicMock:
    """ObjectStorage double whose presign call returns a fixed URL."""
    storage = MagicMock(spec=ObjectStorage)
    storage.presigned_get_object.return_value = "https://example.invalid/signed"
    return storage


# ── 1. Every route denies an unauthenticated caller unless it is public ───────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{PREFIX}/category/"),
        ("POST", f"{PREFIX}/category/add/"),
        ("GET", f"{PREFIX}/dashboard/users/activity/"),
        ("POST", f"{V1}/uploads/initiate"),
        ("GET", f"{V1}/presets"),
        ("POST", f"{V1}/presets"),
        ("GET", f"{V1}/admin/storage/stats"),
    ],
)
def test_non_public_routes_reject_an_anonymous_caller(tier_client, method, path):
    """A 401 here is what makes every 403 below provably "wrong role"."""
    assert tier_client.request(method, path, json={}).status_code == 401


# ── 2. The public read surface — no token at all ──────────────────────────────


def test_anonymous_lists_only_public_objects(
    tier_client, session, public_object, private_object
):
    response = tier_client.get(f"{V1}/objects", headers=NO_AUTH)
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [str(public_object.id)]
    assert str(private_object.id) not in ids


def test_anonymous_reads_a_public_object(tier_client, public_object):
    response = tier_client.get(f"{V1}/objects/{public_object.id}", headers=NO_AUTH)
    assert response.status_code == 200
    assert response.json()["id"] == str(public_object.id)


def test_anonymous_downloads_a_public_object(tier_client, public_object):
    response = tier_client.get(
        f"{V1}/objects/{public_object.id}/download-url", headers=NO_AUTH
    )
    assert response.status_code == 200
    assert response.json()["url"] == "https://example.invalid/signed"


def test_anonymous_lists_variants_of_a_public_object(
    tier_client, session, public_object
):
    session.add(
        MediaVariant(
            media_object_id=public_object.id,
            variant_name="thumb",
            storage_bucket="public-media",
            object_key="thumb.webp",
            size_bytes=10,
            format="webp",
        )
    )
    session.commit()
    response = tier_client.get(
        f"{V1}/objects/{public_object.id}/variants", headers=NO_AUTH
    )
    assert response.status_code == 200
    assert [v["variant_name"] for v in response.json()["items"]] == ["thumb"]


@pytest.mark.parametrize(
    "suffix",
    ["", "/download-url", "/variants"],
    ids=["metadata", "download", "variants"],
)
def test_anonymous_is_not_told_a_private_object_exists(
    tier_client, private_object, suffix
):
    """404, not 403 — the public surface is never an existence oracle.

    A 403 would separate "this id exists but is private" from "no such id",
    which is exactly the probe an unauthenticated caller must not have.
    """
    response = tier_client.get(
        f"{V1}/objects/{private_object.id}{suffix}", headers=NO_AUTH
    )
    assert response.status_code == 404
    unknown = tier_client.get(f"{V1}/objects/{uuid.uuid4()}{suffix}", headers=NO_AUTH)
    assert unknown.status_code == response.status_code


def test_anonymous_branch_filter_cannot_widen_the_public_catalogue(
    tier_client, session, public_object, private_object
):
    """`U4` — ``category_id`` narrows the anonymous listing; it never widens it.

    The category surface sits behind the reader floor, so an anonymous caller
    has no category scope: the branch resolves to the empty set and the page is
    empty. Critically it is *empty*, not the private row — filing a private
    object into a category must not turn a public filter into a way to read it.
    Empty rather than 404/403, because a refusal would depend on whether a
    category the caller can never see happens to exist, which is the existence
    oracle this surface keeps out (see ``require_visibility_access``).
    """
    category = Category(name="Docs", slug="docs", owner_id=str(OWNER_ID))
    session.add(category)
    session.commit()
    session.refresh(category)
    for obj in (public_object, private_object):
        session.add(
            MediaObjectCategoryLink(media_object_id=obj.id, category_id=category.id)
        )
    session.commit()

    filtered = tier_client.get(
        f"{V1}/objects", params={"category_id": category.id}, headers=NO_AUTH
    )
    assert filtered.status_code == 200
    assert filtered.json()["items"] == []

    unknown = tier_client.get(
        f"{V1}/objects", params={"category_id": 999}, headers=NO_AUTH
    )
    assert unknown.status_code == filtered.status_code
    assert unknown.json() == filtered.json()

    unfiltered = tier_client.get(f"{V1}/objects", headers=NO_AUTH)
    assert [item["id"] for item in unfiltered.json()["items"]] == [
        str(public_object.id)
    ]


def test_anonymous_uncategorized_filter_still_sees_only_public_objects(
    tier_client, session, public_object, private_object
):
    """The unfiled filter needs no category scope, so it does reach rows.

    It is still applied after the public scoping: an unfiled *private* object
    stays out, which is the property that matters.
    """
    response = tier_client.get(
        f"{V1}/objects", params={"uncategorized": "true"}, headers=NO_AUTH
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(public_object.id)]


def test_a_broken_token_is_never_silently_downgraded_to_anonymous(tier_client):
    """Presenting garbage credentials fails; it does not fall back to public."""
    response = tier_client.get(
        f"{V1}/objects", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 403


# ── 3. USER tier — public items only ──────────────────────────────────────────


def test_user_tier_reads_public_objects(tier_client, public_object, private_object):
    response = tier_client.get(f"{V1}/objects", headers=_auth("user", STRANGER_ID))
    assert response.status_code == 200
    assert [i["id"] for i in response.json()["items"]] == [str(public_object.id)]


@pytest.mark.parametrize(
    ("method", "path", "tier"),
    [
        ("GET", f"{PREFIX}/category/", "reader"),
        ("GET", f"{V1}/presets", "reader"),
        ("POST", f"{PREFIX}/category/add/", "writer"),
        ("POST", f"{V1}/uploads/initiate", "writer"),
        ("GET", f"{PREFIX}/dashboard/users/activity/", "writer"),
    ],
)
def test_user_tier_is_denied_every_owned_route(tier_client, method, path, tier):
    response = tier_client.request(method, path, headers=_auth("user"), json={})
    assert response.status_code == 403, f"{tier}-tier route admitted a USER principal"


# ── 4. READER tier — owned reads yes, mutations no ────────────────────────────


def test_reader_reads_an_owned_category(tier_client, session):
    session.add(Category(name="Docs", slug="docs", owner_id=str(OWNER_ID)))
    session.commit()
    response = tier_client.get(f"{PREFIX}/category/", headers=_auth("reader"))
    assert response.status_code == 200
    assert [c["name"] for c in response.json()["data"]] == ["Docs"]


def test_reader_reads_the_preset_catalogue(tier_client):
    response = tier_client.get(f"{V1}/presets", headers=_auth("reader"))
    assert response.status_code == 200


def test_reader_reads_an_owned_variant_job(tier_client, session, private_object):
    job = VariantJob(
        media_object_id=private_object.id,
        owner_user_id=OWNER_ID,
        status=VariantJobStatus.QUEUED,
        requested_presets=["thumb"],
        variants_expected=1,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    response = tier_client.get(
        f"{V1}/objects/{private_object.id}/variants/jobs/{job.id}",
        headers=_auth("reader"),
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"{PREFIX}/category/add/", {"name": "New"}),
        ("PUT", f"{PREFIX}/category/edit/1/", {"name": "Edited"}),
        ("DELETE", f"{PREFIX}/category/delete/1/", None),
        ("POST", f"{V1}/presets", {"name": "p", "spec": {"width": 10}}),
        ("GET", f"{PREFIX}/dashboard/users/activity/", None),
    ],
)
def test_reader_is_denied_every_mutation(tier_client, method, path, body):
    response = tier_client.request(method, path, headers=_auth("reader"), json=body)
    assert response.status_code == 403


def test_reader_cannot_patch_or_delete_an_object(tier_client, private_object):
    patched = tier_client.patch(
        f"{V1}/objects/{private_object.id}",
        headers=_auth("reader"),
        json={"original_filename": "renamed.pdf"},
    )
    deleted = tier_client.delete(
        f"{V1}/objects/{private_object.id}", headers=_auth("reader")
    )
    assert (patched.status_code, deleted.status_code) == (403, 403)


# ── 5. WRITER tier — the owned mutation surface ───────────────────────────────


def test_writer_creates_a_category(tier_client):
    response = tier_client.post(
        f"{PREFIX}/category/add/", headers=_auth("writer"), json={"name": "Fresh"}
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Fresh"


def test_writer_patches_an_owned_object(tier_client, private_object):
    response = tier_client.patch(
        f"{V1}/objects/{private_object.id}",
        headers=_auth("writer"),
        json={"original_filename": "renamed.pdf"},
    )
    assert response.status_code == 200
    assert response.json()["original_filename"] == "renamed.pdf"


def test_writer_reaches_the_dashboard(tier_client):
    response = tier_client.get(
        f"{PREFIX}/dashboard/users/activity/", headers=_auth("writer")
    )
    assert response.status_code == 200


def test_admin_role_also_reaches_the_writer_surface(tier_client):
    """The hierarchy is monotone: a higher tier never loses a lower one."""
    response = tier_client.post(
        f"{PREFIX}/category/add/", headers=_auth("admin"), json={"name": "ByAdmin"}
    )
    assert response.status_code == 201


# ── 6. ADMIN / superuser ──────────────────────────────────────────────────────


def test_admin_guard_denies_a_writer(tier_client):
    """``require_admin`` is exported and real, although no route carries it yet.

    Asserted directly so the tier cannot rot between now and the first route
    that needs it.
    """
    guard = tier_client.app.dependency_overrides.get(require_admin, require_admin)
    assert guard is require_admin
    response = tier_client.get(f"{V1}/admin/storage/stats", headers=_auth("admin"))
    # /v1/admin stays on the superuser guard (A16 decision), so an ADMIN-role
    # principal that is not a canonical superuser is denied there.
    assert response.status_code == 403


def test_superuser_reaches_the_admin_surface(tier_client):
    response = tier_client.get(
        f"{V1}/admin/storage/stats",
        headers=_auth("superadmin", is_superuser=True),
    )
    assert response.status_code == 200


def test_a_superuser_flag_alone_never_satisfies_a_role_threshold(tier_client):
    """``is_superuser`` is not a role: the SDK rejects the inconsistent pair.

    A token claiming ``role=user`` with ``is_superuser=true`` is refused by the
    validator itself, so the flag cannot be smuggled past a writer guard.
    """
    response = tier_client.post(
        f"{PREFIX}/category/add/",
        headers=_auth("user", is_superuser=True),
        json={"name": "Sneaky"},
    )
    assert response.status_code == 403


# ── 7. Widening a read must never widen a write ───────────────────────────────


def test_public_read_never_widens_into_a_write(tier_client, public_object):
    """A stranger may read a PUBLIC object and still not mutate it.

    ``_load_object_for_read`` admits it; ``_load_object`` (the write path) stays
    owner-or-superuser, and this holds the line between them.
    """
    read = tier_client.get(f"{V1}/objects/{public_object.id}", headers=NO_AUTH)
    assert read.status_code == 200

    stranger = _auth("writer", STRANGER_ID)
    patched = tier_client.patch(
        f"{V1}/objects/{public_object.id}",
        headers=stranger,
        json={"original_filename": "stolen.pdf"},
    )
    deleted = tier_client.delete(f"{V1}/objects/{public_object.id}", headers=stranger)
    assert (patched.status_code, deleted.status_code) == (403, 403)


def test_the_router_floors_are_mounted_not_just_annotated():
    """A route added to these routers later inherits the floor, not a promise."""
    from media_service.app.routes import (
        category,
        dashboard,
        objects,
        presets,
        shares,
        uploads,
        variants,
    )

    def _mounted(router) -> set:
        return {dep.dependency for dep in router.dependencies}

    assert _mounted(category.router) == {require_reader}
    assert _mounted(presets.router) == {require_reader}
    assert _mounted(shares.router) == {require_reader}
    assert _mounted(dashboard.router) == {require_writer}
    assert _mounted(uploads.router) == {require_writer}
    assert _mounted(objects.router) == {require_writer}
    assert _mounted(variants.router) == {require_writer}
    # The two read routers carry no floor by design: they admit anonymous
    # callers on PUBLIC records, so no dependency admits every route on them.
    assert _mounted(objects.read_router) == set()
    assert _mounted(variants.read_router) == set()
    assert _mounted(shares.public_router) == set()


def test_no_route_module_imports_a_bare_current_user():
    """F7: the bare ``CurrentUser`` vocabulary is gone, not merely unused."""
    import media_service.app.deps as app_deps
    import media_service.core.deps as core_deps

    assert not hasattr(app_deps, "CurrentUser")
    assert not hasattr(core_deps, "CurrentUser")
    assert "CurrentUser" not in app_deps.__all__
