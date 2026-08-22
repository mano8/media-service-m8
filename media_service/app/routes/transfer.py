"""Routes for exporting a media collection (`U9`).

Mounted on the writer floor (A16): starting an export reads the caller's whole
scoped collection, which is a mutation-adjacent action, not a plain owned read
— matching the floor the rest of `U9`'s transfer surface (import) will need.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from fastapi_m8 import BaseController

from media_service.app.deps import CurrentWriter, SessionDep, require_writer
from media_service.controllers.transfer import TransferController
from media_service.schemas.transfer import ExportRequest

router = APIRouter(
    prefix="/export",
    tags=["transfer"],
    dependencies=[Depends(require_writer)],
)


@router.post(
    "",
    response_model=None,
    responses=BaseController.get_error_responses(),
)
def export_media(
    *,
    session: SessionDep,
    current_user: CurrentWriter,
    body: ExportRequest,
) -> StreamingResponse:
    """Export the caller's scoped media collection as a manifest or archive.

    ``manifest`` streams JSON — metadata only, no bytes. ``archive`` is a
    valid, locked request shape (501 today; assembly is the next `U9` step).
    """
    return StreamingResponse(
        TransferController.export(
            session=session, current_user=current_user, body=body
        ),
        media_type="application/json",
    )
