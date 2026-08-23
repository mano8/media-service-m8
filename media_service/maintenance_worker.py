"""Service-owned arq maintenance worker — the only async surface in the service.

This is a *second run-mode of the same media-service image*: launched as
``arq media_service.maintenance_worker.WorkerSettings`` instead of the web
process. It owns the DB-coupled housekeeping jobs (hard-purge, stale-upload
expiry, orphan reconciliation) that must run on a schedule with direct DB +
storage access — work that does **not** belong in the DB-free, enqueue-driven
media-worker-m8 image. It also owns the one *enqueued* (non-cron) job of the
same shape: archive-export assembly (`U9`), which needs the database to resolve
the export and storage to stream the bytes it zips.

The FastAPI app stays sync; only this module is async (arq is async by nature).
Each cron opens a short-lived ``engine.session()`` and calls straight into the
sync :class:`MaintenanceController`. Housekeeping is serial and infrequent, so
briefly blocking the event loop in the sync controller is acceptable;
``asyncio.to_thread`` is the noted escape hatch if that ever changes — and
archive assembly, which streams a whole collection through a zip, is exactly
the case that takes it.

Deployed ``replicas: 1`` so arq's cron fires exactly once per schedule.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import httpx
from arq import cron
from arq.connections import RedisSettings

from media_service.controllers.export_archive import ExportArchiveController
from media_service.controllers.maintenance import MaintenanceController
from media_service.controllers.outbox import OutboxDeliveryController
from media_service.core.arq import MAINTENANCE_QUEUE, get_arq_redis_settings
from media_service.core.config import settings
from media_service.core.deps import engine
from media_service.core.ssrf import WebhookPolicy, build_url_guard
from media_service.db_models.media_objects import utcnow
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


async def deliver_outbox(ctx: dict[str, Any]) -> int:
    """Cron body: deliver due PENDING outbox events to matching subscribers.

    DB-heavy (claims/settles rows) and so lands in the service-owned worker, not
    the DB-free media-worker-m8. A short-lived ``httpx.Client`` POSTs each signed
    event; the controller settles every row with retry/backoff in one commit.
    """
    url_guard = build_url_guard(WebhookPolicy.from_settings(settings))
    with engine.session() as session:
        with httpx.Client(timeout=settings.OUTBOX_DELIVERY_TIMEOUT_SECONDS) as client:
            report = OutboxDeliveryController.deliver_pending(
                session=session,
                client=client,
                now=utcnow(),
                limit=settings.OUTBOX_BATCH_LIMIT,
                max_attempts=settings.OUTBOX_MAX_ATTEMPTS,
                backoff_base_seconds=settings.OUTBOX_BACKOFF_BASE_SECONDS,
                url_guard=url_guard,
            )
    return report.delivered


async def build_export_archive(ctx: dict[str, Any], job_id: uuid.UUID) -> int:
    """Enqueued job body: assemble a queued archive export (`U9`).

    Unlike the crons above this is driven by a request — the web process
    enqueues it from ``POST /media/v1/export`` — and it is the one job here
    whose duration scales with the caller's collection rather than with a fixed
    batch limit, so it runs in a worker thread instead of on the event loop.
    Returns the number of files the archive carried (``0`` when the job was
    already claimed, or failed); the row, not this value, is the contract.
    """

    def _run() -> int:
        with engine.session() as session:
            return ExportArchiveController.build(
                session=session, storage=ctx["storage"], job_id=job_id
            )

    return await asyncio.to_thread(_run)


class WorkerSettings:
    """ARQ ``WorkerSettings`` consumed by the ``arq`` CLI (single scheduler)."""

    redis_settings: RedisSettings = get_arq_redis_settings()
    # Dedicated queue: this maintenance pool and media-worker-m8 share the same
    # media Redis. ARQ defaults every pool to ``arq:queue``, so without distinct
    # names the two pools steal each other's jobs — the maintenance worker pops
    # ``scan_object``/``generate_variants`` (which it does not register) and drops
    # them as "function not found", silently breaking the upload scan pipeline,
    # while media-worker pops the maintenance crons. Keep the maintenance crons on
    # their own queue; media-worker + the web producer stay on the default queue.
    queue_name: str = MAINTENANCE_QUEUE
    on_startup = startup
    on_shutdown = shutdown
    # The crons are exposed as functions too, so an operator can enqueue them
    # on demand; ``build_export_archive`` is *only* ever enqueued (by the web
    # process) and therefore has no cron entry.
    functions = [
        hard_purge_expired,
        expire_stale_uploads,
        reconcile_orphans,
        deliver_outbox,
        build_export_archive,
    ]
    cron_jobs = [
        cron(hard_purge_expired, hour=settings.MEDIA_PURGE_CRON_HOUR, minute=0),
        cron(expire_stale_uploads, minute=settings.MEDIA_STALE_CRON_MINUTE),
        cron(reconcile_orphans, hour=settings.MEDIA_PURGE_CRON_HOUR, minute=30),
        # Latency-sensitive: fires once per minute (unlike the housekeeping crons).
        cron(deliver_outbox, second=settings.OUTBOX_DELIVERY_CRON_SECOND),
    ]
