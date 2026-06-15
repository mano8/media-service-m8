"""Admin routes — superuser-only storage inspection and housekeeping."""

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import SessionDep, StorageDep
from media_service.controllers.admin import AdminController
from media_service.controllers.maintenance import MaintenanceController
from media_service.core.config import settings
from media_service.core.deps import auth
from media_service.schemas.admin import (
    PurgeStaleResponse,
    QuotaUpdateRequest,
    StaleUploadsResponse,
    StorageStatsResponse,
    StorageUsagePublic,
)
from media_service.schemas.maintenance import HardPurgeResponse, OrphanReport


def _all_buckets() -> list[str]:
    """Every configured bucket the reconciler must sweep for orphan bytes."""
    return [
        settings.MINIO_BUCKET_PUBLIC,
        settings.MINIO_BUCKET_PRIVATE,
        settings.MINIO_BUCKET_SENSITIVE,
        settings.MINIO_BUCKET_TEMP,
        settings.MINIO_BUCKET_ARCHIVE,
    ]


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(auth.get_current_active_superuser)],
)


@router.get(
    "/storage/stats",
    response_model=StorageStatsResponse,
    responses=BaseController.get_error_responses(),
)
def get_storage_stats(*, session: SessionDep) -> StorageStatsResponse:
    """Return object counts and bytes grouped by status and category."""
    return AdminController.get_storage_stats(session=session)


@router.get(
    "/uploads/stale",
    response_model=StaleUploadsResponse,
    responses=BaseController.get_error_responses(),
)
def get_stale_uploads(*, session: SessionDep) -> StaleUploadsResponse:
    """List upload sessions that are past expiry and still in INITIATED state."""
    return AdminController.get_stale_uploads(session=session)


@router.post(
    "/uploads/purge-stale",
    response_model=PurgeStaleResponse,
    responses=BaseController.get_error_responses(),
)
def purge_stale_uploads(
    *, session: SessionDep, storage: StorageDep
) -> PurgeStaleResponse:
    """Mark all stale INITIATED sessions as EXPIRED and return the count purged."""
    return AdminController.purge_stale_uploads(session=session, storage=storage)


@router.get(
    "/quotas/{owner_user_id}",
    response_model=StorageUsagePublic,
    responses=BaseController.get_error_responses(),
)
def get_quota(
    *,
    session: SessionDep,
    owner_user_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
) -> StorageUsagePublic:
    """Return storage usage and effective quotas for an owner/tenant scope."""
    return AdminController.get_quota(
        session=session, owner_user_id=owner_user_id, tenant_id=tenant_id
    )


@router.put(
    "/quotas/{owner_user_id}",
    response_model=StorageUsagePublic,
    responses=BaseController.get_error_responses(),
)
def set_quota(
    *,
    session: SessionDep,
    owner_user_id: uuid.UUID,
    body: QuotaUpdateRequest,
    tenant_id: uuid.UUID | None = None,
) -> StorageUsagePublic:
    """Set per-scope quota overrides for an owner/tenant scope."""
    return AdminController.set_quota(
        session=session,
        owner_user_id=owner_user_id,
        update=body,
        tenant_id=tenant_id,
    )


@router.get(
    "/maintenance/orphans",
    response_model=OrphanReport,
    responses=BaseController.get_error_responses(),
)
def get_orphans(*, session: SessionDep, storage: StorageDep) -> OrphanReport:
    """Report storage/DB orphans in both directions (read-only, never deletes)."""
    return MaintenanceController.reconcile_orphans(
        session=session,
        storage=storage,
        buckets=_all_buckets(),
        grace=timedelta(minutes=settings.MEDIA_RECONCILE_GRACE_MINUTES),
        limit=settings.MEDIA_RECONCILE_BATCH_LIMIT,
        repair=False,
    )


@router.post(
    "/maintenance/orphans/repair",
    response_model=OrphanReport,
    responses=BaseController.get_error_responses(),
)
def repair_orphans(
    *, session: SessionDep, storage: StorageDep, confirm: bool = False
) -> OrphanReport:
    """Reconcile orphans; delete storage-orphans only when ``confirm=true``.

    Defaults to a dry-run (report-only) so an accidental POST never destroys
    bytes; DB-orphans are *never* deleted regardless of ``confirm``.
    """
    return MaintenanceController.reconcile_orphans(
        session=session,
        storage=storage,
        buckets=_all_buckets(),
        grace=timedelta(minutes=settings.MEDIA_RECONCILE_GRACE_MINUTES),
        limit=settings.MEDIA_RECONCILE_BATCH_LIMIT,
        repair=confirm,
    )


@router.post(
    "/maintenance/purge-expired",
    response_model=HardPurgeResponse,
    responses=BaseController.get_error_responses(),
)
def purge_expired(*, session: SessionDep, storage: StorageDep) -> HardPurgeResponse:
    """Hard-delete soft-deleted objects past the retention window (operator run)."""
    return MaintenanceController.hard_purge_expired(
        session=session,
        storage=storage,
        older_than=timedelta(days=settings.MEDIA_RETENTION_PURGE_DAYS),
        limit=settings.MEDIA_PURGE_BATCH_LIMIT,
    )
