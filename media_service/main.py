"""media_service entry point.

All CORS, health, lifespan, and the shared metrics middleware/collectors are
wired by ``create_app`` (which calls ``auth_sdk_m8.observability.metrics.setup``
itself when ``METRICS_ENABLED``). Only media-specific additions live here: the
media-owned counters and the read-only ``/metrics`` endpoint.
"""

from fastapi import APIRouter
from fastapi.responses import Response

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

# Register media-owned counters against the shared REGISTRY. The shared HTTP
# collectors are registered by create_app — registering them here too would
# raise "Duplicated timeseries in CollectorRegistry".
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


def _register_metrics_endpoint(router: APIRouter, *, enabled: bool) -> None:
    """Expose Prometheus metrics under the API prefix when enabled.

    ``create_app`` installs the metrics middleware and registers the shared
    collectors; this only adds the read endpoint that renders ``REGISTRY``
    (shared HTTP metrics plus the media-owned counters).
    """
    if not enabled:
        return

    from auth_sdk_m8.observability.metrics import render as _render_metrics  # noqa: PLC0415

    @router.get("/metrics", include_in_schema=False)
    def metrics_endpoint() -> Response:
        data, content_type = _render_metrics()
        return Response(content=data, media_type=content_type)


api_router = APIRouter(prefix=settings.API_PREFIX)
api_router.include_router(domain_router)
_register_metrics_endpoint(api_router, enabled=settings.METRICS_ENABLED)

app = create_app(
    settings,
    api_router,
    service_name="media-service-m8",
    service_version="1.0.0",
    health=HealthConfig(checks=[minio_health_check]),
    lifecycle=AppLifecycle(auth_deps=auth, db_engine=engine),
)
