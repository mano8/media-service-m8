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
:func:`media_service.controllers.objects._user_tenant_id` rather than
reinventing it here.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import and_, func
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from fastapi_m8 import UserModel

from media_service.controllers.objects import _user_tenant_id
from media_service.core.category_tree import build_category_tree, count_category_nodes
from media_service.db_models.categories import (
    CategoriesPublic,
    Category,
    CategoryCreate,
    CategoryPublic,
    CategoryTreePublic,
    CategoryUpdate,
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
    tenant_id = _user_tenant_id(current_user)
    if tenant_id is not None:
        return statement.where(col(Category.tenant_id) == tenant_id)
    return statement.where(
        and_(
            col(Category.owner_id) == _owner_id(current_user),
            col(Category.tenant_id).is_(None),
        )
    )


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
    if current_user.is_superuser:
        return row
    tenant_id = _user_tenant_id(current_user)
    if tenant_id is not None:
        in_scope = row.tenant_id == tenant_id
    else:
        in_scope = row.tenant_id is None and row.owner_id == _owner_id(current_user)
    if not in_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions."
        )
    return row


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
        not belong to.
        """
        row = Category.model_validate(
            req,
            update={
                "owner_id": _owner_id(current_user),
                "tenant_id": _user_tenant_id(current_user),
            },
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
        """Patch an owned category from the set fields of ``req``."""
        row = _load_category(session, current_user, category_id)
        row.sqlmodel_update(req.model_dump(exclude_unset=True))
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
        """Delete an owned category."""
        row = _load_category(session, current_user, category_id)
        session.delete(row)
        session.commit()
