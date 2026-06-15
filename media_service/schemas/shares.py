"""Pydantic schemas for media share-link endpoints."""

from datetime import datetime
import uuid

from sqlmodel import Field, SQLModel


class ShareTokenCreate(SQLModel):
    """Owner-supplied parameters for minting a share link."""

    # Seconds until the link expires; the caller's preferred lifetime. ``None``
    # falls back to the operator default (``MEDIA_SHARE_DEFAULT_EXPIRES_SECONDS``).
    # A value above the operator ceiling (``MEDIA_SHARE_MAX_EXPIRES_SECONDS``) is
    # rejected at create time — the bound is configurable, so it is enforced in
    # the controller rather than pinned to a static schema constant.
    expires_in: int | None = Field(default=None, ge=1)
    max_uses: int | None = Field(default=None, ge=1)


class ShareTokenPublic(SQLModel):
    """Public representation of a share link, including its signed token."""

    id: uuid.UUID
    media_object_id: uuid.UUID
    token: str
    expires_at: datetime
    max_uses: int | None
    uses: int
    revoked: bool
    created_at: datetime


class ShareTokenListResponse(SQLModel):
    """A list of share links owned for a single media object."""

    items: list[ShareTokenPublic]
    count: int
