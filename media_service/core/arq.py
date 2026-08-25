"""ARQ connection pool for enqueuing background worker jobs.

media-service is the *producer*: it enqueues ``scan_object`` and
``generate_variants`` and ``build_export_archive`` jobs that media-worker-m8
consumes. The
Redis connection reuses the media-owned ``MEDIA_REDIS_*`` settings via
:func:`get_media_redis_config`, so queues share the single media Redis.
"""

from typing import Annotated

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Depends

from media_sdk_m8 import ExportArchiveJobPayload, ScanJobPayload, VariantJobPayload

from media_service.core.media_redis import get_media_redis_config

#: ARQ task names registered by media-worker-m8's ``WorkerSettings``.
SCAN_TASK = "scan_object"
VARIANTS_TASK = "generate_variants"

#: Dedicated queue consumed by the *service-owned* maintenance worker
#: (:mod:`media_service.maintenance_worker`). ARQ defaults every pool to
#: ``arq:queue``, which media-worker-m8 drains — enqueueing a service-owned
#: task there would have media-worker pop a function it does not register and
#: drop it as "function not found", so the queue name is stated here once and
#: read by both the producer below and the worker's ``WorkerSettings``.
MAINTENANCE_QUEUE = "arq:maintenance"

#: ARQ task name registered by media-worker-m8 (`P2 U11`).
EXPORT_ARCHIVE_TASK = "build_export_archive"


def get_arq_redis_settings() -> RedisSettings:
    """Build ARQ ``RedisSettings`` from the media-owned Redis config."""
    config = get_media_redis_config()
    return RedisSettings(
        host=config.host,
        port=config.port,
        username=config.username or None,
        password=config.password,
    )


async def get_arq_pool() -> ArqRedis:  # pragma: no cover
    """FastAPI dependency yielding a live ARQ pool (overridden in tests)."""
    return await create_pool(get_arq_redis_settings())


ArqPoolDep = Annotated[ArqRedis, Depends(get_arq_pool)]


async def enqueue_scan(pool: ArqRedis, payload: ScanJobPayload) -> None:
    """Enqueue an antivirus-scan job for an uploaded object."""
    await pool.enqueue_job(SCAN_TASK, payload)


async def enqueue_variants(pool: ArqRedis, payload: VariantJobPayload) -> None:
    """Enqueue an image-variant job, pinning the ARQ job id to the VariantJob id."""
    await pool.enqueue_job(VARIANTS_TASK, payload, _job_id=str(payload.job_id))


async def enqueue_export_archive(
    pool: ArqRedis, payload: ExportArchiveJobPayload
) -> None:
    """Enqueue delegated archive assembly for ``media-worker-m8``.

    Pins the ARQ job id to the ``ExportJob`` id so a retried enqueue of the
    same job cannot fan out into two assemblies of the same archive, and
    The payload contains only storage references selected under the caller's
    authorized scope, so the DB-free worker makes no authorization or database
    decision.
    """
    await pool.enqueue_job(
        EXPORT_ARCHIVE_TASK,
        payload,
        _job_id=str(payload.job_id),
    )
