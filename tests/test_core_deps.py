"""Tests for app/deps.py get_storage and core/deps.py require_service_token."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from media_service.app.deps import get_storage
from media_service.core.config import settings
from media_service.core.deps import require_service_token
from media_service.storage.client import ObjectStorage

_TOKEN = settings.MEDIA_INTERNAL_SERVICE_TOKEN.get_secret_value()


def test_get_storage_returns_object_storage_instance():
    fake = MagicMock(spec=ObjectStorage)
    with patch("media_service.app.deps.ObjectStorage", return_value=fake):
        result = get_storage()
    assert result is fake


def test_require_service_token_accepts_matching_bearer():
    # Valid token returns None without raising.
    assert require_service_token(authorization=f"Bearer {_TOKEN}") is None


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer wrong-token",
        _TOKEN,  # missing the "Bearer " scheme prefix
        f"bearer {_TOKEN}",  # wrong-case scheme
    ],
)
def test_require_service_token_rejects_bad_headers(header):
    with pytest.raises(HTTPException) as exc:
        require_service_token(authorization=header)
    assert exc.value.status_code == 403
