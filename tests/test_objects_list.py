"""Tests for GET /media/v1/objects (listing, filtering, cursor pagination).

The last section covers the user-category branch filters (`U4`): ``category_id``
with ``include_descendants``, and the ``uncategorized`` pseudo-node. They are
the one group of filters that resolves through the caller's *category* scope
rather than a column, so they are asserted to narrow the listing and never to
widen it.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session

from media_service.db_models.categories import Category
from media_service.db_models.media_object_categories import MediaObjectCategoryLink
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

    second = client.get(
        "/media/v1/objects"
        f"?limit=2&sort_by=created_at&order=desc&cursor={page1['next_cursor']}"
    )
    page2 = second.json()
    assert page2["count"] == 2

    third = client.get(
        "/media/v1/objects"
        f"?limit=2&sort_by=created_at&order=desc&cursor={page2['next_cursor']}"
    )
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


def test_list_filter_q_escapes_like_wildcards(
    client: TestClient, session: Session, current_user
):
    # "%" / "_" in the search term must match literally, not as LIKE wildcards.
    _make_object(session, current_user.id, filename="50%_off.pdf")
    _make_object(session, current_user.id, filename="photo.png")
    # An unescaped "%" would match every filename; escaped, it only hits the literal.
    resp = client.get("/media/v1/objects?q=50%25")  # %25 == "%"
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["original_filename"] == "50%_off.pdf"


# ── sorting ───────────────────────────────────────────────────────────────────


def test_list_defaults_to_filename_sort(
    client: TestClient, session: Session, current_user
):
    _make_object(session, current_user.id, filename="zeta.pdf")
    _make_object(session, current_user.id, filename="alpha.pdf")
    _make_object(session, current_user.id, filename="middle.pdf")
    resp = client.get("/media/v1/objects")
    filenames = [o["original_filename"] for o in resp.json()["items"]]
    assert filenames == ["alpha.pdf", "middle.pdf", "zeta.pdf"]


def test_list_sort_by_filename_asc_with_cursor(
    client: TestClient, session: Session, current_user
):
    for filename in ("b.pdf", "a.pdf", "c.pdf"):
        _make_object(session, current_user.id, filename=filename)
    first = client.get("/media/v1/objects?sort_by=original_filename&order=asc&limit=1")
    assert first.json()["items"][0]["original_filename"] == "a.pdf"
    cursor = first.json()["next_cursor"]
    second = client.get(
        f"/media/v1/objects?sort_by=original_filename&order=asc&limit=1&cursor={cursor}"
    )
    assert second.json()["items"][0]["original_filename"] == "b.pdf"


def test_list_sort_by_filename_cursor_with_null_filename(
    client: TestClient, session: Session, current_user
):
    """Cursor value for None original_filename serialises to "" (covers _cursor_sort_value null branch)."""
    import uuid

    null_obj = MediaObject(
        id=uuid.uuid4(),
        owner_user_id=current_user.id,
        category=MediaCategory.DOCUMENT,
        visibility=MediaVisibility.PRIVATE,
        storage_bucket="private-media",
        object_key=f"users/{current_user.id}/doc/null-file",
        original_filename=None,
        mime_type="application/octet-stream",
        size_bytes=1,
        status=MediaObjectStatus.UPLOADED,
    )
    session.add(null_obj)
    session.commit()
    _make_object(session, current_user.id, filename="z.pdf")
    # The null-filename object sorts first (coalesce → ""); verify cursor round-trip works.
    first = client.get("/media/v1/objects?sort_by=original_filename&order=asc&limit=1")
    assert first.status_code == 200
    assert first.json()["next_cursor"] is not None
    cursor = first.json()["next_cursor"]
    second = client.get(
        f"/media/v1/objects?sort_by=original_filename&order=asc&limit=1&cursor={cursor}"
    )
    assert second.status_code == 200


def test_list_sort_by_category_and_status(
    client: TestClient, session: Session, current_user
):
    _make_object(
        session,
        current_user.id,
        category=MediaCategory.RECEIPT,
        status=MediaObjectStatus.READY,
    )
    _make_object(
        session,
        current_user.id,
        category=MediaCategory.AVATAR,
        status=MediaObjectStatus.FAILED,
    )
    by_category = client.get("/media/v1/objects?sort_by=category&order=asc")
    assert [o["category"] for o in by_category.json()["items"]] == [
        "avatar",
        "receipt",
    ]

    by_status = client.get("/media/v1/objects?sort_by=status&order=asc")
    assert [o["status"] for o in by_status.json()["items"]] == [
        "failed",
        "ready",
    ]


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


# ── user-category branch filter (`U4`) ────────────────────────────────────────


def _make_category(
    session: Session,
    owner_id: uuid.UUID,
    name: str,
    *,
    parent_id: int | None = None,
) -> Category:
    """Insert a user category owned by the given user."""
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


def _file_into(session: Session, obj: MediaObject, *categories: Category) -> None:
    """File ``obj`` into every given user category."""
    for category in categories:
        session.add(
            MediaObjectCategoryLink(media_object_id=obj.id, category_id=category.id)
        )
    session.commit()


def _ids(resp) -> set[str]:
    """The object ids a listing response returned."""
    return {item["id"] for item in resp.json()["items"]}


def test_list_filter_category_id_includes_descendants_by_default(
    client: TestClient, session: Session, current_user
):
    """A branch returns its own media *and* every descendant's, in one page."""
    root = _make_category(session, current_user.id, "Documents")
    child = _make_category(session, current_user.id, "Invoices", parent_id=root.id)
    grandchild = _make_category(session, current_user.id, "Y2026", parent_id=child.id)
    on_root = _make_object(session, current_user.id, filename="root.pdf")
    on_child = _make_object(session, current_user.id, filename="child.pdf")
    on_grandchild = _make_object(session, current_user.id, filename="deep.pdf")
    unfiled = _make_object(session, current_user.id, filename="loose.pdf")
    _file_into(session, on_root, root)
    _file_into(session, on_child, child)
    _file_into(session, on_grandchild, grandchild)

    resp = client.get(f"/media/v1/objects?category_id={root.id}")
    assert resp.status_code == 200
    assert _ids(resp) == {str(on_root.id), str(on_child.id), str(on_grandchild.id)}
    assert str(unfiled.id) not in _ids(resp)
    assert resp.json()["count"] == 3


