"""Business logic for user-defined media categories.

Extracted from ``app/routes/category.py`` (`D5`), which was the last domain
still carrying its CRUD inline on the legacy
``ResponseModelBase``/``BaseController``/broad-``except`` pattern. The logic now
matches the shape of :mod:`media_service.controllers.objects`: module-level
scoping/loading helpers, a controller class of static methods, and typed
``HTTPException`` for every refusal — a swallowed exception here would hide a
failed commit behind a ``200 {"success": false}``.

A category is an owned record with no public form, so nothing in this module
takes an optional principal: the reader/writer floors on the router (A16, `D2`)
guarantee a real caller.

Every query is additionally scoped by tenant (`D4`): a tenanted caller sees
their tenant's shared categories, an untenanted caller falls back to their own
``owner_id`` scope. Tenant extraction reuses
:func:`media_service.core.tenancy.user_tenant_id` rather than reinventing it
here.

The same scope is what every *assignment* surface is validated against —
:func:`resolve_category_ids` is the trust boundary for the ``category_ids`` a
caller sends to upload initiate/complete and to the object PATCH — so a filing
can only ever name a category that caller can already see.

The hierarchy invariants live here too, because the database cannot express
them: a parent must sit in the same scope as its child, the ``parent_id`` chain
must stay acyclic, a sibling slug must stay unique (the composite unique
constraint is defeated by SQL's distinct-NULL rule whenever ``tenant_id`` or
``parent_id`` is null), and a category with children may not be deleted out
from under them.
"""

import uuid
from collections.abc import Sequence

from fastapi import HTTPException, status
from sqlalchemy import and_, func
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from fastapi_m8 import UserModel

from media_service.core.category_tree import (
    build_category_tree,
    category_refs,
    collect_branch_ids,
    count_category_nodes,
)
from media_service.core.tenancy import user_tenant_id
from media_service.db_models.categories import (
    CategoriesPublic,
    Category,
    CategoryCreate,
    CategoryPublic,
    CategoryTreePublic,
    CategoryUpdate,
    MediaObjectCategoryRef,
)
from media_service.db_models.media_object_categories import MediaObjectCategoryLink


def _owner_id(current_user: UserModel) -> uuid.UUID:
    """Return the caller's id as a UUID, matching the stored ``owner_id``."""
    return uuid.UUID(str(current_user.id))


def _scoped_query(current_user: UserModel) -> SelectOfScalar[Category]:
    """Build the base query, narrowed to the caller's tenant/owner scope.

    A superuser sees every tenant's categories. A tenanted caller sees the
    categories shared within their tenant (`D4`, locked 2026-07-04: "Tenant-
    scoped: categories are shared within a tenant"). An untenanted caller
    falls back to their own ``owner_id`` scope, restricted to untenanted rows
    so a solo user never sees a stray tenant-owned row.
    """
    statement = select(Category)
    if current_user.is_superuser:
        return statement
    tenant_id = user_tenant_id(current_user)
    if tenant_id is not None:
        return statement.where(col(Category.tenant_id) == tenant_id)
    return statement.where(
        and_(
            col(Category.owner_id) == _owner_id(current_user),
            col(Category.tenant_id).is_(None),
        )
    )


def _in_scope(current_user: UserModel, row: Category) -> bool:
    """Report whether ``row`` falls inside the caller's tenant/owner scope.

    The read side of :func:`_scoped_query`, applied to a row already in hand.
    Kept separate so the row loader and the parent loader below share one
    definition of "in scope" instead of drifting apart.
    """
    if current_user.is_superuser:
        return True
    tenant_id = user_tenant_id(current_user)
    if tenant_id is not None:
        return row.tenant_id == tenant_id
    return row.tenant_id is None and row.owner_id == _owner_id(current_user)


