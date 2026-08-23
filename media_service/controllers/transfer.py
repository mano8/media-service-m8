"""Business logic for exporting/importing a media collection (`U9`).

Both export formats are resolved here. They share one scoping rule and differ
only in what leaves the service:

``manifest``
    Synchronous. Streams metadata and never touches bytes.
``archive``
    Asynchronous. Creates an
    :class:`~media_service.db_models.export_jobs.ExportJob` that the
    service-owned maintenance worker assembles into a zip
    (:mod:`media_service.controllers.export_archive`), collected afterwards
    from ``GET /media/v1/export/{job_id}`` as a presigned download.

Both reuse the same scoping and filter helpers as the objects list
(``_scoped_query``, ``_apply_filters``, ``_apply_category_filter`` from
:mod:`media_service.controllers.objects`) so a caller cannot export another
tenant's media by passing a foreign `category_id`/`owner_user_id` filter — the
export surface can never see rows the list surface would refuse. The manifest
also reuses
:meth:`media_service.controllers.category.CategoryController.get_category_tree`
for the tree, rather than a second count-rollup implementation.

Every query the manifest needs — resolving the filter (which may 404/403 on a
foreign `category_id`), loading the matching objects, and building the category
tree — runs **before** the response starts streaming
(:meth:`TransferController.export_manifest`). A refusal must come back as a
normal 404/403, not as a corrupted body after a 200 has already gone out; only
the JSON *serialization* of already-fetched rows happens lazily, in
:func:`stream_manifest`, which never runs a query and so can never raise an
``HTTPException``. The archive path resolves the very same filter eagerly for
the same reason: an export that would be refused must be refused at request
time, not by a worker nobody is watching.
"""

import uuid
from collections.abc import Iterator, Mapping, Sequence

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from fastapi_m8 import UserModel

from media_service.controllers.category import (
    CategoryController,
    category_refs_by_object,
)
from media_service.controllers.objects import (
    _apply_category_filter,
    _apply_filters,
    _scoped_query,
)
from media_service.controllers.shares import _as_aware
from media_service.core.config import settings
from media_service.core.tenancy import user_tenant_id
from media_service.db_models.categories import MediaObjectCategoryRef
from media_service.db_models.export_jobs import (
    ACTIVE_EXPORT_STATUSES,
    ExportJob,
    ExportJobStatus,
)
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    ScanStatus,
    utcnow,
)
from media_service.schemas.objects import ObjectListParams
from media_service.schemas.transfer import ExportJobPublic, ManifestObjectEntry
from media_service.storage.client import ObjectStorage
from media_service.storage.presign import create_download_url


def _resolved_filters(filters: ObjectListParams | None) -> ObjectListParams:
    """Default an omitted filter set to "everything in the caller's scope"."""
    return filters if filters is not None else ObjectListParams()


def export_statement(
    session: Session, current_user: UserModel, filters: ObjectListParams
) -> SelectOfScalar[MediaObject]:
    """Build the scoped, filtered query both export formats select from.

    A ``category_id`` outside the caller's scope raises here (via
    ``_apply_category_filter`` -> ``branch_category_ids``), the same 404/403
    the objects list itself would answer — which is why both formats call this
    eagerly rather than deferring it to a generator or to a worker.
    """
    statement = _apply_filters(_scoped_query(current_user, filters), filters)
    return _apply_category_filter(session, current_user, statement, filters)


def with_bytes_only(
    statement: SelectOfScalar[MediaObject],
) -> SelectOfScalar[MediaObject]:
    """Narrow an export query to rows whose bytes may actually be handed out.

    An archive must never carry bytes the download surface itself would refuse:
    :meth:`media_service.controllers.objects.ObjectsController.download_url`
    409s anything whose scan has not cleared, so an unscanned, infected or
    quarantined object would otherwise leave the service inside a zip and
    bypass that gate entirely. Soft-deleted rows are excluded for the same
    reason (a superuser's ``include_deleted`` widens the *manifest*, never the
    bytes).

    The row still appears in the archive's ``manifest.json`` carrying its real
    ``scan_status``, so an import can see exactly why the bytes are missing
    instead of silently receiving a shorter collection.
    """
    return statement.where(
        col(MediaObject.scan_status) == ScanStatus.CLEAN,
        col(MediaObject.status) == MediaObjectStatus.READY,
        col(MediaObject.deleted_at).is_(None),
    )


def _manifest_objects(
    session: Session, current_user: UserModel, filters: ObjectListParams
) -> list[MediaObject]:
    """Return every object matching ``filters`` in the caller's scope, ordered.

    Deliberately not paginated: a manifest exports the whole filtered
    collection, not one page of it, so ``filters.limit``/``filters.cursor`` are
    read by every other export-adjacent surface but not by this one. Ordered by
    id for a deterministic stream.
    """
    statement = export_statement(session, current_user, filters).order_by(
        col(MediaObject.id)
    )
    return list(session.exec(statement).all())


