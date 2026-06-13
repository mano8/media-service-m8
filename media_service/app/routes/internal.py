"""Internal (service-to-service) routes guarded by the shared service token.

The media-worker calls these to report scan verdicts and register the variants
it produced. Every route requires ``Authorization: Bearer <service token>``;
none are user-facing.
"""

import uuid

from fastapi import APIRouter, Depends

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import SessionDep
from media_service.controllers.objects import ObjectsController
from media_service.controllers.variants import VariantsController
from media_service.core.deps import require_service_token
from media_service.db_models.media_objects import MediaObjectPublic
from media_service.schemas.objects import ScanResultRequest
from media_service.schemas.variants import (
    VariantJobPublic,
    VariantJobUpdate,
    VariantPublic,
    VariantRegisterRequest,
)

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_service_token)],
)


@router.post(
    "/objects/{object_id}/scan-result",
    response_model=MediaObjectPublic,
    responses=BaseController.get_error_responses(),
)
def apply_scan_result(
    *,
    session: SessionDep,
    object_id: uuid.UUID,
    body: ScanResultRequest,
) -> MediaObjectPublic:
    """Apply an antivirus verdict to an object (CLEAN→READY, else QUARANTINED)."""
    return ObjectsController.apply_scan_result(
        session=session,
        object_id=object_id,
        scan_status=body.scan_status,
    )


@router.post(
    "/objects/{object_id}/variants",
    response_model=VariantPublic,
    responses=BaseController.get_error_responses(),
)
def register_variant(
    *,
    session: SessionDep,
    object_id: uuid.UUID,
    body: VariantRegisterRequest,
) -> VariantPublic:
    """Register (idempotently) a variant the worker wrote to storage."""
    return VariantsController.register_variant(
        session=session,
        object_id=object_id,
        req=body,
    )


@router.patch(
    "/variant-jobs/{job_id}",
    response_model=VariantJobPublic,
    responses=BaseController.get_error_responses(),
)
def update_variant_job(
    *,
    session: SessionDep,
    job_id: uuid.UUID,
    body: VariantJobUpdate,
) -> VariantJobPublic:
    """Advance a variant job's status/progress from the worker."""
    return VariantsController.update_job_status(
        session=session,
        job_id=job_id,
        req=body,
    )
