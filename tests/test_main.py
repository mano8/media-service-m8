"""Tests for media_service/main.py — minio_health_check."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi_m8 import HealthStatus

from media_service.main import minio_health_check


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