def _manifest_entry(obj: MediaObject, category_paths: list[str]) -> ManifestObjectEntry:
    """Project one ``MediaObject`` row into its manifest shape. No bytes."""
    return ManifestObjectEntry(
        id=obj.id,
        filename=obj.original_filename,
        category=obj.category,
        category_paths=category_paths,
        visibility=obj.visibility,
        size_bytes=obj.size_bytes,
        sha256=obj.sha256,
        mime_type=obj.mime_type,
        status=obj.status,
        scan_status=obj.scan_status,
        created_at=obj.created_at,
        updated_at=obj.updated_at,
    )


def _category_tree_json(session: Session, current_user: UserModel) -> str:
    """Serialize the caller's full category tree, independent of any filter.

    The manifest always carries the whole tree, not just the branches the
    exported objects happen to touch, so an import can recreate the tree
    structure (parents included) even when a filter narrowed the objects.
    Reuses :meth:`CategoryController.get_category_tree` — the same counts the
    ``GET /category/tree/`` endpoint serves — rather than a second rollup.
    """
    tree = CategoryController.get_category_tree(
        session=session, current_user=current_user
    )
    return "[" + ",".join(node.model_dump_json() for node in tree.data) + "]"


def stream_manifest(
    tree_json: str,
    objects: Sequence[MediaObject],
    refs_by_object: Mapping[uuid.UUID, list[MediaObjectCategoryRef]],
) -> Iterator[str]:
    """Yield a ``manifest`` export as a JSON document, one object at a time.

    Takes already-fetched rows, not a session: every query that could refuse
    the request has already run by the time this is called (see the module
    docstring), so this function only serializes and can never raise. Streamed
    rather than built as one Python string up front so response assembly cost
    stays flat per object instead of growing with the whole collection held in
    memory as one string at once.
    """
    yield '{"category_tree":' + tree_json + ',"objects":['
    first = True
    for obj in objects:
        paths = [ref.path for ref in refs_by_object.get(obj.id, [])]
        entry = _manifest_entry(obj, paths)
        prefix = "" if first else ","
        first = False
        yield prefix + entry.model_dump_json()
    yield "]}"


def build_manifest_stream(
    session: Session, current_user: UserModel, filters: ObjectListParams
) -> Iterator[str]:
    """Resolve a manifest export and return its ready-to-serialize generator.

    Shared by the synchronous ``manifest`` response and by the archive
    assembler, which writes the identical document into the zip as
    ``manifest.json`` — the two formats describe a collection the same way or
    an import would have to learn two vocabularies.
    """
    objects = _manifest_objects(session, current_user, filters)
    refs_by_object = category_refs_by_object(
        session, current_user, [obj.id for obj in objects]
    )
    tree_json = _category_tree_json(session, current_user)
    return stream_manifest(tree_json, objects, refs_by_object)


def _export_totals(
    session: Session, statement: SelectOfScalar[MediaObject]
) -> tuple[int, int]:
    """Return ``(objects matched, bytes the archive would carry)``.

    Counted in SQL rather than by materialising the rows: the ceilings exist to
    refuse an export that is too big to assemble, so the check must not itself
    load the collection it is about to refuse. The byte total covers only the
    rows whose bytes are actually included (:func:`with_bytes_only`), so a
    collection of quarantined objects is not refused for weight it would never
    carry.
    """
    matched = session.exec(select(func.count()).select_from(statement.subquery())).one()
    # Summed over the subquery's own column, not ``MediaObject.size_bytes``:
    # referencing the mapped column here would re-add the table to the FROM
    # clause beside the subquery and silently produce a cross join.
    carried = with_bytes_only(statement).subquery()
    total_bytes = session.exec(
        select(func.coalesce(func.sum(carried.c.size_bytes), 0))
    ).one()
    return int(matched), int(total_bytes)


def _assert_within_export_ceilings(matched: int, total_bytes: int) -> None:
    """Refuse an archive the worker should never be asked to build.

    422 rather than 413: the request is well-formed and the caller's fix is to
    narrow the filters, not to shrink a body. Checked before a job row exists,
    so an oversized export never occupies the caller's single in-flight slot
    and never has to be failed by a worker after the fact.
    """
    if matched > settings.MEDIA_EXPORT_MAX_OBJECTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Archive export matches {matched} objects, above the "
                f"{settings.MEDIA_EXPORT_MAX_OBJECTS} ceiling; narrow the filters."
            ),
        )
    if total_bytes > settings.MEDIA_EXPORT_MAX_TOTAL_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Archive export would carry {total_bytes} bytes, above the "
                f"{settings.MEDIA_EXPORT_MAX_TOTAL_BYTES} ceiling; "
                "narrow the filters."
            ),
        )


