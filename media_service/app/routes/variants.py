"""Routes for image-variant generation and query.

Split the same way as ``objects.py`` (A16): ``router`` carries the writer floor
for the two mutations, ``read_router`` carries the reads. ``read_router`` has no
mounted floor because one of its routes is anonymous-capable — the variants of a
``PUBLIC`` object are as public as the object — so each read states its own tier
in its signature instead.
"""

import uuid

from fastapi import APIRouter, Depends

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import (
    CurrentReader,
    CurrentWriter,
    OptionalPrincipal,
    SessionDep,
    StorageDep,
    require_writer,
)
from media_service.controllers.variants import VariantsController
from media_service.core.arq import ArqPoolDep, enqueue_variants
from media_service.core.rate_limit import RateLimiter
from media_service.schemas.variants import (
    VariantGenerateRequest,
    VariantJobPublic,
    VariantListResponse,
)

read_router = APIRouter(prefix="/objects", tags=["variants"])
router = APIRouter(
    prefix="/objects",
    tags=["variants"],
    dependencies=[Depends(require_writer)],
)

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
    current_user: CurrentWriter,
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


@read_router.get(
    "/{object_id}/variants",
    response_model=VariantListResponse,
    responses=BaseController.get_error_responses(),
)
def list_variants(
    *,
    session: SessionDep,
    current_user: OptionalPrincipal,
    object_id: uuid.UUID,
) -> VariantListResponse:
    """List the generated variants for a media object.

    Public: follows the source object's visibility, anonymous callers included.
    """
    return VariantsController.list_variants(
        session=session,
        current_user=current_user,
        object_id=object_id,
    )


@read_router.get(
    "/{object_id}/variants/jobs/{job_id}",
    response_model=VariantJobPublic,
    responses=BaseController.get_error_responses(),
)
def get_variant_job(
    *,
    session: SessionDep,
    current_user: CurrentReader,
    object_id: uuid.UUID,
    job_id: uuid.UUID,
) -> VariantJobPublic:
    """Return a variant job's progress.

    Owner-scoped, so it carries its own reader tier rather than the public floor
    of the router it is mounted on.
    """
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
    current_user: CurrentWriter,
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
