"""Public routes for image-variant generation and query."""

import uuid

from fastapi import APIRouter, Depends

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import CurrentUser, SessionDep, StorageDep
from media_service.controllers.variants import VariantsController
from media_service.core.arq import ArqPoolDep, enqueue_variants
from media_service.core.rate_limit import RateLimiter
from media_service.schemas.variants import (
    VariantGenerateRequest,
    VariantJobPublic,
    VariantListResponse,
)

router = APIRouter(prefix="/objects", tags=["variants"])

_generate_limit = RateLimiter("variants:generate", limit=30, window_seconds=60)


@router.post(
    "/{object_id}/variants:generate",
    response_model=VariantJobPublic,
    status_code=202,
    responses=BaseController.get_error_responses(),
    dependencies=[Depends(_generate_limit)],
)
async def generate_variants(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    arq_pool: ArqPoolDep,
    object_id: uuid.UUID,
    body: VariantGenerateRequest,
) -> VariantJobPublic:
    """Create a variant job and enqueue it for the worker."""
    job_public, payload = VariantsController.generate(
        session=session,
        current_user=current_user,
        object_id=object_id,
        req=body,
    )
    await enqueue_variants(arq_pool, payload)
    return job_public


@router.get(
    "/{object_id}/variants",
    response_model=VariantListResponse,
    responses=BaseController.get_error_responses(),
)
def list_variants(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    object_id: uuid.UUID,
) -> VariantListResponse:
    """List the generated variants for a media object."""
    return VariantsController.list_variants(
        session=session,
        current_user=current_user,
        object_id=object_id,
    )


@router.get(
    "/{object_id}/variants/jobs/{job_id}",
    response_model=VariantJobPublic,
    responses=BaseController.get_error_responses(),
)
def get_variant_job(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    object_id: uuid.UUID,
    job_id: uuid.UUID,
) -> VariantJobPublic:
    """Return a variant job's progress."""
    return VariantsController.get_job(
        session=session,
        current_user=current_user,
        object_id=object_id,
        job_id=job_id,
    )


@router.delete(
    "/{object_id}/variants/{variant_id}",
    response_model=None,
    status_code=204,
    responses=BaseController.get_error_responses(),
)
def delete_variant(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    storage: StorageDep,
    object_id: uuid.UUID,
    variant_id: uuid.UUID,
) -> None:
    """Delete a generated variant and its stored bytes."""
    VariantsController.delete_variant(
        session=session,
        current_user=current_user,
        object_id=object_id,
        variant_id=variant_id,
        storage=storage,
    )