def _scope_key(
    tenant_id: uuid.UUID | None, owner_id: uuid.UUID
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Return the pair two rows must agree on to live in the same tree.

    A tenanted row is scoped by its tenant alone — categories are shared within
    a tenant, so two owners there belong to one tree. An untenanted row is
    scoped by its owner. Comparing the pair is what makes "cross-tenant parent"
    a structural rule rather than a permission one: a superuser passes every
    scope check and would otherwise be able to graft one tenant's branch onto
    another's, leaving a tree that no tenant-scoped read can render.
    """
    return (tenant_id, None) if tenant_id is not None else (None, owner_id)


def _load_category(
    session: Session, current_user: UserModel, category_id: int
) -> Category:
    """Fetch a category, enforcing tenant/ownership scope for non-superusers.

    Raises 404 for a missing row and 403 when a non-superuser caller is
    outside the row's tenant/owner scope. The two are deliberately distinct:
    the caller is already authenticated past the reader floor, so a category
    id is not an existence oracle here.
    """
    row = session.get(Category, category_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Category not found."
        )
    if not _in_scope(current_user, row):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions."
        )
    return row


def _load_parent(
    session: Session,
    current_user: UserModel,
    parent_id: int,
    *,
    tenant_id: uuid.UUID | None,
    owner_id: uuid.UUID,
) -> Category:
    """Fetch the requested parent, refusing a missing or foreign one.

    ``tenant_id``/``owner_id`` are the child's, not the caller's, so the same
    guard serves a create (the values just stamped on the new row) and a
    reparent (the values already on the stored row).
    """
    parent = session.get(Category, parent_id)
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Parent category not found."
        )
    if not _in_scope(current_user, parent):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions."
        )
    if _scope_key(parent.tenant_id, parent.owner_id) != _scope_key(tenant_id, owner_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Parent category belongs to another tenant.",
        )
    return parent


def _reject_cycle(session: Session, category_id: int, parent_id: int) -> None:
    """Refuse a reparent that would make ``category_id`` its own ancestor.

    Walks the stored ``parent_id`` chain upward from the requested parent. The
    self-parent case is the first iteration, so it needs no separate branch.
    ``seen`` bounds the walk: a cycle already on disk — a row written before
    this guard existed, or a direct DB write — must break the loop rather than
    hang the request, exactly as
    :func:`core.category_tree.resolve_category_paths` does on the read side.
    """
    seen: set[int] = set()
    ancestor_id: int | None = parent_id
    while ancestor_id is not None:
        if ancestor_id == category_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A category cannot be nested under itself or its descendants.",
            )
        if ancestor_id in seen:
            return
        seen.add(ancestor_id)
        ancestor = session.get(Category, ancestor_id)
        if ancestor is None:
            return
        ancestor_id = ancestor.parent_id


def _reject_duplicate_slug(
    session: Session,
    *,
    slug: str,
    parent_id: int | None,
    tenant_id: uuid.UUID | None,
    owner_id: uuid.UUID,
    exclude_id: int | None = None,
) -> None:
    """Refuse a slug already taken by a sibling under the same parent.

    ``uq_category_tenant_parent_slug`` cannot carry this on its own: SQL treats
    NULLs as distinct in a unique constraint, so the constraint only bites when
    ``tenant_id`` *and* ``parent_id`` are both non-null. Every root, and every
    row of an untenanted owner, escapes it — the case `U3` recorded in the
    model docstring and handed to this surface. The check is expressed over the
    same scope key the tree is read through, so a tenant's siblings collide
    across owners just as they render together.
    """
    statement = select(Category).where(col(Category.slug) == slug)
    if tenant_id is not None:
        statement = statement.where(col(Category.tenant_id) == tenant_id)
    else:
        statement = statement.where(
            and_(
                col(Category.tenant_id).is_(None),
                col(Category.owner_id) == owner_id,
            )
        )
    if parent_id is None:
        statement = statement.where(col(Category.parent_id).is_(None))
    else:
        statement = statement.where(col(Category.parent_id) == parent_id)
    if exclude_id is not None:
        statement = statement.where(col(Category.id) != exclude_id)
    if session.exec(statement.limit(1)).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A sibling category already uses this name.",
        )


def _unique_ids(category_ids: Sequence[int]) -> list[int]:
    """De-duplicate ids, preserving the order they were given in.

    A filing is a set: the link table's composite primary key already makes a
    repeated filing a no-op, so a body naming the same category twice is
    collapsed rather than refused.
    """
    seen: set[int] = set()
    unique: list[int] = []
    for category_id in category_ids:
        if category_id not in seen:
            seen.add(category_id)
            unique.append(category_id)
    return unique


def resolve_category_ids(
    session: Session, current_user: UserModel, category_ids: Sequence[int]
) -> list[Category]:
    """Resolve caller-supplied ids to in-scope rows, refusing any that miss.

    The trust boundary for every assignment surface (upload initiate, upload
    complete, object PATCH): an id the caller cannot see is refused with the
    same 404/403 pair ``GET /category/get/{id}`` answers, so a filing can never
    reference another tenant's category — and cannot be used as an existence
    oracle for one either.

    One query resolves the whole set, so the cost does not grow with the number
    of ids; the per-id reload below only runs on the refusal path, where the
    scoped query cannot say *why* an id is absent.
    """
    wanted = _unique_ids(category_ids)
    if not wanted:
        return []
    rows = session.exec(
        _scoped_query(current_user).where(col(Category.id).in_(wanted))
    ).all()
    found = {row.id: row for row in rows}
    for category_id in wanted:
        if category_id not in found:
            found[category_id] = _load_category(session, current_user, category_id)
    return [found[category_id] for category_id in wanted]


def categories_in_scope(
    session: Session, current_user: UserModel, category_ids: Sequence[int]
) -> list[Category]:
    """Return the ids that still resolve in the caller's scope, dropping the rest.

    The lenient counterpart to :func:`resolve_category_ids`, for ids that were
    validated once and then stored — the filing declared at upload initiate and
    replayed at completion. The object is in storage by then and the caller
    cannot retry the upload, so a category deleted in between is dropped from
    the filing rather than failing the completion outright.
    """
    wanted = _unique_ids(category_ids)
    if not wanted:
        return []
    rows = session.exec(
        _scoped_query(current_user).where(col(Category.id).in_(wanted))
    ).all()
    by_id = {row.id: row for row in rows}
    return [by_id[category_id] for category_id in wanted if category_id in by_id]


def branch_category_ids(
    session: Session,
    current_user: UserModel | None,
    category_id: int,
    *,
    include_descendants: bool,
) -> list[int]:
    """Resolve a branch filter to the category ids it is allowed to match.

    The read-side trust boundary for the objects list's ``category_id`` filter
    (`U4`), and the counterpart of :func:`resolve_category_ids` on the write
    side. It answers only *which categories* the branch covers; narrowing the
    listing is the objects controller's job, and it applies the result as an
    extra ``where`` on an already-scoped query, so a branch can subtract rows
    from what a caller may see but never add one.

    An authenticated caller naming an id outside their scope gets the same
    404/403 pair ``GET /category/get/{id}`` answers — a filter that silently
    returned an empty page would hide a typo behind a plausible result.

    ``current_user is None`` is the anonymous caller on the public read surface
    (A16, `D3`): the category surface sits behind the reader floor, so an
    anonymous caller has no category scope at all and every branch resolves to
    the empty set — an empty page, not a refusal. Refusing would make the
    outcome depend on whether a row the caller can never see happens to exist,
    which is precisely the existence oracle
    :func:`media_service.controllers.objects.require_visibility_access`
    keeps off this surface.
    """
    if current_user is None:
        return []
    row = _load_category(session, current_user, category_id)
    if not include_descendants:
        return [row.id]
    return collect_branch_ids(session.exec(_scoped_query(current_user)).all(), row.id)


def assigned_category_refs(
    session: Session, current_user: UserModel, categories: Sequence[Category]
) -> list[MediaObjectCategoryRef]:
    """Project one object's filing into public refs with resolved paths.

    Single-object paths only (a write response echoing what it just filed). The
    scope rows are loaded once per call to resolve the slug paths, which is one
    extra query for one object; the list surface needs a per-page load instead
    and is the next `U4` step, not this one.
    """
    if not categories:
        return []
    return category_refs(categories, session.exec(_scoped_query(current_user)).all())


def category_refs_by_object(
    session: Session,
    current_user: UserModel | None,
    object_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[MediaObjectCategoryRef]]:
    """Project every object's filing into public refs, one load per page (`U4`).

    The list-page counterpart of :func:`assigned_category_refs`: instead of
    resolving one object's filing with its own query, it resolves the whole
    page's filings in exactly two queries total — the link rows for
    ``object_ids``, then the caller's category scope once, to resolve slug
    paths — regardless of how many objects are on the page. Never one query
    per object.

    ``current_user is None`` is the anonymous public-catalogue caller (A16,
    `D3`): a category has no public form (`D2`), so every object on an
    anonymous listing gets an empty filing rather than a query that would
    otherwise need a real principal to scope.

    A link naming a category outside the caller's *current* scope (e.g. a
    superuser whose scope narrowed after the filing was made) is dropped
    rather than surfaced unresolved — the same "degrade, don't fail" choice
    :func:`core.category_tree.resolve_category_paths` makes for a dangling
    parent.
    """
    if current_user is None or not object_ids:
        return {}
    links = session.exec(
        select(MediaObjectCategoryLink)
        .where(col(MediaObjectCategoryLink.media_object_id).in_(object_ids))
        .order_by(
            col(MediaObjectCategoryLink.media_object_id),
            col(MediaObjectCategoryLink.category_id),
        )
    ).all()
    if not links:
        return {}
    scope = session.exec(_scoped_query(current_user)).all()
    by_id = {row.id: row for row in scope}
    known_links = [link for link in links if link.category_id in by_id]
    refs = category_refs([by_id[link.category_id] for link in known_links], scope)
    result: dict[uuid.UUID, list[MediaObjectCategoryRef]] = {}
    for link, ref in zip(known_links, refs):
        result.setdefault(link.media_object_id, []).append(ref)
    return result


class CategoryController:
    """Handle CRUD over the caller's user-defined category records."""

    @staticmethod
    def list_categories(
        *,
        session: Session,
        current_user: UserModel,
        skip: int = 0,
        limit: int = 100,
    ) -> CategoriesPublic:
        """Return a page of the caller's categories with the in-scope total.

        ``count`` is the full scoped total, not the page length, so a client can
        page without re-deriving how many rows exist.
        """
        scoped = _scoped_query(current_user)
        count = session.scalar(select(func.count()).select_from(scoped.subquery()))
        items = session.exec(scoped.offset(skip).limit(limit)).all()
        return CategoriesPublic(
            data=[CategoryPublic.model_validate(row) for row in items],
            count=count or 0,
        )

    @staticmethod
    def get_category_tree(
        *,
        session: Session,
        current_user: UserModel,
    ) -> CategoryTreePublic:
        """Return the caller's nested category tree with per-node object counts.

        ``object_count`` is a single grouped query over the in-scope category
        ids, not one query per node, so the tree endpoint stays O(1) queries
        regardless of tree size. ``count`` is the total node count at every
        depth, matching :func:`core.category_tree.count_category_nodes`.
        """
        categories = session.exec(_scoped_query(current_user)).all()
        if not categories:
            return CategoryTreePublic(data=[], count=0)
        category_ids = [category.id for category in categories]
        counts_statement = (
            select(
                MediaObjectCategoryLink.category_id,
                func.count(),
            )
            .where(col(MediaObjectCategoryLink.category_id).in_(category_ids))
            .group_by(col(MediaObjectCategoryLink.category_id))
        )
        direct_counts = dict(session.exec(counts_statement).all())
        roots = build_category_tree(categories, direct_counts)
        return CategoryTreePublic(data=roots, count=count_category_nodes(roots))

    @staticmethod
    def get_category(
        *,
        session: Session,
        current_user: UserModel,
        category_id: int,
    ) -> CategoryPublic:
        """Return one owned category."""
        return CategoryPublic.model_validate(
            _load_category(session, current_user, category_id)
        )

    @staticmethod
    def create_category(
        *,
        session: Session,
        current_user: UserModel,
        req: CategoryCreate,
    ) -> CategoryPublic:
        """Create a category owned by the caller, stamped with their tenant.

        ``tenant_id`` is never client-supplied — it is derived server-side from
        the caller so a category cannot be filed into a tenant its owner does
        not belong to. A supplied ``parent_id`` must resolve to a category in
        the same scope, and the slug must be free among the parent's children
        (or among the roots, when no parent is given). No cycle is possible
        here: the row has no id yet, so nothing can point back at it.
        """
        owner_id = _owner_id(current_user)
        tenant_id = user_tenant_id(current_user)
        if req.parent_id is not None:
            _load_parent(
                session,
                current_user,
                req.parent_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        _reject_duplicate_slug(
            session,
            slug=req.slug,
            parent_id=req.parent_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        row = Category.model_validate(
            req,
            update={"owner_id": owner_id, "tenant_id": tenant_id},
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return CategoryPublic.model_validate(row)

    @staticmethod
    def update_category(
        *,
        session: Session,
        current_user: UserModel,
        category_id: int,
        req: CategoryUpdate,
    ) -> CategoryPublic:
        """Patch an owned category from the set fields of ``req``.

        A reparent is validated against the *stored* row's scope, so moving a
        branch can neither cross a tenant boundary nor close a loop. The slug
        is re-checked whenever the name or the parent moves, since either
        changes which siblings it has to be unique among.
        """
        row = _load_category(session, current_user, category_id)
        changes = req.model_dump(exclude_unset=True)
        parent_id = changes.get("parent_id", row.parent_id)
        slug = changes.get("slug", row.slug)
        if parent_id != row.parent_id and parent_id is not None:
            _load_parent(
                session,
                current_user,
                parent_id,
                tenant_id=row.tenant_id,
                owner_id=row.owner_id,
            )
            _reject_cycle(session, row.id, parent_id)
        if (parent_id, slug) != (row.parent_id, row.slug):
            _reject_duplicate_slug(
                session,
                slug=slug,
                parent_id=parent_id,
                tenant_id=row.tenant_id,
                owner_id=row.owner_id,
                exclude_id=row.id,
            )
        row.sqlmodel_update(changes)
        session.add(row)
        session.commit()
        session.refresh(row)
        return CategoryPublic.model_validate(row)

    @staticmethod
    def delete_category(
        *,
        session: Session,
        current_user: UserModel,
        category_id: int,
    ) -> None:
        """Delete an owned category that has no children.

        A category holding children is refused with 409 rather than silently
        orphaning or cascading them — the caller reparents or deletes the
        branch first, so a delete can never remove more than the row asked
        for. Assigned media is the opposite case and is allowed: the
        ``media_object_category`` rows go with the category and the media
        objects themselves survive, which is why a filing is a link row and
        not a column on the object.
        """
        row = _load_category(session, current_user, category_id)
        child = session.exec(
            select(Category).where(col(Category.parent_id) == category_id).limit(1)
        ).first()
        if child is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Category has child categories; reparent or delete them first.",
            )
        session.delete(row)
        session.commit()
