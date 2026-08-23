"""Routes for exporting and importing a media collection (`U9`).

Split by authorization tier the same way ``objects.py`` and ``variants.py`` are
(A16): ``router`` carries the writer floor because *starting* an export reads
the caller's whole scoped collection and, for an archive, commissions work —
mutation-adjacent, not a plain owned read — while ``read_router`` carries the
reader floor for collecting an export already started. Neither admits an
anonymous caller, so both floors are mounted rather than restated per route.

``import_router`` is the third: a separate router only because it sits under a
different prefix, on the same writer floor as starting an export — an import
creates categories and media in the caller's own collection, which is a
mutation by any reading.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from fastapi_m8 import BaseController

from media_sdk_m8 import ScanJobPayload

from media_service.app.deps import (
    CurrentReader,
    CurrentWriter,
    SessionDep,
    StorageDep,
    require_reader,
    require_writer,
)
from media_service.controllers.transfer import TransferController
from media_service.controllers.transfer_import import ImportController, ImportOutcome
from arq.connections import ArqRedis

from media_service.core.arq import (
    ArqPoolDep,
    enqueue_export_archive,
    enqueue_scan,
)
from media_service.core.rate_limit import RateLimiter
from media_service.schemas.transfer import (
    ExportJobPublic,
    ExportRequest,
    ImportFormat,
    ImportReport,
)

_logger = logging.getLogger(__name__)

read_router = APIRouter(
    prefix="/export",
    tags=["transfer"],
    dependencies=[Depends(require_reader)],
)
router = APIRouter(
    prefix="/export",
    tags=["transfer"],
    dependencies=[Depends(require_writer)],
)
import_router = APIRouter(
    prefix="/import",
    tags=["transfer"],
    dependencies=[Depends(require_writer)],
)

# An import reads a whole uploaded collection, recreates categories and drives
# every archived file through the upload pipeline, so it is far heavier than a
# single upload; the window is correspondingly tighter than the 20/min the
# upload routes carry.
_import_limit = RateLimiter("transfer:import", limit=5, window_seconds=60)


@router.post(
    "",
    response_model=None,
    responses={
        **BaseController.get_error_responses(),
        202: {
            "model": ExportJobPublic,
            "description": "Archive export accepted; collect it from "
            "`GET /media/v1/export/{job_id}`.",
        },
    },
)
async def export_media(
    *,
    session: SessionDep,
    current_user: CurrentWriter,
    arq_pool: ArqPoolDep,
    body: ExportRequest,
) -> StreamingResponse | JSONResponse:
    """Export the caller's scoped media collection as a manifest or archive.

    ``manifest`` streams JSON — metadata only, no bytes — and is answered
    inline. ``archive`` has to read every object out of storage, so it is
    accepted (202) as a job and delegated to media-worker through the shared
    SDK payload.

    The controller work is synchronous and touches the database, so it is run
    in the threadpool rather than on the event loop this ``async`` handler
    needs for the enqueue — the same treatment FastAPI gives a plain ``def``
    handler.
    """
    if body.format == "archive":
        started = await run_in_threadpool(
            TransferController.start_archive_export,
            session=session,
            current_user=current_user,
            filters=body.filters,
        )
        try:
            await enqueue_export_archive(arq_pool, started.payload)
        except Exception as exc:
            # The row is committed but nothing will ever claim it. Fail it here
            # so it does not hold the caller's one in-flight export slot for a
            # job that never started.
            await run_in_threadpool(
                TransferController.fail_unqueued_export,
                session=session,
                job_id=started.job.id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Export could not be queued; try again shortly.",
            ) from exc
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=jsonable_encoder(started.job),
        )
    stream = await run_in_threadpool(
        TransferController.export_manifest,
        session=session,
        current_user=current_user,
        filters=body.filters,
    )
    return StreamingResponse(stream, media_type="application/json")


@read_router.get(
    "/{job_id}",
    response_model=ExportJobPublic,
    responses=BaseController.get_error_responses(),
)
def get_export_job(
    *,
    session: SessionDep,
    current_user: CurrentReader,
    storage: StorageDep,
    job_id: uuid.UUID,
) -> ExportJobPublic:
    """Return an archive export's progress, with a download once it is ready.

    Owner-scoped: a job belonging to somebody else is a 403 and an unknown one
    a 404, matching the rest of the owned surface. The presigned URL appears
    only while the job is ``completed`` and its archive has not lapsed.
    """
    return TransferController.get_export_job(
        session=session,
        current_user=current_user,
        storage=storage,
        job_id=job_id,
    )


@import_router.post(
    "",
    response_model=ImportReport,
    responses=BaseController.get_error_responses(),
    dependencies=[Depends(_import_limit)],
)
async def import_media(
    *,
    session: SessionDep,
    current_user: CurrentWriter,
    storage: StorageDep,
    arq_pool: ArqPoolDep,
    body_format: Annotated[ImportFormat, Form(alias="format")],
    file: Annotated[UploadFile, File()],
) -> ImportReport:
    """Recreate a collection from an exported manifest or archive.

    Multipart rather than a JSON body for both formats, because both are files:
    an export hands the caller a document to keep, and an import takes that same
    document back — one path, one shape, whichever format it holds.

    Answers ``200`` with a per-row report even when individual rows were refused
    — the batch succeeded, and the caller needs to see which objects landed. A
    refusal of the whole document (unreadable, or over an import ceiling) is a
    normal 4xx.

    The controller work is synchronous and touches the database and storage, so
    it runs in the threadpool, leaving this ``async`` handler's event loop for
    the scan enqueues — the same treatment the export route gives its
    controller.
    """
    outcome: ImportOutcome = await run_in_threadpool(
        ImportController.run,
        session=session,
        current_user=current_user,
        storage=storage,
        fmt=body_format,
        source=file.file,
    )
    await _enqueue_import_scans(arq_pool, outcome)
    return outcome.report


async def _enqueue_import_scans(arq_pool: ArqRedis, outcome: ImportOutcome) -> None:
    """Queue an antivirus scan for every object the import created.

    A failed enqueue is reported on its row (``scan_queued: false``) rather than
    failing the whole request: the objects are already committed, and an object
    whose scan never ran stays ``PENDING`` and therefore undownloadable — the
    safe state — so the useful thing to do is tell the caller which rows need
    re-driving, not discard a report for a collection that did land.
    """
    rows = {
        row.media_object_id: row
        for row in outcome.report.objects
        if row.status == "created"
    }
    for media_object in outcome.created_objects:
        try:
            await enqueue_scan(
                arq_pool,
                ScanJobPayload(
                    object_id=media_object.id,
                    bucket=media_object.storage_bucket,
                    object_key=media_object.object_key,
                    owner_user_id=media_object.owner_user_id,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("media.import.scan_not_queued %s: %s", media_object.id, exc)
        else:
            rows[media_object.id].scan_queued = True
