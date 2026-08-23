"""Routes for exporting a media collection (`U9`).

Split by authorization tier the same way ``objects.py`` and ``variants.py`` are
(A16): ``router`` carries the writer floor because *starting* an export reads
the caller's whole scoped collection and, for an archive, commissions work —
mutation-adjacent, not a plain owned read — while ``read_router`` carries the
reader floor for collecting an export already started. Neither admits an
anonymous caller, so both floors are mounted rather than restated per route.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from fastapi_m8 import BaseController

from media_service.app.deps import (
    CurrentReader,
    CurrentWriter,
    SessionDep,
    StorageDep,
    require_reader,
    require_writer,
)
from media_service.controllers.transfer import TransferController
from media_service.core.arq import ArqPoolDep, enqueue_export_archive
from media_service.schemas.transfer import ExportJobPublic, ExportRequest

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
    accepted (202) as a job and assembled by the worker.

    The controller work is synchronous and touches the database, so it is run
    in the threadpool rather than on the event loop this ``async`` handler
    needs for the enqueue — the same treatment FastAPI gives a plain ``def``
    handler.
    """
    if body.format == "archive":
        job = await run_in_threadpool(
            TransferController.start_archive_export,
            session=session,
            current_user=current_user,
            filters=body.filters,
        )
        try:
            await enqueue_export_archive(arq_pool, job.id)
        except Exception as exc:
            # The row is committed but nothing will ever claim it. Fail it here
            # so it does not hold the caller's one in-flight export slot for a
            # job that never started.
            await run_in_threadpool(
                TransferController.fail_unqueued_export,
                session=session,
                job_id=job.id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Export could not be queued; try again shortly.",
            ) from exc
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content=jsonable_encoder(job)
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