def test_list_filter_category_id_without_descendants_is_direct_only(
    client: TestClient, session: Session, current_user
):
    root = _make_category(session, current_user.id, "Documents")
    child = _make_category(session, current_user.id, "Invoices", parent_id=root.id)
    on_root = _make_object(session, current_user.id, filename="root.pdf")
    on_child = _make_object(session, current_user.id, filename="child.pdf")
    _file_into(session, on_root, root)
    _file_into(session, on_child, child)

    resp = client.get(
        f"/media/v1/objects?category_id={root.id}&include_descendants=false"
    )
    assert _ids(resp) == {str(on_root.id)}


def test_list_filter_category_id_returns_a_multi_filed_object_once(
    client: TestClient, session: Session, current_user
):
    """An object filed into two categories of one branch is not duplicated.

    This is what the correlated ``EXISTS`` buys over a join: a join would emit
    the row once per matching link and break both ``count`` and the cursor.
    """
    root = _make_category(session, current_user.id, "Documents")
    child = _make_category(session, current_user.id, "Invoices", parent_id=root.id)
    obj = _make_object(session, current_user.id)
    _file_into(session, obj, root, child)

    resp = client.get(f"/media/v1/objects?category_id={root.id}")
    assert resp.json()["count"] == 1
    assert _ids(resp) == {str(obj.id)}


def test_list_filter_uncategorized_returns_only_unfiled_media(
    client: TestClient, session: Session, current_user
):
    category = _make_category(session, current_user.id, "Documents")
    filed = _make_object(session, current_user.id, filename="filed.pdf")
    unfiled = _make_object(session, current_user.id, filename="loose.pdf")
    _file_into(session, filed, category)

    resp = client.get("/media/v1/objects?uncategorized=true")
    assert _ids(resp) == {str(unfiled.id)}


