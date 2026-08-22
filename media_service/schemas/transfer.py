"""Pydantic schemas for the export/import (transfer) surface (`U9`).

Only the ``manifest`` shape is real today. ``archive`` is accepted on the
request (the format choice is locked 2026-07-04) but is not yet assembled — the
async job that builds it is the next `U9` step — so the controller answers 501
for it rather than silently downgrading to a manifest.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlmodel import Field, SQLModel

from media_service.db_models.categories import CategoryNode
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
)
from media_service.schemas.objects import ObjectListParams

ExportFormat = Literal["manifest", "archive"]


class ExportRequest(SQLModel):
    """Body of ``POST /media/v1/export``.

    ``filters`` reuses :class:`media_service.schemas.objects.ObjectListParams`
    so export and the library share one filter vocabulary, including the
    ``category_id``/``include_descendants``/``uncategorized`` branch filters
    (`D-filter`). ``limit`` and ``cursor`` on that shape are page-sized for the
    library and make no sense for a full export; the export controller reads
    every other field and ignores those two rather than paging.
    """

    format: ExportFormat
    filters: ObjectListParams | None = None


class ManifestObjectEntry(SQLModel):
    """One object's row in a ``manifest`` export.

    No bytes and no storage location (``storage_bucket``/``object_key``) — a
    manifest is metadata-only by design (locked 2026-07-04). ``id`` is carried
    so a later import can correlate a re-created record back to the row it came
    from; ``category_paths`` are the object's user-category (M2M) filing,
    resolved the same way :func:`media_service.controllers.category.category_refs`
    resolves them elsewhere, while ``category`` stays the fixed policy enum.
    """

    id: uuid.UUID
    filename: str | None
    category: MediaCategory
    category_paths: list[str] = Field(default_factory=list)
    visibility: MediaVisibility
    size_bytes: int
    sha256: str | None
    mime_type: str
    status: MediaObjectStatus
    scan_status: ScanStatus
    created_at: datetime
    updated_at: datetime


class ExportManifest(SQLModel):
    """Full shape of a ``manifest`` export, for typing/tests only.

    The route never builds this object in memory — it streams the same
    fields incrementally (`U9`'s "stream JSON" requirement) — but the shape is
    named here once so tests can assert against it instead of a bare dict.
    """

    category_tree: list[CategoryNode]
    objects: list[ManifestObjectEntry]
