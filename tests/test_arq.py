"""Tests for core/arq.py — Redis settings and job enqueue helpers."""

import uuid
from unittest.mock import AsyncMock

import pytest

from media_sdk_m8 import ScanJobPayload, VariantJobPayload, VariantSpec

from media_service.core.arq import (
    SCAN_TASK,
    VARIANTS_TASK,
    enqueue_scan,
    enqueue_variants,
    get_arq_redis_settings,
)


def test_redis_settings_map_media_redis_config():
    from media_service.core.media_redis import get_media_redis_config

    cfg = get_media_redis_config()
    settings = get_arq_redis_settings()
    assert settings.host == cfg.host
    assert settings.port == cfg.port
    assert settings.username == cfg.username
    assert settings.password == cfg.password


@pytest.mark.anyio
async def test_enqueue_scan_uses_scan_task_name():
    pool = AsyncMock()
    payload = ScanJobPayload(
        object_id=uuid.uuid4(),
        bucket="private-media",
        object_key="k",
        owner_user_id=uuid.uuid4(),
    )
    await enqueue_scan(pool, payload)
    pool.enqueue_job.assert_awaited_once_with(SCAN_TASK, payload)


@pytest.mark.anyio
async def test_enqueue_variants_pins_job_id():
    pool = AsyncMock()
    job_id = uuid.uuid4()
    payload = VariantJobPayload(
        job_id=job_id,
        media_object_id=uuid.uuid4(),
        source_bucket="private-media",
        source_object_key="k",
        specs=[
            VariantSpec(
                variant_name="thumb_webp",
                output_options={"name": "thumb_webp"},
                target_bucket="private-media",
                target_key="variants/thumb_webp/x.webp",
            )
        ],
    )
    await enqueue_variants(pool, payload)
    pool.enqueue_job.assert_awaited_once_with(
        VARIANTS_TASK, payload, _job_id=str(job_id)
    )
