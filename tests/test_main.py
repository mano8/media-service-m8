"""Tests for media_service/main.py — minio_health_check + metrics endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from fastapi_m8 import HealthStatus

from media_service.main import _register_metrics_endpoint, minio_health_check

_SCRAPE_CRED = "test-scrape-credential-ABC"


@pytest.mark.anyio
async def test_minio_health_check_ok():
    mock_client = MagicMock()
    mock_client.bucket_exists = AsyncMock(return_value=True)
    mock_minio_cls = MagicMock(return_value=mock_client)

    with patch.dict("sys.modules", {"miniopy_async": MagicMock(Minio=mock_minio_cls)}):
        result = await minio_health_check()

    assert result.status == HealthStatus.OK
    assert result.name == "minio"


@pytest.mark.anyio
async def test_minio_health_check_degraded_on_error():
    mock_client = MagicMock()
    mock_client.bucket_exists = AsyncMock(side_effect=ConnectionError("refused"))
    mock_minio_cls = MagicMock(return_value=mock_client)

    with patch.dict("sys.modules", {"miniopy_async": MagicMock(Minio=mock_minio_cls)}):
        result = await minio_health_check()

    assert result.status == HealthStatus.DEGRADED
    assert result.name == "minio"
    assert "refused" in (result.error or "")


def test_register_metrics_endpoint_enabled_serves_metrics():
    router = APIRouter()
    _register_metrics_endpoint(router, enabled=True)

    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get("/metrics")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_register_metrics_endpoint_disabled_registers_nothing():
    router = APIRouter()
    _register_metrics_endpoint(router, enabled=False)

    assert not any(getattr(r, "path", None) == "/metrics" for r in router.routes)


# ── Scrape-credential guard (item 1.4) ───────────────────────────────────────


def test_metrics_guard_no_credential_allows_unauthenticated():
    """credential=None → guard is a no-op; metrics accessible without auth."""
    router = APIRouter()
    _register_metrics_endpoint(router, enabled=True, credential=None)

    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get("/metrics")

    assert resp.status_code == 200


def test_metrics_guard_with_credential_missing_bearer_returns_401():
    """credential set, no Authorization header → 401."""
    router = APIRouter()
    _register_metrics_endpoint(router, enabled=True, credential=_SCRAPE_CRED)

    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app, raise_server_exceptions=False).get("/metrics")

    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_metrics_guard_with_credential_wrong_bearer_returns_401():
    """credential set, wrong bearer token → 401."""
    router = APIRouter()
    _register_metrics_endpoint(router, enabled=True, credential=_SCRAPE_CRED)

    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app, raise_server_exceptions=False).get(
        "/metrics", headers={"Authorization": "Bearer wrong-credential"}
    )

    assert resp.status_code == 401


def test_metrics_guard_with_credential_correct_bearer_returns_200():
    """credential set, correct bearer token → 200."""
    router = APIRouter()
    _register_metrics_endpoint(router, enabled=True, credential=_SCRAPE_CRED)

    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get(
        "/metrics", headers={"Authorization": f"Bearer {_SCRAPE_CRED}"}
    )

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
