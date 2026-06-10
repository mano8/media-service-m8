"""Tests for media_service/metrics.py — unit coverage of all inc_* functions."""

from unittest.mock import MagicMock

import media_service.metrics as metrics_mod


def test_setup_disabled_leaves_all_counters_none():
    metrics_mod.setup(enabled=False, api_prefix="media")
    assert metrics_mod._uploads_initiated is None
    assert metrics_mod._uploads_completed is None
    assert metrics_mod._uploads_failed is None
    assert metrics_mod._uploads_rejected is None
    assert metrics_mod._bytes_uploaded is None
    assert metrics_mod._download_urls_generated is None


def test_inc_upload_initiated_noop_when_disabled():
    metrics_mod._uploads_initiated = None
    metrics_mod.inc_upload_initiated("image", "public")  # must not raise


def test_inc_upload_initiated_calls_labels_inc(monkeypatch):
    mock_counter = MagicMock()
    monkeypatch.setattr(metrics_mod, "_uploads_initiated", mock_counter)
    metrics_mod.inc_upload_initiated("image", "public")
    mock_counter.labels.assert_called_once_with(category="image", visibility="public")
    mock_counter.labels.return_value.inc.assert_called_once()


def test_inc_upload_completed_noop_when_disabled():
    metrics_mod._uploads_completed = None
    metrics_mod._bytes_uploaded = None
    metrics_mod.inc_upload_completed("document", 4096)  # must not raise


def test_inc_upload_completed_increments_both_counters(monkeypatch):
    mock_completed = MagicMock()
    mock_bytes = MagicMock()
    monkeypatch.setattr(metrics_mod, "_uploads_completed", mock_completed)
    monkeypatch.setattr(metrics_mod, "_bytes_uploaded", mock_bytes)
    metrics_mod.inc_upload_completed("document", 4096)
    mock_completed.labels.assert_called_once_with(category="document")
    mock_completed.labels.return_value.inc.assert_called_once()
    mock_bytes.labels.assert_called_once_with(category="document")
    mock_bytes.labels.return_value.inc.assert_called_once_with(4096)


def test_inc_upload_failed_noop_when_disabled():
    metrics_mod._uploads_failed = None
    metrics_mod.inc_upload_failed()  # must not raise


def test_inc_upload_failed_increments_counter(monkeypatch):
    mock_counter = MagicMock()
    monkeypatch.setattr(metrics_mod, "_uploads_failed", mock_counter)
    metrics_mod.inc_upload_failed()
    mock_counter.inc.assert_called_once()


def test_inc_upload_rejected_noop_when_disabled():
    metrics_mod._uploads_rejected = None
    metrics_mod.inc_upload_rejected("mime_mismatch")  # must not raise


def test_inc_upload_rejected_calls_labels_inc(monkeypatch):
    mock_counter = MagicMock()
    monkeypatch.setattr(metrics_mod, "_uploads_rejected", mock_counter)
    metrics_mod.inc_upload_rejected("size_exceeded")
    mock_counter.labels.assert_called_once_with(reason="size_exceeded")
    mock_counter.labels.return_value.inc.assert_called_once()


def test_inc_download_url_generated_noop_when_disabled():
    metrics_mod._download_urls_generated = None
    metrics_mod.inc_download_url_generated()  # must not raise


def test_inc_download_url_generated_increments_counter(monkeypatch):
    mock_counter = MagicMock()
    monkeypatch.setattr(metrics_mod, "_download_urls_generated", mock_counter)
    metrics_mod.inc_download_url_generated()
    mock_counter.inc.assert_called_once()
