"""Assembly of an ``archive`` export, off the request path (`U9`).

The request path never builds a zip: ``POST /media/v1/export`` with
``format="archive"`` only records an
:class:`~media_service.db_models.export_jobs.ExportJob` and enqueues it. This
module is what the service-owned maintenance worker
(:mod:`media_service.maintenance_worker`) runs afterwards — it has the DB and
storage access the assembly needs, unlike the DB-free media-worker-m8, so the
archive path costs no new cross-service job contract.

Three properties this module exists to hold:

* **Bounded memory.** The zip is written to a temporary *file* and streamed
  into storage from there (:func:`media_service.storage.client.put_object_stream`);
  neither the archive nor any single object is ever resident in memory. Object
  bytes move through in ``MEDIA_EXPORT_STREAM_CHUNK_SIZE`` chunks.
* **No widened scope.** The worker holds no token, so it makes no
  authorization decision: it replays the filter the request was authorized
  under, through the very same
  :func:`media_service.controllers.transfer.export_statement` the manifest
  export and the objects list use, with the scope snapshot recorded on the job
  row. It can only ever see what the requesting principal could already see.
* **No partial success.** The job's location columns are written only after the
  finished archive is in storage, so a storage failure mid-stream leaves a
  ``failed`` job with nothing to download rather than a ``completed`` one
  pointing at a truncated zip.
"""

import logging
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from typing import IO, cast

from sqlmodel import Session, col

from fastapi_m8 import UserModel

from media_service.controllers.transfer import (
    build_manifest_stream,
    export_statement,
    with_bytes_only,
)
from media_service.core.config import settings
from media_service.db_models.export_jobs import ExportJob, ExportJobStatus
from media_service.db_models.media_objects import MediaObject, utcnow
from media_service.schemas.objects import ObjectListParams
from media_service.storage.buckets import StorageClass, bucket_for_storage_class
from media_service.storage.client import ObjectStorage, put_object_stream
from media_service.storage.keys import _safe_filename, build_export_archive_key

_logger = logging.getLogger(__name__)

#: Entry carrying the exported collection's metadata — byte-identical to what
#: the synchronous ``manifest`` export streams, so an import reads one shape.
MANIFEST_ENTRY = "manifest.json"

#: Directory prefix under which object bytes are filed inside the archive.
FILES_PREFIX = "files"

ARCHIVE_CONTENT_TYPE = "application/zip"

#: Cap on the failure text copied onto the job row (the column is 1024).
_MAX_ERROR_CHARS = 500


@dataclass(frozen=True)
class ExportPrincipal:
    """The scope an export job was authorized under, replayed for its query.

    Carries exactly the three attributes the object-scoping helpers read —
    ``id``, ``is_superuser`` and ``tenant_id`` — and nothing else: it is a
    record of a decision already made at the trust boundary, not a principal
    the worker can authenticate or widen. ``UserModel`` cannot stand in here
    because it carries no tenant claim (`D1`/`D4`), which is precisely the
    attribute a tenanted export must not lose.
    """

    id: uuid.UUID
    is_superuser: bool
    tenant_id: uuid.UUID | None

    @classmethod
    def from_job(cls, job: ExportJob) -> "ExportPrincipal":
        """Rebuild the requesting scope from the job row."""
        return cls(
            id=job.owner_user_id,
            is_superuser=job.is_superuser,
            tenant_id=job.tenant_id,
        )

    @property
    def as_user(self) -> UserModel:
        """This snapshot where the scoping helpers expect a ``UserModel``.

        The helpers are annotated for the request-path principal but read only
        the three attributes above, so the cast states the substitution once
        here instead of loosening every signature in
        :mod:`media_service.controllers.objects`.
        """
        return cast(UserModel, self)


def archive_entry_name(obj: MediaObject) -> str:
    """Return the in-archive path for one object's bytes.

    Filed under the object id so two objects sharing a filename cannot collide,
    and the filename is put through the same
    :func:`media_service.storage.keys._safe_filename` sanitiser used for
    storage keys — an entry named ``../../etc/passwd`` is a zip-slip against
    whoever extracts the archive, and the stored ``original_filename`` is
    caller-supplied text.
    """
    filename = _safe_filename(obj.original_filename or "file")
    return f"{FILES_PREFIX}/{obj.id}/{filename}"


def _archivable_objects(
    session: Session, principal: ExportPrincipal, filters: ObjectListParams
) -> list[MediaObject]:
    """Rows whose bytes belong in the archive, in a deterministic order."""
    statement = with_bytes_only(
        export_statement(session, principal.as_user, filters)
    ).order_by(col(MediaObject.id))
    return list(session.exec(statement).all())


