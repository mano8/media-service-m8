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
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from fastapi_m8 import UserModel

from media_service.db_models.categories import (
    CategoriesPublic,
    Category,
    CategoryCreate,
    CategoryPublic,
    CategoryUpdate,
)


def _owner_id(current_user: UserModel) -> uuid.UUID:
    """Return the caller's id as a UUID, matching the stored ``owner_id``."""
    return uuid.UUID(str(current_user.id))


def _scoped_query(current_user: UserModel) -> SelectOfScalar[Category]:
    """Build the base query, narrowed to the caller's own rows.

    A superuser sees every tenant's categories; everyone else sees only what
    they own. Tenant scoping proper is the next `U4` step and lands on top of
    this helper, so the narrowing stays in one place.
    """
    statement = select(Category)
    if current_user.is_superuser:
        return statement
    return statement.where(col(Category.owner_id) == _owner_id(current_user))


def _load_category(
    session: Session, current_user: UserModel, category_id: int
) -> Category:
    """Fetch a category, enforcing ownership for non-superusers.

    Raises 404 for a missing row and 403 when a non-owner is not a superuser.
    The two are deliberately distinct: the caller is already authenticated past
    the reader floor, so a category id is not an existence oracle here.
    """
    row = session.get(Category, category_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Category not found."
        )
    if not current_user.is_superuser and row.owner_id != _owner_id(current_user):
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
        """Create a category owned by the caller."""
        row = Category.model_validate(req, update={"owner_id": _owner_id(current_user)})
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