def _active_export_job(session: Session, owner_id: uuid.UUID) -> ExportJob | None:
    """Return the caller's unfinished export job, if one is already running."""
    return session.exec(
        select(ExportJob).where(
            col(ExportJob.owner_user_id) == owner_id,
            col(ExportJob.status).in_(ACTIVE_EXPORT_STATUSES),
        )
    ).first()


def _load_export_job(
    session: Session, current_user: UserModel, job_id: uuid.UUID
) -> ExportJob:
    """Fetch an export job, enforcing ownership for non-superusers.

    Matches the ownership rule the rest of the owned surface uses
    (:func:`media_service.controllers.objects._load_object`): 404 for a job that
    does not exist, 403 for one that belongs to somebody else.
    """
    job = session.get(ExportJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Export job not found."
        )
    owner_id = uuid.UUID(str(current_user.id))
    if not current_user.is_superuser and job.owner_user_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions."
        )
    return job


def _archive_is_expired(job: ExportJob) -> bool:
    """Whether a completed job's download window has already closed.

    Normalised through the share surface's :func:`_as_aware` rather than a
    second copy of it: a ``DateTime(timezone=True)`` column reads back naive
    under SQLite and aware under Postgres, so comparing the raw value would
    raise on one backend and pass on the other.
    """
    return job.expires_at is not None and _as_aware(job.expires_at) <= utcnow()


class TransferController:
    """Handle export/import of a caller's media collection."""

    @staticmethod
    def export_manifest(
        *,
        session: Session,
        current_user: UserModel,
        filters: ObjectListParams | None,
    ) -> Iterator[str]:
        """Resolve a ``manifest`` export request and return its streamed body.

        Every query — and thus every possible 404/403 from an out-of-scope
        filter — runs here, before the generator is handed to the response, so
        a refusal is a normal error response rather than a truncated 200 body.
        """
        return build_manifest_stream(session, current_user, _resolved_filters(filters))

    @staticmethod
    def start_archive_export(
        *,
        session: Session,
        current_user: UserModel,
        filters: ObjectListParams | None,
    ) -> ExportJobPublic:
        """Create the job that assembles an ``archive`` export off-request.

        Resolves the filter against the caller's scope first (so a foreign
        ``category_id`` is the same 404/403 the manifest answers and never
        becomes a queued job), then bounds the work: one unfinished export per
        caller (409) and the configured object/byte ceilings (422). Only the
        scope the request was authorized under is recorded on the row — the
        assembler re-runs this same query; it does not decide who may see what.
        """
        params = _resolved_filters(filters)
        owner_id = uuid.UUID(str(current_user.id))
        if _active_export_job(session, owner_id) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "An archive export is already in progress; collect or wait "
                    "for it before starting another."
                ),
            )
        statement = export_statement(session, current_user, params)
        matched, total_bytes = _export_totals(session, statement)
        _assert_within_export_ceilings(matched, total_bytes)
        job = ExportJob(
            owner_user_id=owner_id,
            tenant_id=user_tenant_id(current_user),
            is_superuser=bool(current_user.is_superuser),
            filters=params.model_dump(mode="json"),
            object_count=matched,
            total_size_bytes=total_bytes,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return ExportJobPublic.model_validate(job)

    @staticmethod
    def fail_unqueued_export(*, session: Session, job_id: uuid.UUID) -> None:
        """Mark a job failed when its enqueue never reached the broker.

        Without this a Redis outage leaves the row ``queued`` forever: no worker
        will ever claim it, and it holds the caller's single in-flight slot, so
        every later export is refused with a 409 about a job that was never
        running.
        """
        job = session.get(ExportJob, job_id)
        if job is None or job.status != ExportJobStatus.QUEUED:
            return
        job.status = ExportJobStatus.FAILED
        job.error = "Export could not be queued."
        job.updated_at = utcnow()
        session.add(job)
        session.commit()

    @staticmethod
    def get_export_job(
        *,
        session: Session,
        current_user: UserModel,
        storage: ObjectStorage,
        job_id: uuid.UUID,
    ) -> ExportJobPublic:
        """Return an export job's progress, with a download once it is ready.

        The presigned URL is minted per read and never stored: a stored URL
        would keep working after the row said the export had lapsed, and would
        outlive its own signature besides. An expired archive is a 410, not a
        job reported ``completed`` pointing at bytes the reconciler may already
        have reclaimed.
        """
        job = _load_export_job(session, current_user, job_id)
        public = ExportJobPublic.model_validate(job)
        bucket, object_key = job.storage_bucket, job.object_key
        if job.status != ExportJobStatus.COMPLETED or bucket is None:
            # Location columns are unset until assembly finishes, so a job that
            # has not completed simply reports its status with no download.
            return public
        if _archive_is_expired(job):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="The exported archive has expired; start a new export.",
            )
        public.download_url = create_download_url(
            storage=storage,
            bucket=bucket,
            object_key=str(object_key),
            expires_seconds=settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS,
            filename=f"export-{job.id}.zip",
        )
        return public
