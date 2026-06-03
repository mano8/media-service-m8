"""media_service entry point.

All CORS, health, metrics middleware, and lifespan wiring are handled
by ``create_app``. Only media-specific additions live here.
"""

from fastapi import APIRouter

from auth_sdk_m8.observability import metrics as _metrics
from fastapi_m8 import (
    AppLifecycle,
    HealthCheckResult,
    HealthConfig,
    HealthStatus,
    create_app,
)
from media_service import metrics as _media_metrics
from media_service.app.main import api_router as domain_router
from media_service.core.config import settings
from media_service.core.deps import auth, engine

_metrics.setup(
    enabled=settings.METRICS_ENABLED,
    groups_str=settings.METRICS_GROUPS,
    api_prefix=settings.API_PREFIX,
)
_media_metrics.setup(enabled=settings.METRICS_ENABLED, api_prefix=settings.API_PREFIX)


async def minio_health_check() -> HealthCheckResult:
    """Check MinIO reachability.

    Returns DEGRADED (not FAIL) on connection errors so a brief storage
    outage doesn't 503 the whole service under LENIENT policy.
    """
    try:
        from miniopy_async import Minio  # noqa: PLC0415

        client = Minio(
            f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_USE_SSL,
        )
        await client.bucket_exists(settings.MINIO_BUCKET_PUBLIC)
        return HealthCheckResult(name="minio", status=HealthStatus.OK)
    except Exception as exc:
        return HealthCheckResult(
            name="minio",
            status=HealthStatus.DEGRADED,
            error=str(exc),
            meta={"host": settings.MINIO_HOST},
        )


api_router = APIRouter(prefix=settings.API_PREFIX)
api_router.include_router(domain_router)

app = create_app(
    settings,
    api_router,
    service_name="media-service-m8",
    service_version="1.0.0",
    health=HealthConfig(checks=[minio_health_check]),
    lifecycle=AppLifecycle(auth_deps=auth, db_engine=engine),
)