def test_list_filter_uncategorized_false_is_the_unfiltered_library(
    client: TestClient, session: Session, current_user
):
    """The default is off: it narrows nothing, so both rows still come back."""
    category = _make_category(session, current_user.id, "Documents")
    filed = _make_object(session, current_user.id, filename="filed.pdf")
    unfiled = _make_object(session, current_user.id, filename="loose.pdf")
    _file_into(session, filed, category)

    resp = client.get("/media/v1/objects?uncategorized=false")
    assert _ids(resp) == {str(filed.id), str(unfiled.id)}


def test_list_filter_uncategorized_with_category_id_is_422(client: TestClient):
    """Their intersection is empty by construction, so it is a bad request."""
    resp = client.get("/media/v1/objects?uncategorized=true&category_id=1")
    assert resp.status_code == 422


def test_list_filter_category_id_zero_is_rejected(client: TestClient):
    """No reserved ``category_id=0`` sentinel — ``uncategorized`` owns that case."""
    assert client.get("/media/v1/objects?category_id=0").status_code == 422


def test_list_filter_category_id_unknown_is_404(client: TestClient):
    """A typo is a refusal, not a plausible empty page."""
    resp = client.get("/media/v1/objects?category_id=999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Category not found."


def test_list_filter_another_owners_category_is_forbidden(
    client: TestClient, session: Session, superuser
):
    foreign = _make_category(session, superuser.id, "Theirs")
    resp = client.get(f"/media/v1/objects?category_id={foreign.id}")
    assert resp.status_code == 403


def test_list_filter_branch_narrows_but_never_widens(
    client: TestClient, session: Session, current_user, superuser
):
    """The filter is a ``where`` on the scoped query, so it cannot reach out.

    Another owner's object filed into a category the caller *can* see stays
    invisible: the visibility scoping runs first and the branch clause only
    ever subtracts from it.
    """
    category = _make_category(session, current_user.id, "Documents")
    mine = _make_object(session, current_user.id, filename="mine.pdf")
    theirs = _make_object(session, superuser.id, filename="theirs.pdf")
    _file_into(session, mine, category)
    _file_into(session, theirs, category)

    resp = client.get(f"/media/v1/objects?category_id={category.id}")
    assert _ids(resp) == {str(mine.id)}


def test_list_filter_branch_composes_with_the_fixed_category_enum(
    client: TestClient, session: Session, current_user
):
    """The two filters are different axes and compose — `U4`'s locked decision."""
    category = _make_category(session, current_user.id, "Documents")
    doc = _make_object(
        session, current_user.id, category=MediaCategory.DOCUMENT, filename="a.pdf"
    )
    receipt = _make_object(
        session, current_user.id, category=MediaCategory.RECEIPT, filename="b.pdf"
    )
    _file_into(session, doc, category)
    _file_into(session, receipt, category)

    resp = client.get(f"/media/v1/objects?category_id={category.id}&category=receipt")
    assert _ids(resp) == {str(receipt.id)}


def test_list_filter_branch_paginates(
    client: TestClient, session: Session, current_user
):
    """The branch filter rides the existing keyset cursor, not a second path."""
    category = _make_category(session, current_user.id, "Documents")
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        _file_into(
            session, _make_object(session, current_user.id, filename=name), category
        )

    first = client.get(f"/media/v1/objects?category_id={category.id}&limit=2").json()
    assert [i["original_filename"] for i in first["items"]] == ["a.pdf", "b.pdf"]
    assert first["next_cursor"] is not None
    cursor = first["next_cursor"]
    second = client.get(
        f"/media/v1/objects?category_id={category.id}&limit=2&cursor={cursor}"
    ).json()
    assert [i["original_filename"] for i in second["items"]] == ["c.pdf"]
    assert second["next_cursor"] is None


def test_list_filter_superuser_branch_spans_owners(
    superuser_client: TestClient, session: Session, current_user, superuser
):
    """A superuser's category scope is every row, and the branch follows it."""
    root = _make_category(session, current_user.id, "Documents")
    child = _make_category(session, superuser.id, "Theirs", parent_id=root.id)
    mine = _make_object(session, current_user.id, filename="mine.pdf")
    theirs = _make_object(session, superuser.id, filename="theirs.pdf")
    _file_into(session, mine, root)
    _file_into(session, theirs, child)

    resp = superuser_client.get(f"/media/v1/objects?category_id={root.id}")
    assert _ids(resp) == {str(mine.id), str(theirs.id)}


def test_list_filter_branch_walk_survives_a_cycle_written_to_disk(
    client: TestClient, session: Session, current_user
):
    """A loop no CRUD path can create must not hang the listing.

    ``controllers.category`` rejects cycles on write, so this one is written
    straight to the database — the same degenerate case
    ``resolve_category_paths`` is pinned against on the path side.
    """
    root = _make_category(session, current_user.id, "Documents")
    child = _make_category(session, current_user.id, "Invoices", parent_id=root.id)
    root.parent_id = child.id
    session.add(root)
    session.commit()
    filed = _make_object(session, current_user.id, filename="deep.pdf")
    _file_into(session, filed, child)

    resp = client.get(f"/media/v1/objects?category_id={root.id}")
    assert resp.status_code == 200
    assert _ids(resp) == {str(filed.id)}


# ── `categories` array on the list page (`U4`) ──────────────────────────────


def test_list_populates_the_categories_array(
    client: TestClient, session: Session, current_user
):
    """Every item's ``categories`` array carries its filing, with resolved paths."""
    root = _make_category(session, current_user.id, "Documents")
    child = _make_category(session, current_user.id, "Invoices", parent_id=root.id)
    multi = _make_object(session, current_user.id, filename="multi.pdf")
    single = _make_object(session, current_user.id, filename="single.pdf")
    unfiled = _make_object(session, current_user.id, filename="loose.pdf")
    _file_into(session, multi, root, child)
    _file_into(session, single, root)

    resp = client.get("/media/v1/objects")
    assert resp.status_code == 200
    by_id = {item["id"]: item["categories"] for item in resp.json()["items"]}

    assert {c["path"] for c in by_id[str(multi.id)]} == {
        "documents",
        "documents/invoices",
    }
    assert [c["path"] for c in by_id[str(single.id)]] == ["documents"]
    assert by_id[str(unfiled.id)] == []


def test_list_categories_query_count_does_not_grow_with_page_size(
    client: TestClient, session: Session, engine, current_user
):
    """The `categories` projection is one joined load per page, not per object.

    Filing two objects and eight objects into the same branch and comparing the
    number of SQL statements the listing issues proves the cost is flat: two
    extra queries (the link rows, then the scope for path resolution),
    regardless of how many objects are on the page.
    """

    def _query_count(fn) -> int:
        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _record)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", _record)
        return len(statements)

    root = _make_category(session, current_user.id, "Documents")
    child = _make_category(session, current_user.id, "Invoices", parent_id=root.id)

    small = [
        _make_object(session, current_user.id, filename=f"s{i}.pdf") for i in range(2)
    ]
    for obj in small:
        _file_into(session, obj, root, child)
    small_count = _query_count(lambda: client.get("/media/v1/objects"))
    assert small_count > 0

    large = [
        _make_object(session, current_user.id, filename=f"l{i}.pdf") for i in range(8)
    ]
    for obj in large:
        _file_into(session, obj, root, child)
    large_count = _query_count(lambda: client.get("/media/v1/objects?limit=20"))

    assert large_count == small_count


def test_list_categories_stays_empty_when_the_branch_filter_excludes_the_filing(
    client: TestClient, session: Session, current_user
):
    """A filed object still reports its full filing, even outside the branch filter.

    ``categories`` reflects everything the object is filed into, not just the
    branch the listing happened to be narrowed to.
    """
    root = _make_category(session, current_user.id, "Documents")
    other = _make_category(session, current_user.id, "Photos")
    obj = _make_object(session, current_user.id)
    _file_into(session, obj, root, other)

    resp = client.get(f"/media/v1/objects?category_id={root.id}")
    assert resp.status_code == 200
    (item,) = resp.json()["items"]
    assert {c["id"] for c in item["categories"]} == {root.id, other.id}
