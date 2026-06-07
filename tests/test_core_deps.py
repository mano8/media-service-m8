"""Tests for app/deps.py get_storage."""

from unittest.mock import MagicMock, patch

from media_service.app.deps import get_storage
from media_service.storage.client import ObjectStorage


def test_get_storage_returns_object_storage_instance():
    fake = MagicMock(spec=ObjectStorage)
    with patch("media_service.app.deps.ObjectStorage", return_value=fake):
        result = get_storage()
    assert result is fake
