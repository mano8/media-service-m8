"""media_service fastapi app db models for categories"""

from typing import TYPE_CHECKING, List, Optional
import uuid
from pydantic import model_validator
from sqlalchemy import UniqueConstraint
from sqlmodel import Column, Field, Relationship, SQLModel
from slugify import slugify

from fastapi_m8 import TimestampMixin
from media_service.core.db_models import UUIDString, prefixed_tables
from media_service.core.config import settings
from media_service.db_models.media_object_categories import MediaObjectCategoryLink

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from media_service.db_models.media_objects import MediaObject


# ---------------------------------------------------------------
# ---------------------------------------------------------------
# ------- Category
# ---------------------------------------------------------------
# ---------------------------------------------------------------
class CategoryBase(SQLModel):
    """
    Shared fields for category schemas.
    """

    name: str = Field(
        min_length=1,
        max_length=50,
        description="Category name",
    )
    slug: str = Field(
        min_length=1,
        max_length=50,
        description="URL-friendly identifier",
    )
    parent_id: int | None = Field(
        default=None,
        foreign_key=f"{prefixed_tables('category')}.id",
        index=True,
        description="Parent category ID; null marks a tree root",
    )


class CategoryGenerators(CategoryBase):
    """
    Category schema with slug auto-generation.
    """

    @model_validator(mode="before")
    @classmethod
    def generate_slug(cls, values):
        """
        Auto-generate `slug` from the `name` field.
        """
        name = values.get("name")
        if name:
            values["slug"] = slugify(values.get("name"))
        return values


class CategoryCreate(CategoryGenerators):
    """
    Schema for creating a new category.
    """


class CategoryUpdate(CategoryGenerators):
    """
    Schema for updating an existing category.
    """


class Category(TimestampMixin, CategoryBase, SQLModel, table=True):
    """
    Database model for a category.

    Nested through the self-referential ``parent_id`` (an adjacency list; a null
    parent is a tree root) and shared within a tenant, falling back to
    ``owner_id`` when the caller has no tenant.

    Uniqueness is composite — ``(tenant_id, parent_id, slug)`` — so the same
    name may be reused under a different parent or in a different tenant. SQL
    treats NULLs as distinct in a unique constraint, so this constraint does not
    by itself reject a duplicate slug between two *roots* of the same untenanted
    owner; the CRUD surface (`U4`) rejects that case explicitly.
    """

    __tablename__ = prefixed_tables("category")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "parent_id",
            "slug",
            name="uq_category_tenant_parent_slug",
        ),
        {"mysql_engine": settings.DB_ENGINE, "mysql_charset": settings.DB_CHARSET},
    )
    id: int = Field(
        default=None,
        primary_key=True,
        index=True,
        description="Category ID",
    )
    owner_id: uuid.UUID = Field(
        sa_column=Column("owner_id", UUIDString(), nullable=False, index=True),
        description="ID of the user who owns this category",
    )
    tenant_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column("tenant_id", UUIDString(), nullable=True, index=True),
        description="Tenant the category is shared within; null falls back to owner scope",
    )

    parent: Optional["Category"] = Relationship(
        back_populates="children",
        sa_relationship_kwargs={"remote_side": "Category.id"},
    )
    children: list["Category"] = Relationship(back_populates="parent")
    media_objects: list["MediaObject"] = Relationship(
        back_populates="user_categories",
        link_model=MediaObjectCategoryLink,
    )


class CategoryPublic(CategoryBase, SQLModel):
    """
    Public representation of a category.
    """

    id: int = Field(
        description="Category ID",
    )
    owner_id: uuid.UUID = Field(
        description="ID of the user who owns this category",
    )
    tenant_id: uuid.UUID | None = Field(
        default=None,
        description="Tenant the category is shared within",
    )


class CategoriesPublic(SQLModel):
    """
    Wrapper for a list of public categories.
    """

    data: List[CategoryPublic] = Field(
        description="List of categories",
    )
    count: int = Field(
        description="Total categories count",
    )


class CategoryNode(CategoryPublic):
    """
    One node of the nested category tree.

    Both counts are carried so a tree pane can render "how much is in here"
    without a second call and can skip fetching a genuinely empty subtree
    (`U7`).
    """

    object_count: int = Field(
        default=0,
        ge=0,
        description="Media objects filed directly on this category",
    )
    total_object_count: int = Field(
        default=0,
        ge=0,
        description="Distinct media objects filed on this category or any descendant",
    )
    children: list["CategoryNode"] = Field(
        default_factory=list,
        description="Nested child nodes, ordered by name",
    )


CategoryNode.model_rebuild()


class CategoryTreePublic(SQLModel):
    """
    Wrapper for the caller's nested category tree.
    """

    data: list[CategoryNode] = Field(
        default_factory=list,
        description="Root nodes of the tree",
    )
    count: int = Field(
        default=0,
        description="Total categories in the tree, at every depth",
    )


class MediaObjectCategoryRef(SQLModel):
    """
    A user category a media object is filed into, with its resolved path.

    ``path`` is the slug chain from the root down to this category
    (``"documents/invoices/2026"``), so a client can show where an object lives
    without holding the whole tree.
    """

    id: int = Field(
        description="Category ID",
    )
    name: str = Field(
        description="Category name",
    )
    path: str = Field(
        description="Slash-joined slug path from the tree root to this category",
    )
