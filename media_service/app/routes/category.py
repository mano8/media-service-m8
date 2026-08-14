"""Category api routes.

A category is an owned record with no public form — the table carries no
visibility column — so the router mounts the reader floor and the three
mutations name ``CurrentWriter`` (A16).
"""

from typing import Any, Optional, Union
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select
from sqlmodel import func

from media_service.app.deps import (
    CurrentReader,
    CurrentWriter,
    SessionDep,
    require_reader,
)

from media_service.db_models.categories import (
    Category,
    CategoryCreate,
    CategoryUpdate,
    CategoriesPublic,
)
from fastapi_m8 import BaseController, ResponseMessage, ResponseModelBase

router = APIRouter(
    prefix="/category",
    tags=["category"],
    dependencies=[Depends(require_reader)],
)
# pylint: disable=broad-exception-caught, not-callable


@router.get(
    "/",
    response_model=Optional[CategoriesPublic],
    responses=BaseController.get_error_responses(),
)
async def read_root(
    session: SessionDep, current_user: CurrentReader, skip: int = 0, limit: int = 100
) -> Any:
    """Retrieve category list."""
    try:
        if current_user.is_superuser:
            count_statement = select(func.count()).select_from(Category)
            count = session.exec(count_statement).one()
            statement = select(Category).offset(skip).limit(limit)
            items = session.exec(statement).all()
        else:
            count_statement = (
                select(func.count())
                .select_from(Category)
                .where(Category.owner_id == str(current_user.id))
            )
            count = session.exec(count_statement).one()
            statement = (
                select(Category)
                .where(Category.owner_id == str(current_user.id))
                .offset(skip)
                .limit(limit)
            )
            items = session.exec(statement).all()

        return CategoriesPublic(data=items, count=count)
    except Exception as ex:
        return BaseController.handle_exception(ex=ex, session=session)


@router.get(
    "/get/{item_id}/",
    response_model=Union[ResponseModelBase, ResponseMessage],
    responses=BaseController.get_error_responses(),
)
def read_item(session: SessionDep, current_user: CurrentReader, item_id: int) -> Any:
    """
    Get item by ID.
    """
    try:
        item = session.get(Category, item_id)
        if not item:
            return ResponseMessage(success=False, msg="Item not found.")
        if not current_user.is_superuser and (
            str(item.owner_id) != str(str(current_user.id))
        ):
            raise HTTPException(status_code=403, detail="Not enough permissions")
        return ResponseModelBase(success=True, data=dict(item))
    except HTTPException as ex:
        raise ex
    except Exception as ex:
        return BaseController.handle_exception(ex=ex, session=session)


@router.post(
    "/add/",
    response_model=ResponseModelBase,
    responses=BaseController.get_error_responses(),
)
def create_item(
    *, session: SessionDep, current_user: CurrentWriter, item_in: CategoryCreate
) -> Any:
    """
    Create new item.
    """
    try:
        item = Category.model_validate(item_in, update={"owner_id": current_user.id})
        session.add(item)
        session.commit()
        session.refresh(item)
        return ResponseModelBase(success=True, data=dict(item))
    except Exception as ex:
        return BaseController.handle_exception(ex=ex, session=session)


@router.put(
    "/edit/{item_id}/",
    response_model=ResponseModelBase,
    responses=BaseController.get_error_responses(),
)
def update_item(
    *,
    session: SessionDep,
    current_user: CurrentWriter,
    item_id: int,
    item_in: CategoryUpdate,
) -> Any:
    """
    Update an item.
    """
    try:
        item = session.get(Category, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        if not current_user.is_superuser and (
            str(item.owner_id) != str(current_user.id)
        ):
            raise HTTPException(status_code=403, detail="Not enough permissions")
        update_dict = item_in.model_dump(exclude_unset=True)
        item.sqlmodel_update(update_dict)
        session.add(item)
        session.commit()
        session.refresh(item)
        return ResponseModelBase(success=True, data=dict(item))
    except Exception as ex:
        return BaseController.handle_exception(ex=ex, session=session)


@router.delete(
    "/delete/{item_id}/",
    response_model=ResponseMessage,
    responses=BaseController.get_error_responses(),
)
def delete_item(
    session: SessionDep, current_user: CurrentWriter, item_id: int
) -> ResponseMessage:
    """
    Delete an item.
    """
    try:
        item = session.get(Category, item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Item not found")
        if not current_user.is_superuser and (
            str(item.owner_id) != str(current_user.id)
        ):
            raise HTTPException(status_code=403, detail="Not enough permissions")
        session.delete(item)
        session.commit()
        return ResponseMessage(success=True, msg="Category deleted successfully")
    except Exception as ex:
        return BaseController.handle_exception(ex=ex, session=session)
