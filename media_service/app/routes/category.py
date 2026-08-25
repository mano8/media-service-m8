"""Category api routes.

A category is an owned record with no public form — the table carries no
visibility column — so the router mounts the reader floor and the three
mutations name ``CurrentWriter`` (A16, `D2`).

The handlers are thin: every scoping, ownership and refusal decision lives in
:class:`media_service.controllers.category.CategoryController` (`D5`), on the
same convention as the rest of the media surface.
"""

from fastapi import APIRouter, Depends

from fastapi_m8 import BaseController

from media_service.app.deps import (
    CurrentReader,
    CurrentWriter,
    SessionDep,
    require_reader,
)
from media_service.controllers.category import CategoryController
from media_service.db_models.categories import (
    CategoriesPublic,
    CategoryCreate,
    CategoryPublic,
    CategoryTreePublic,
    CategoryUpdate,
)

router = APIRouter(
    prefix="/category",
    tags=["category"],
    dependencies=[Depends(require_reader)],
)


@router.get(
    "/",
    response_model=CategoriesPublic,
    responses=BaseController.get_error_responses(),
)
def read_root(
    *,
    session: SessionDep,
    current_user: CurrentReader,
    skip: int = 0,
    limit: int = 100,
) -> CategoriesPublic:
    """Retrieve the caller's category list."""
    return CategoryController.list_categories(
        session=session, current_user=current_user, skip=skip, limit=limit
    )


@router.get(
    "/tree/",
    response_model=CategoryTreePublic,
    responses=BaseController.get_error_responses(),
)
def read_tree(
    *,
    session: SessionDep,
    current_user: CurrentReader,
) -> CategoryTreePublic:
    """Get the caller's nested category tree, each node carrying object counts."""
    return CategoryController.get_category_tree(
        session=session, current_user=current_user
    )


@router.get(
    "/get/{item_id}/",
    response_model=CategoryPublic,
    responses=BaseController.get_error_responses(),
)
def read_item(
    *,
    session: SessionDep,
    current_user: CurrentReader,
    item_id: int,
) -> CategoryPublic:
    """Get a category by ID."""
    return CategoryController.get_category(
        session=session, current_user=current_user, category_id=item_id
    )


@router.post(
    "/add/",
    response_model=CategoryPublic,
    status_code=201,
    responses=BaseController.get_error_responses(),
)
def create_item(
    *,
    session: SessionDep,
    current_user: CurrentWriter,
    item_in: CategoryCreate,
) -> CategoryPublic:
    """Create a new category, optionally nested under an existing one.

    A ``parent_id`` must name a category in the caller's own scope, and the
    name must be free among that parent's children.
    """
    return CategoryController.create_category(
        session=session, current_user=current_user, req=item_in
    )


@router.put(
    "/edit/{item_id}/",
    response_model=CategoryPublic,
    responses=BaseController.get_error_responses(),
)
def update_item(
    *,
    session: SessionDep,
    current_user: CurrentWriter,
    item_id: int,
    item_in: CategoryUpdate,
) -> CategoryPublic:
    """Update an owned category, including reparenting it.

    A reparent may not cross a tenant boundary or close a cycle, and the
    resulting name must be free among the new siblings.
    """
    return CategoryController.update_category(
        session=session,
        current_user=current_user,
        category_id=item_id,
        req=item_in,
    )


@router.delete(
    "/delete/{item_id}/",
    response_model=None,
    status_code=204,
    responses=BaseController.get_error_responses(),
)
def delete_item(
    *,
    session: SessionDep,
    current_user: CurrentWriter,
    item_id: int,
) -> None:
    """Delete an owned category that holds no child categories.

    A category with children is refused; reparent or delete them first.
    Assigned media is unaffected — only the filings are removed.
    """
    CategoryController.delete_category(
        session=session, current_user=current_user, category_id=item_id
    )
