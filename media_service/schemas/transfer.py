"""Pydantic schemas for the export/import (transfer) surface (`U9`).

Both locked export formats (2026-07-04) are real. ``manifest`` is answered
synchronously as a streamed JSON body — metadata only, no bytes.  ``archive``
cannot be: it has to read every object out of storage and zip it, so the
request creates an :class:`~media_service.db_models.export_jobs.ExportJob`
(202 + :class:`ExportJobPublic`) and the assembled zip is collected later from
``GET /media/v1/export/{job_id}``, which hands back a presigned download once
the job completes.

The import half reads the very same document back. Its schemas are separate
types rather than the export ones reused, because the two directions trust
their input differently: an export projects rows this service already owns,
while an import parses an attacker-controlled file — so every bound the
category and media models carry is restated on the way in, and the fields an
import must never believe (``status``, ``scan_status``, timestamps, foreign row
ids) are simply absent from the inbound shapes.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlmodel import Field, SQLModel

from media_service.db_models.categories import (
    MAX_CATEGORY_ASSIGNMENTS,
    CategoryNode,
)
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


# ── Import (`U9`) ────────────────────────────────────────────────────────────

ImportFormat = Literal["manifest", "archive"]


class ImportCategoryNode(SQLModel):
    """One node of an uploaded category tree.

    Deliberately identity-free. The export's :class:`CategoryNode` carries
    ``id``/``parent_id``/``owner_id``/``tenant_id`` and the two object counts;
    none of them survive parsing here, because a foreign manifest's row ids
    mean nothing in the importing scope and its counts are derived data. What
    an import reads is the *shape*: the nesting, and a name and slug per level.
    The extra fields are ignored rather than refused, so an untouched export
    document imports as-is.
    """

    name: str = Field(min_length=1, max_length=50)
    slug: str = Field(min_length=1, max_length=50)
    children: list["ImportCategoryNode"] = Field(default_factory=list)


ImportCategoryNode.model_rebuild()


class ImportObjectEntry(SQLModel):
    """One object's row in an uploaded manifest.

    A deliberately narrower shape than the :class:`ManifestObjectEntry` it is
    parsed from: ``status`` and ``scan_status`` are **not** fields here, so the
    import path structurally cannot read — let alone trust — a verdict some
    other service reached about these bytes (`U9`). A re-imported object is
    re-driven through the normal upload+scan pipeline and starts ``PENDING``
    like every other upload. ``created_at``/``updated_at`` are dropped for the
    same reason: the importing service stamps its own.

    ``id`` is the source row's id and is the correlation key an import matches
    on — the field the export carries precisely so a round-trip can find its
    way back.
    """

    id: uuid.UUID
    filename: str | None = Field(default=None, max_length=255)
    category: MediaCategory
    category_paths: list[str] = Field(
        default_factory=list, max_length=MAX_CATEGORY_ASSIGNMENTS
    )
    visibility: MediaVisibility
    size_bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    mime_type: str = Field(min_length=1, max_length=255)


class ImportManifest(SQLModel):
    """The uploaded document both import formats are driven from.

    For ``manifest`` it *is* the uploaded file; for ``archive`` it is the
    ``manifest.json`` entry inside the zip — the export writes the identical
    document in both cases, so an import reads one shape either way.
    """

    category_tree: list[ImportCategoryNode] = Field(default_factory=list)
    objects: list[ImportObjectEntry] = Field(default_factory=list)


#: Outcome of one manifest row.
#:
#: ``created``
#:     New bytes were driven through the upload pipeline and a new object
#:     exists, pending its scan.
#: ``linked``
#:     The object was already here and was re-filed into the recreated
#:     categories; nothing was uploaded.
#: ``skipped``
#:     Nothing to do for this row — a manifest-only import with no local
#:     counterpart carries no bytes to create one from.
#: ``failed``
#:     The row was refused, with ``reason`` saying why.
ImportRowStatus = Literal["created", "linked", "skipped", "failed"]

#: Why a row ended up as it did.
#:
#: The first five tokens are `U1`'s upload-reject vocabulary, reused verbatim
#: rather than paraphrased, so a client maps an import failure to the same copy
#: it already shows for the equivalent upload failure. The rest name outcomes
#: only an import can have.
ImportRowReason = Literal[
    # `U1`'s vocabulary (media_service.schemas.uploads.UploadRejectDetail)
    "size_exceeded",
    "mime_mismatch",
    "sha256_mismatch",
    "quota_bytes_exceeded",
    "quota_objects_exceeded",
    # import-only outcomes
    "missing_bytes",
    "already_exists",
    "id_conflict",
    "unsupported_mime",
    "invalid_metadata",
    "storage_error",
]


class ImportObjectResult(SQLModel):
    """What happened to one row of the uploaded manifest.

    ``media_object_id`` equals ``source_id`` whenever a row was created or
    linked: an import reuses the source id so importing the same document twice
    links the second time instead of duplicating the collection. No storage
    location is exposed here, for the same reason the manifest export carries
    none.
    """

    source_id: uuid.UUID
    filename: str | None = None
    status: ImportRowStatus
    reason: ImportRowReason | None = None
    message: str | None = None
    media_object_id: uuid.UUID | None = None
    category_paths: list[str] = Field(default_factory=list)
    scan_queued: bool = False


class ImportReport(SQLModel):
    """Per-row result report for one ``POST /media/v1/import`` call.

    A batch answers ``200`` with this report even when individual rows failed:
    the request itself succeeded, and the caller needs to see *which* rows did
    not land and why. A refusal of the whole document — an unreadable archive,
    a manifest over one of the ceilings — is still a normal 4xx.
    """

    format: ImportFormat
    categories_created: int = 0
    categories_reused: int = 0
    created: int = 0
    linked: int = 0
    skipped: int = 0
    failed: int = 0
    objects: list[ImportObjectResult] = Field(default_factory=list)
