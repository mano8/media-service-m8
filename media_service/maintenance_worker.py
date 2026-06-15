"""Service-owned arq maintenance worker — the only async surface in the service.

This is a *second run-mode of the same media-service image*: launched as
``arq media_service.maintenance_worker.WorkerSettings`` instead of the web
process. It owns the DB-coupled housekeeping jobs (hard-purge, stale-upload
expiry, orphan reconciliation) that must run on a schedule with direct DB +
storage access — work that does **not** belong in the DB-free, enqueue-driven
media-worker-m8 image.

The FastAPI app stays sync; only this module is async (arq is async by nature).
Each cron opens a short-lived ``engine.session()`` and calls straight into the
sync :class:`MaintenanceController`. Housekeeping is serial and infrequent, so
briefly blocking the event loop in the sync controller is acceptable;
``asyncio.to_thread`` is the noted escape hatch if that ever changes.

Deployed ``replicas: 1`` so arq's cron fires exactly once per schedule.
"""

from datetime import timedelta
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from media_service.controllers.maintenance import MaintenanceController
from media_service.core.arq import get_arq_redis_settings
from media_service.core.config import settings
from media_service.core.deps import engine
from media_service.storage.client import ObjectStorage, get_storage_config


def _all_buckets() -> list[str]:
    """Every configured bucket the reconciler must sweep for orphan bytes."""
    return [
        settings.MINIO_BUCKET_PUBLIC,
        settings.MINIO_BUCKET_PRIVATE,
        settings.MINIO_BUCKET_SENSITIVE,
        settings.MINIO_BUCKET_TEMP,
        settings.MINIO_BUCKET_ARCHIVE,
    ]


async def startup(ctx: dict[str, Any]) -> None:
    """Build the per-process object-storage client once."""
    ctx["storage"] = ObjectStorage(get_storage_config())


async def shutdown(ctx: dict[str, Any]) -> None:
    """Dispose the DB engine connection pool at process exit."""
    engine.dispose()


async def hard_purge_expired(ctx: dict[str, Any]) -> int:
    """Cron body: hard-delete soft-deleted objects past the retention window."""
    with engine.session() as session:
        result = MaintenanceController.hard_purge_expired(
            session=session,
            storage=ctx["storage"],
            older_than=timedelta(days=settings.MEDIA_RETENTION_PURGE_DAYS),
            limit=settings.MEDIA_PURGE_BATCH_LIMIT,
        )
    return result.purged


async def expire_stale_uploads(ctx: dict[str, Any]) -> int:
    """Cron body: expire stale INITIATED uploads and drop their orphaned bytes."""
    with engine.session() as session:
        result = MaintenanceController.expire_stale_uploads(
            session=session, storage=ctx["storage"]
        )
    return result.purged


async def reconcile_orphans(ctx: dict[str, Any]) -> int:
    """Cron body: report-only reconciliation across all buckets (no repair)."""
    with engine.session() as session:
        report = MaintenanceController.reconcile_orphans(
            session=session,
            storage=ctx["storage"],
            buckets=_all_buckets(),
            grace=timedelta(minutes=settings.MEDIA_RECONCILE_GRACE_MINUTES),
            limit=settings.MEDIA_RECONCILE_BATCH_LIMIT,
            repair=False,
        )
    return report.db_orphan_count + report.storage_orphan_count


class WorkerSettings:
    """ARQ ``WorkerSettings`` consumed by the ``arq`` CLI (single scheduler)."""

    redis_settings: RedisSettings = get_arq_redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    # Exposed as functions too so an operator can enqueue them on demand.
    functions = [hard_purge_expired, expire_stale_uploads, reconcile_orphans]
    cron_jobs = [
        cron(hard_purge_expired, hour=settings.MEDIA_PURGE_CRON_HOUR, minute=0),
        cron(expire_stale_uploads, minute=settings.MEDIA_STALE_CRON_MINUTE),
        cron(reconcile_orphans, hour=settings.MEDIA_PURGE_CRON_HOUR, minute=30),
    ]
