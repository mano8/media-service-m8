"""Tests for core/media_types.py."""

import pytest

from media_service.core.media_types import (
    content_type_for_format,
    is_processable_image,
)


@pytest.mark.parametrize(
    "mime",
    ["image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"],
)
def test_processable_images(mime):
    assert is_processable_image(mime) is True


@pytest.mark.parametrize(
    "mime",
    ["image/svg+xml", "application/pdf", "text/plain", "video/mp4"],
)
def test_non_processable_types(mime):
    assert is_processable_image(mime) is False


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("WEBP", "image/webp"),
        ("jpeg", "image/jpeg"),
        ("PNG", "image/png"),
        ("gif", "image/gif"),
        ("AVIF", "image/avif"),
    ],
)
def test_content_type_for_known_formats(fmt, expected):
    assert content_type_for_format(fmt) == expected


def test_content_type_for_unknown_format_defaults_to_octet_stream():
    assert content_type_for_format("tiff") == "application/octet-stream"
