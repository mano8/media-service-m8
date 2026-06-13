"""Business logic for image-variant generation, query, and worker upserts."""

import uuid

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from auth_sdk_m8.schemas.user import UserModel

from media_sdk_m8 import VariantJobPayload

from media_service.controllers.objects import (
    _best_effort_remove,
    _fetch_object,
    _load_object,
    _load_object_for_read,
)
from media_service.core.media_types import is_processable_image
from media_service.core.presets import resolve_presets
from media_service.db_models.media_objects import MediaObjectStatus, utcnow
from media_service.db_models.media_variants import MediaVariant
from media_service.db_models.variant_jobs import VariantJob, VariantJobStatus
from media_service.schemas.variants import (
    VariantGenerateRequest,
    VariantJobPublic,
    VariantJobUpdate,
    VariantListResponse,
    VariantPublic,
    VariantRegisterRequest,
)
from media_service.storage.client import ObjectStorage


def _load_variant_job(
    session: Session, object_id: uuid.UUID, job_id: uuid.UUID
) -> VariantJob:
    """Fetch a VariantJob scoped to its object, 404 on mismatch/missing."""
    job = session.get(VariantJob, job_id)
    if job is None or job.media_object_id != object_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Variant job not found."
        )
    return job


class VariantsController:
    """Handle the producer side of image-variant generation."""

    @staticmethod
    def generate(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
        req: VariantGenerateRequest,
    ) -> tuple[VariantJobPublic, VariantJobPayload]:
        """Create a VariantJob and the payload to enqueue for the worker.

        Rejects objects that are not yet ``UPLOADED`` (409) or are not a
        processable image (422); unknown presets raise 422 in the resolver.
        """
        obj = _load_object(session, current_user, object_id)
        if obj.status != MediaObjectStatus.UPLOADED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Object must be UPLOADED before generating variants.",
            )
        if not is_processable_image(obj.mime_type):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Object is not a processable image.",
            )
        specs = resolve_presets(
            session,
            current_user=current_user,
            names=req.presets,
            media_object=obj,
        )
        job = VariantJob(
            media_object_id=obj.id,
            owner_user_id=obj.owner_user_id,
            tenant_id=obj.tenant_id,
            status=VariantJobStatus.QUEUED,
            requested_presets=req.presets,
            variants_expected=len(specs),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        payload = VariantJobPayload(
            job_id=job.id,
            media_object_id=obj.id,
            source_bucket=obj.storage_bucket,
            source_object_key=obj.object_key,
            specs=specs,
        )
        return VariantJobPublic.model_validate(job), payload

    @staticmethod
    def get_job(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> VariantJobPublic:
        """Return a variant job's progress (owner/superuser only)."""
        _load_object(session, current_user, object_id)
        job = _load_variant_job(session, object_id, job_id)
        return VariantJobPublic.model_validate(job)

    @staticmethod
    def list_variants(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
    ) -> VariantListResponse:
        """List the generated variants for an object the caller may read."""
        _load_object_for_read(session, current_user, object_id)
        rows = list(
            session.exec(
                select(MediaVariant).where(
                    col(MediaVariant.media_object_id) == object_id
                )
            ).all()
        )
        return VariantListResponse(
            items=[VariantPublic.model_validate(r) for r in rows],
            count=len(rows),
        )

    @staticmethod
    def delete_variant(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
        variant_id: uuid.UUID,
        storage: ObjectStorage,
    ) -> None:
        """Delete one variant row and best-effort remove its stored bytes."""
        _load_object(session, current_user, object_id)
        variant = session.get(MediaVariant, variant_id)
        if variant is None or variant.media_object_id != object_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Variant not found."
            )
        session.delete(variant)
        session.commit()
        _best_effort_remove(
            storage,
            bucket=variant.storage_bucket,
            object_key=variant.object_key,
            context="variant-delete",
        )

    @staticmethod
    def register_variant(
        *,
        session: Session,
        object_id: uuid.UUID,
        req: VariantRegisterRequest,
    ) -> VariantPublic:
        """Upsert a worker-written variant, idempotent on (object, variant_name)."""
        _fetch_object(session, object_id, include_deleted=True)
        existing = session.exec(
            select(MediaVariant).where(
                col(MediaVariant.media_object_id) == object_id,
                col(MediaVariant.variant_name) == req.variant_name,
            )
        ).first()
        variant = existing or MediaVariant(
            media_object_id=object_id, variant_name=req.variant_name, size_bytes=0
        )
        variant.storage_bucket = req.storage_bucket
        variant.object_key = req.object_key
        variant.width = req.width
        variant.height = req.height
        variant.size_bytes = req.size_bytes
        variant.format = req.format
        session.add(variant)
        session.commit()
        session.refresh(variant)
        return VariantPublic.model_validate(variant)

    @staticmethod
    def update_job_status(
        *,
        session: Session,
        job_id: uuid.UUID,
        req: VariantJobUpdate,
    ) -> VariantJobPublic:
        """Advance a variant job's status/progress from the worker."""
        job = session.get(VariantJob, job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Variant job not found."
            )
        job.status = req.status
        if req.variants_created is not None:
            job.variants_created = req.variants_created
        if req.error is not None:
            job.error = req.error
        job.updated_at = utcnow()
        session.add(job)
        session.commit()
        session.refresh(job)
        return VariantJobPublic.model_validate(job)
