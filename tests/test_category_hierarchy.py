"""Tests for the hierarchical, tenant-scoped, multi-assign category model (`U3`).

Covers what the schema itself guarantees — composite uniqueness, the
many-to-many link round-trip, subtree count rollup and path resolution — plus
the migration-facing assertion that every new table and column is visible to
Alembic's autogenerate target metadata. Tenant scoping of *queries*, the tree
endpoint and the CRUD guards belong to `U4`.
"""

import pathlib
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from media_service.core.category_tree import (
    build_category_tree,
    category_refs,
    count_category_nodes,
    resolve_category_paths,
)
from media_service.core.db_models import prefixed_tables
from media_service.db_models.categories import Category, CategoryTreePublic
from media_service.db_models.media_object_categories import MediaObjectCategoryLink
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObject,
    MediaObjectPublic,
    MediaObjectStatus,
    MediaVisibility,
)

OWNER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_A = uuid.UUID("22222222-2222-2222-2222-222222222222")
TENANT_B = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _category(
    session: Session,
    name: str,
    *,
    slug: str | None = None,
    parent: Category | None = None,
    tenant_id: uuid.UUID | None = TENANT_A,
    owner_id: uuid.UUID = OWNER_ID,
) -> Category:
    """Insert one category and return it refreshed."""
    cat = Category(
        name=name,
        slug=slug or name.lower().replace(" ", "-"),
        parent_id=parent.id if parent else None,
        tenant_id=tenant_id,
        owner_id=owner_id,
    )
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _media_object(session: Session, key: str) -> MediaObject:
    """Insert one ready media object and return it refreshed."""
    obj = MediaObject(
        tenant_id=TENANT_A,
        owner_user_id=OWNER_ID,
        category=MediaCategory.DOCUMENT,
        visibility=MediaVisibility.PRIVATE,
        storage_bucket="media-private",
        object_key=key,
        mime_type="application/pdf",
        size_bytes=10,
        status=MediaObjectStatus.READY,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _file_into(session: Session, obj: MediaObject, *categories: Category) -> None:
    """Link one media object into every given category."""
    for cat in categories:
        session.add(MediaObjectCategoryLink(media_object_id=obj.id, category_id=cat.id))
    session.commit()


# ── hierarchy + tenant columns ────────────────────────────────────────────────


def test_root_category_has_null_parent(session: Session):
    root = _category(session, "Documents")
    assert root.parent_id is None
    assert root.tenant_id == TENANT_A


def test_child_resolves_parent_and_children_relationships(session: Session):
    root = _category(session, "Documents")
    child = _category(session, "Invoices", parent=root)

    session.refresh(root)
    assert child.parent is not None
    assert child.parent.id == root.id
    assert [c.id for c in root.children] == [child.id]


def test_category_is_tenant_scoped_but_keeps_owner(session: Session):
    """`owner_id` survives as the untenanted fallback and audit trail."""
    untenanted = _category(session, "Solo", tenant_id=None)
    assert untenanted.tenant_id is None
    assert untenanted.owner_id == OWNER_ID


# ── composite uniqueness ──────────────────────────────────────────────────────


def test_same_slug_allowed_under_different_parents(session: Session):
    docs = _category(session, "Documents")
    assets = _category(session, "Assets")

    first = _category(session, "2026", parent=docs)
    second = _category(session, "2026", parent=assets)

    assert first.id != second.id
    assert first.slug == second.slug


def test_same_slug_allowed_in_different_tenants(session: Session):
    a = _category(session, "Documents", tenant_id=TENANT_A)
    b = _category(session, "Documents", tenant_id=TENANT_B)

    assert a.id != b.id
    assert a.slug == b.slug


def test_true_duplicate_is_rejected(session: Session):
    """Same tenant, same parent, same slug — the composite constraint bites."""
    docs = _category(session, "Documents")
    _category(session, "2026", parent=docs)

    session.add(
        Category(
            name="2026",
            slug="2026",
            parent_id=docs.id,
            tenant_id=TENANT_A,
            owner_id=OWNER_ID,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_global_slug_uniqueness_is_gone():
    """The old `uq_category_slug` no longer blocks a reused name."""
    table = SQLModel.metadata.tables[prefixed_tables("category")]
    constraint_names = {c.name for c in table.constraints}

    assert "uq_category_slug" not in constraint_names
    assert "uq_category_tenant_parent_slug" in constraint_names
    assert not table.c.slug.unique
    assert not table.c.name.unique


# ── many-to-many link ─────────────────────────────────────────────────────────


def test_object_resolves_to_multiple_categories(session: Session):
    docs = _category(session, "Documents")
    invoices = _category(session, "Invoices", parent=docs)
    obj = _media_object(session, "a.pdf")

    _file_into(session, obj, docs, invoices)

    session.refresh(obj)
    assert {c.id for c in obj.user_categories} == {docs.id, invoices.id}


def test_category_resolves_to_multiple_objects(session: Session):
    docs = _category(session, "Documents")
    first = _media_object(session, "a.pdf")
    second = _media_object(session, "b.pdf")

    _file_into(session, first, docs)
    _file_into(session, second, docs)

    session.refresh(docs)
    assert {o.id for o in docs.media_objects} == {first.id, second.id}


def test_repeated_filing_is_rejected_by_the_composite_primary_key(session: Session):
    docs = _category(session, "Documents")
    obj = _media_object(session, "a.pdf")
    _file_into(session, obj, docs)

    session.add(MediaObjectCategoryLink(media_object_id=obj.id, category_id=docs.id))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_deleting_a_category_detaches_links_and_keeps_the_media(session: Session):
    docs = _category(session, "Documents")
    obj = _media_object(session, "a.pdf")
    _file_into(session, obj, docs)

    session.refresh(docs)
    session.delete(docs)
    session.commit()

    assert session.get(MediaObject, obj.id) is not None
    assert session.exec(select(MediaObjectCategoryLink)).all() == []


# ── tree assembly and counts ──────────────────────────────────────────────────


def test_tree_nests_children_under_their_root(session: Session):
    docs = _category(session, "Documents")
    invoices = _category(session, "Invoices", parent=docs)
    y2026 = _category(session, "2026", parent=invoices)
    assets = _category(session, "Assets")

    roots = build_category_tree([docs, invoices, y2026, assets])

    assert [r.name for r in roots] == ["Assets", "Documents"]
    documents = next(r for r in roots if r.id == docs.id)
    assert [c.id for c in documents.children] == [invoices.id]
    assert [c.id for c in documents.children[0].children] == [y2026.id]
    assert count_category_nodes(roots) == 4


def test_subtree_counts_roll_up_including_the_empty_node(session: Session):
    docs = _category(session, "Documents")
    invoices = _category(session, "Invoices", parent=docs)
    y2026 = _category(session, "2026", parent=invoices)
    empty = _category(session, "Drafts", parent=docs)

    roots = build_category_tree(
        [docs, invoices, y2026, empty],
        direct_counts={docs.id: 1, invoices.id: 2, y2026.id: 3},
    )

    documents = roots[0]
    assert documents.object_count == 1
    assert documents.total_object_count == 6

    by_id = {c.id: c for c in documents.children}
    assert by_id[invoices.id].object_count == 2
    assert by_id[invoices.id].total_object_count == 5
    assert by_id[empty.id].object_count == 0
    assert by_id[empty.id].total_object_count == 0


def test_tree_of_an_empty_tenant_is_empty():
    roots = build_category_tree([])

    assert roots == []
    assert count_category_nodes(roots) == 0
    assert CategoryTreePublic(data=roots, count=0).model_dump() == {
        "data": [],
        "count": 0,
    }


def test_category_node_serializes_recursively(session: Session):
    docs = _category(session, "Documents")
    invoices = _category(session, "Invoices", parent=docs)

    roots = build_category_tree([docs, invoices], direct_counts={invoices.id: 4})
    payload = CategoryTreePublic(
        data=roots, count=count_category_nodes(roots)
    ).model_dump()

    assert payload["count"] == 2
    child = payload["data"][0]["children"][0]
    assert child["name"] == "Invoices"
    assert child["parent_id"] == docs.id
    assert child["total_object_count"] == 4
    assert child["children"] == []


def test_orphan_parent_reference_is_treated_as_a_root(session: Session):
    """A subtree handed over without its parent still forms a tree."""
    docs = _category(session, "Documents")
    invoices = _category(session, "Invoices", parent=docs)

    roots = build_category_tree([invoices])

    assert [r.id for r in roots] == [invoices.id]


# ── resolved paths ────────────────────────────────────────────────────────────


def test_paths_resolve_from_the_root_down(session: Session):
    docs = _category(session, "Documents")
    invoices = _category(session, "Invoices", parent=docs)
    y2026 = _category(session, "2026", parent=invoices)

    paths = resolve_category_paths([docs, invoices, y2026])

    assert paths[docs.id] == "documents"
    assert paths[invoices.id] == "documents/invoices"
    assert paths[y2026.id] == "documents/invoices/2026"


def test_path_resolution_breaks_a_cycle(session: Session):
    """A corrupt parent loop resolves to a finite path instead of hanging."""
    first = _category(session, "First")
    second = _category(session, "Second", parent=first)
    first.parent_id = second.id
    session.add(first)
    session.commit()
    session.refresh(first)

    paths = resolve_category_paths([first, second])

    assert paths[first.id].endswith("first")
    assert paths[second.id].endswith("second")


def test_category_refs_carry_id_name_and_path(session: Session):
    docs = _category(session, "Documents")
    invoices = _category(session, "Invoices", parent=docs)

    refs = category_refs([invoices], scope=[docs, invoices])

    assert [(r.id, r.name, r.path) for r in refs] == [
        (invoices.id, "Invoices", "documents/invoices")
    ]


# ── public object schema ──────────────────────────────────────────────────────


def test_media_object_public_defaults_to_no_categories(session: Session):
    obj = _media_object(session, "a.pdf")

    public = MediaObjectPublic.model_validate(obj)

    assert public.categories == []


def test_media_object_public_accepts_resolved_category_refs(session: Session):
    docs = _category(session, "Documents")
    invoices = _category(session, "Invoices", parent=docs)
    obj = _media_object(session, "a.pdf")
    _file_into(session, obj, invoices)

    public = MediaObjectPublic.model_validate(
        obj, update={"categories": category_refs([invoices], scope=[docs, invoices])}
    )

    assert [c.path for c in public.categories] == ["documents/invoices"]


# ── migration-facing metadata (`D6`: autogenerated, never hand-written) ───────


def test_link_table_is_visible_to_autogenerate_metadata():
    """Alembic autogenerates against `SQLModel.metadata`; the table must be in it."""
    name = prefixed_tables("media_object_category")
    assert name in SQLModel.metadata.tables

    table = SQLModel.metadata.tables[name]
    assert {c.name for c in table.primary_key.columns} == {
        "media_object_id",
        "category_id",
    }
    assert {c.name for c in table.c if c.index} == {"media_object_id", "category_id"}
    assert {fk.column.table.name for fk in table.foreign_keys} == {
        prefixed_tables("media_object"),
        prefixed_tables("category"),
    }


def test_category_table_carries_the_new_columns():
    table = SQLModel.metadata.tables[prefixed_tables("category")]

    assert "parent_id" in table.c
    assert table.c.parent_id.nullable
    assert table.c.parent_id.index
    assert {fk.column.table.name for fk in table.c.parent_id.foreign_keys} == {
        prefixed_tables("category")
    }

    assert "tenant_id" in table.c
    assert table.c.tenant_id.nullable
    assert table.c.tenant_id.index


def test_repo_still_ships_no_hand_written_alembic_revisions():
    """`D6`: `docker_start.sh` autogenerates the baseline; nothing is hand-written."""
    versions = (
        pathlib.Path(__file__).resolve().parents[1]
        / "media_service"
        / "alembic"
        / "versions"
    )

    assert not versions.exists()
