"""ARQ connection pool for enqueuing background worker jobs.

media-service is the *producer*: it enqueues ``scan_object`` and
``generate_variants`` jobs that media-worker-m8 consumes. The Redis connection
reuses the media-owned ``MEDIA_REDIS_*`` settings via
:func:`get_media_redis_config`, so queues share the single media Redis.
"""

from typing import Annotated

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Depends

from media_sdk_m8 import ScanJobPayload, VariantJobPayload

from media_service.core.media_redis import get_media_redis_config

#: ARQ task names registered by media-worker-m8's ``WorkerSettings``.
SCAN_TASK = "scan_object"
VARIANTS_TASK = "generate_variants"


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