def write_archive(
    *,
    session: Session,
    storage: ObjectStorage,
    principal: ExportPrincipal,
    filters: ObjectListParams,
    fh: IO[bytes],
) -> int:
    """Write the whole archive into *fh* and return how many files it carries.

    The manifest is deflated (JSON compresses well); object bytes are stored
    without compression, because media payloads are already compressed and
    deflating them again costs CPU proportional to the collection for close to
    nothing in return.
    """
    chunk_size = settings.MEDIA_EXPORT_STREAM_CHUNK_SIZE
    with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        with zf.open(MANIFEST_ENTRY, "w") as manifest:
            for chunk in build_manifest_stream(session, principal.as_user, filters):
                manifest.write(chunk.encode("utf-8"))
        embedded = 0
        for obj in _archivable_objects(session, principal, filters):
            info = zipfile.ZipInfo(archive_entry_name(obj))
            info.compress_type = zipfile.ZIP_STORED
            with zf.open(info, "w") as dest:
                for chunk_bytes in storage.stream_object(
                    bucket=obj.storage_bucket,
                    object_key=obj.object_key,
                    chunk_size=chunk_size,
                ):
                    dest.write(chunk_bytes)
            embedded += 1
    return embedded


def _claim(session: Session, job_id: uuid.UUID) -> ExportJob | None:
    """Move a ``queued`` job to ``processing``, or decline to run it.

    Anything not ``queued`` is a redelivery of work already claimed, completed
    or failed, and is skipped rather than assembled twice — the maintenance
    worker is deployed single-replica, so this claim needs to be idempotent,
    not distributed-safe.
    """
    job = session.get(ExportJob, job_id)
    if job is None or job.status != ExportJobStatus.QUEUED:
        return None
    job.status = ExportJobStatus.PROCESSING
    job.updated_at = utcnow()
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def _complete(
    session: Session,
    job: ExportJob,
    *,
    bucket: str,
    object_key: str,
    size_bytes: int,
) -> None:
    """Record the assembled archive's location and its download window."""
    job.status = ExportJobStatus.COMPLETED
    job.storage_bucket = bucket
    job.object_key = object_key
    job.size_bytes = size_bytes
    job.expires_at = utcnow() + timedelta(
        seconds=settings.MEDIA_EXPORT_ARCHIVE_TTL_SECONDS
    )
    job.updated_at = utcnow()
    session.add(job)
    session.commit()


def _fail(session: Session, job: ExportJob, exc: Exception) -> None:
    """Mark the job failed, keeping every location column unset.

    The failure text is truncated and carries the exception type rather than a
    raw storage message, so a backend error cannot smuggle bucket names or
    endpoints into a caller-visible field.
    """
    job.status = ExportJobStatus.FAILED
    job.error = f"Archive assembly failed: {type(exc).__name__}"[:_MAX_ERROR_CHARS]
    job.updated_at = utcnow()
    session.add(job)
    session.commit()


def _best_effort_remove(
    storage: ObjectStorage, *, bucket: str, object_key: str
) -> None:
    """Drop a half-written archive, logging and swallowing storage errors."""
    try:
        storage.remove_object(bucket=bucket, object_key=object_key)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "media.export.cleanup_failed %s/%s: %s", bucket, object_key, exc
        )


class ExportArchiveController:
    """Assemble a queued archive export into storage."""

    @staticmethod
    def build(*, session: Session, storage: ObjectStorage, job_id: uuid.UUID) -> int:
        """Assemble one queued export job; return the file count it carried.

        Returns ``0`` for a job that was skipped or failed — the row, not the
        return value, is the contract; the count is the worker's telemetry.
        """
        job = _claim(session, job_id)
        if job is None:
            return 0
        principal = ExportPrincipal.from_job(job)
        filters = ObjectListParams.model_validate(job.filters)
        bucket = bucket_for_storage_class(StorageClass.TEMP)
        object_key = build_export_archive_key(
            owner_user_id=job.owner_user_id,
            job_id=job.id,
            tenant_id=job.tenant_id,
        )
        try:
            with tempfile.TemporaryFile() as fh:
                embedded = write_archive(
                    session=session,
                    storage=storage,
                    principal=principal,
                    filters=filters,
                    fh=fh,
                )
                size_bytes = fh.tell()
                fh.seek(0)
                put_object_stream(
                    storage,
                    bucket=bucket,
                    object_key=object_key,
                    data=fh,
                    length=size_bytes,
                    content_type=ARCHIVE_CONTENT_TYPE,
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("media.export.failed %s: %s", job.id, exc)
            # A failed put can still have left bytes behind; the job will never
            # point at them, so remove them rather than leave the reconciler to
            # find an orphan nobody can explain.
            _best_effort_remove(storage, bucket=bucket, object_key=object_key)
            _fail(session, job, exc)
            return 0
        _complete(
            session,
            job,
            bucket=bucket,
            object_key=object_key,
            size_bytes=size_bytes,
        )
        return embedded
