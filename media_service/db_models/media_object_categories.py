"""Link table joining media objects to user-defined categories.

A media object may be filed into several user categories, and a category holds
many objects, so the association is many-to-many (`U3`). The fixed
``MediaCategory`` enum on ``MediaObject`` is untouched — it keeps driving policy
(quota, size cap, allowed MIME); user categories are a separate organizational
layer on top of it.
"""

import uuid

from sqlmodel import Field, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables


class MediaObjectCategoryLink(SQLModel, table=True):
    """One filing of a media object into one user category.

    The composite primary key makes a repeated filing a no-op at the schema
    level, and both columns are indexed so the join is cheap in either
    direction (object → its categories, category → its objects).
    """

    __tablename__ = prefixed_tables("media_object_category")
    __table_args__ = (
        {"mysql_engine": settings.DB_ENGINE, "mysql_charset": settings.DB_CHARSET},
    )

    media_object_id: uuid.UUID = Field(
        foreign_key=f"{prefixed_tables('media_object')}.id",
        primary_key=True,
        index=True,
        ondelete="CASCADE",
        description="Filed media object",
    )
    category_id: int = Field(
        foreign_key=f"{prefixed_tables('category')}.id",
        primary_key=True,
        index=True,
        ondelete="CASCADE",
        description="User category the object is filed into",
    )
