"""Pydantic schemas for the export/import (transfer) surface (`U9`).

Both locked export formats (2026-07-04) are real. ``manifest`` is answered
synchronously as a streamed JSON body — metadata only, no bytes.  ``archive``
cannot be: it has to read every object out of storage and zip it, so the
request creates an :class:`~media_service.db_models.export_jobs.ExportJob`
(202 + :class:`ExportJobPublic`) and the assembled zip is collected later from
``GET /media/v1/export/{job_id}``, which hands back a presigned download once
the job completes.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlmodel import Field, SQLModel

from media_service.db_models.categories import CategoryNode
from media_service.db_models.export_jobs import ExportJobStatus
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


class ExportJobPublic(SQLModel):
    """Public view of an ``archive`` export job.

    Returned by ``POST /media/v1/export`` (202, ``queued``) and by
    ``GET /media/v1/export/{job_id}``. ``download_url`` is a short-lived
    presigned GET, minted per status read rather than stored, and is therefore
    populated only while the job is ``completed`` and unexpired — the same rule
    the object download surface applies, so an export URL never outlives the
    signature that makes it work. The storage bucket and key the archive lives
    at are deliberately not exposed: a caller gets the signed URL, not the
    location.
    """

    id: uuid.UUID
    status: ExportJobStatus
    object_count: int
    total_size_bytes: int
    size_bytes: int | None = None
    expires_at: datetime | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    download_url: str | None = None
