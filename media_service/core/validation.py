"""Content validation helpers for uploaded objects."""

import hashlib
from typing import Optional

import filetype as _filetype

from media_service.core.config import settings


def sniff_mime(head: bytes) -> Optional[str]:
    """Detect MIME type from the leading bytes of a file. Returns None if unrecognised."""
    if not isinstance(head, bytes):
        return None
    kind = _filetype.guess(head)
    return kind.mime if kind else None


def mime_consistent(declared: str, sniffed: Optional[str]) -> bool:
    """Return True when the sniffed type is compatible with the declared type."""
    if sniffed is None:
        return True
    if declared == sniffed:
        return True
    declared_major = declared.split("/")[0]
    sniffed_major = sniffed.split("/")[0]
    if declared_major == sniffed_major and declared_major in {
        "image",
        "video",
        "audio",
    }:
        return True
    return False


def verify_sha256(data: bytes, expected: str) -> bool:
    """Return True when the SHA-256 digest of data matches the expected hex string."""
    return hashlib.sha256(data).hexdigest() == expected.lower()


def max_size_for_category(category: str) -> int:
    """Return the maximum upload size in bytes for the given category."""
    override = settings.MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY
    if override and category in override:
        return override[category]
    return settings.MEDIA_MAX_UPLOAD_SIZE_BYTES
