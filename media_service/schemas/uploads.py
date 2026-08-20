"""Pydantic schemas for the presigned upload flow."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import model_validator
from sqlmodel import Field, SQLModel

from media_service.core.validation import max_size_for_category
from media_service.db_models.categories import MAX_CATEGORY_ASSIGNMENTS
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObjectPublic,
    MediaVisibility,
)


class UploadInitiateRequest(SQLModel):
    """Payload to initiate a presigned upload session."""

    category: MediaCategory
    visibility: MediaVisibility
    original_filename: str
    mime_type: str
    # Declared upper bound for the object. Must be at least one byte and is
    # capped at the category maximum here, before any presigned URL is issued,
    # so the declared size is a real policy input rather than a number a caller
    # can shrink to slip past quota accounting (actual stored size is the
    # authority at completion).
    expected_size_bytes: int = Field(ge=1)
    # Optional user categories to file the object into once it completes (`U4`).
    # Validated here, before a URL is issued, and stored on the session so the
    # filing survives to completion; the fixed ``category`` above is untouched
    # and keeps driving policy (size cap, allowed MIME, quota).
    category_ids: list[int] = Field(
        default_factory=list,
        max_length=MAX_CATEGORY_ASSIGNMENTS,
        description="User categories to file the completed object into",
    )

    @model_validator(mode="after")
    def _cap_to_category_maximum(self) -> "UploadInitiateRequest":
        cap = max_size_for_category(str(self.category))
        if self.expected_size_bytes > cap:
            raise ValueError(
                f"expected_size_bytes {self.expected_size_bytes} exceeds the "
                f"maximum of {cap} bytes for category {self.category}."
            )
        return self


class UploadInitiateResponse(SQLModel):
    """Response after a session is created.

    ``upload_url`` is an S3 POST endpoint: the client must send a multipart
    ``POST`` containing every entry of ``upload_fields`` followed by the
    ``file`` part. The signed policy caps the body size and pins the
    ``Content-Type``, so storage rejects an oversized or wrong-typed upload
    rather than letting it land.
    """

    session_id: uuid.UUID
    upload_url: str
    upload_fields: dict[str, str]
    expires_at: datetime


class UploadCompleteRequest(SQLModel):
    """Optional payload for completing an upload."""

    sha256: str | None = None
    # ``None`` keeps whatever filing was declared at initiate; a list replaces
    # it wholesale (set semantics), and ``[]`` completes the object unfiled.
    # The distinction is why this is nullable rather than defaulting to ``[]``:
    # a body sent only to carry ``sha256`` must not silently clear the filing.
    category_ids: list[int] | None = Field(
        default=None,
        max_length=MAX_CATEGORY_ASSIGNMENTS,
        description="Replaces the categories declared at initiate; [] files none",
    )


class UploadCompleteResponse(SQLModel):
    """Response after an upload is confirmed in storage."""

    media_object: MediaObjectPublic


class UploadRejectDetail(SQLModel):
    """Structured 422 detail raised when a staged upload is rejected.

    ``reason`` is the same stable token already computed for the
    ``inc_upload_rejected`` metric, so the client can map it to friendly copy
    instead of string-matching. ``message`` stays byte-identical to the prior
    plain-string detail (``f"Upload rejected: {reason}."``) so the client's
    ``messageFromDetail`` fallback keeps rendering unchanged. This is a flat
    JSON object, never a list, so that fallback's ``{msg}``-array branch does
    not need to change to read it.
    """

    code: Literal["upload_rejected"] = "upload_rejected"
    reason: Literal[
        "size_exceeded",
        "mime_mismatch",
        "sha256_mismatch",
        "quota_bytes_exceeded",
        "quota_objects_exceeded",
    ]
    message: str
