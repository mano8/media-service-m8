"""Database model tracking an asynchronous archive-export job (`U9`).

A ``manifest`` export is synchronous — it streams metadata and never touches
bytes. An ``archive`` export has to read every object's bytes out of storage
and zip them, which does not belong on the request path, so it becomes a row
here plus an ARQ job that the service-owned maintenance worker assembles.

The row is deliberately *not* a copy of the export: it stores the resolved
``filters`` and a snapshot of the authorizing principal's scope
(``owner_user_id``/``tenant_id``/``is_superuser``) — the three attributes the
object-listing scope helpers read — so the assembler re-runs the exact same
scoped query the request path already proved resolvable, instead of carrying a
materialised (and potentially multi-megabyte) object list in a column.
"""

from datetime import datetime
from enum import StrEnum
import uuid
from typing import Any

from sqlalchemy import JSON, Column, DateTime, String
from sqlmodel import Field, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables
from media_service.db_models.media_objects import utcnow


class ExportJobStatus(StrEnum):
    """Lifecycle state for an archive-export job.

    Mirrors :class:`media_service.db_models.variant_jobs.VariantJobStatus` so
    the two asynchronous surfaces report progress in one vocabulary.
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


#: States in which an export job still owes the caller a result. A caller may
#: hold only one of these at a time (bounding archive fan-out per principal).
ACTIVE_EXPORT_STATUSES = (ExportJobStatus.QUEUED, ExportJobStatus.PROCESSING)


class ExportJobBase(SQLModel):
    """Shared fields for an archive-export job."""

    owner_user_id: uuid.UUID = Field(index=True)
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    # Scope snapshot, not an authorization decision the worker gets to make:
    # a superuser's export covers a wider set than an owner's, so the flag the
    # request was authorized under is recorded rather than re-derived from a
    # token the worker never sees.
    is_superuser: bool = Field(default=False)
    status: ExportJobStatus = Field(
        default=ExportJobStatus.QUEUED,
        sa_column=Column(String(32), nullable=False, index=True),
    )
    #: ``ObjectListParams`` as serialized JSON — the same filter vocabulary the
    #: objects list and the manifest export read (`D-filter`).
    filters: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False),
    )
    #: Matching objects at request time (what the ceiling was checked against).
    object_count: int = Field(default=0, ge=0)
    #: Sum of the bytes the archive is expected to carry at request time.
    total_size_bytes: int = Field(default=0, ge=0)
    # Where the assembled archive landed. Null until the job completes; the
    # bucket is stored as written rather than re-derived, matching how media
    # objects record their own location.
    storage_bucket: str | None = Field(default=None, max_length=63)
    object_key: str | None = Field(default=None, max_length=1024)
    #: Size of the assembled zip (not of its contents).
    size_bytes: int | None = Field(default=None, ge=0)
    #: When the assembled bytes stop being downloadable and become reclaimable.
    expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    error: str | None = Field(default=None, max_length=1024)


class ExportJob(ExportJobBase, SQLModel, table=True):
    """A unit of archive-export work whose ``id`` doubles as the ARQ job id."""

    __tablename__ = prefixed_tables("export_job")
    __table_args__ = (
        {"mysql_engine": settings.DB_ENGINE, "mysql_charset": settings.DB_CHARSET},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
