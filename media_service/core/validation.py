"""Content validation helpers for uploaded objects."""

import hashlib
from typing import Optional

import filetype as _filetype

from media_service.core.config import settings

# Content types we accept on upload. This is a positive allowlist: anything not
# listed here is rejected at the boundary. Markup/script-bearing formats
# (image/svg+xml, text/html, application/xhtml+xml, image/svg, text/xml, ...)
# are deliberately excluded — they sniff to None and, if ever served inline,
# enable stored XSS.
ALLOWED_DECLARED_MIME: frozenset[str] = frozenset(
    {
        # images (binary, magic-byte sniffable)
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "image/heic",
        "image/avif",
        # video
        "video/mp4",
        "video/webm",
        "video/quicktime",
        # audio
        "audio/mpeg",
        "audio/ogg",
        "audio/wav",
        "audio/flac",
        # documents / archives (sniffable)
        "application/pdf",
        "application/zip",
        # plain tabular/data text — NOT markup; the sniffer cannot identify these
        "text/plain",
        "text/csv",
        "application/json",
    }
)

# Subset of the allowlist that is text-based: the magic-byte sniffer genuinely
# cannot recognise these, so a sniff result of None is expected and accepted.
# Every other allowed type is binary and MUST sniff to a positive match.
_UNSNIFFABLE_DECLARED: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/csv",
        "application/json",
    }
)

_MEDIA_MAJORS = frozenset({"image", "video", "audio"})


def is_allowed_declared_mime(declared: str) -> bool:
    """Return True when the client-declared content type is on the upload allowlist."""
    return declared in ALLOWED_DECLARED_MIME


def sniff_mime(head: bytes) -> Optional[str]:
    """Detect MIME type from the leading bytes of a file. Returns None if unrecognised."""
    if not isinstance(head, bytes):
        return None
    kind = _filetype.guess(head)
    return kind.mime if kind else None


def mime_consistent(declared: str, sniffed: Optional[str]) -> bool:
    """Return True when the sniffed content is consistent with the declared type.

    Fails closed: a declared type outside the allowlist is rejected, and for
    binary (sniffable) types an unidentified payload (``sniffed is None``) is
    rejected rather than waved through. Only the explicitly text-based formats
    in ``_UNSNIFFABLE_DECLARED`` may legitimately sniff to None.
    """
    if declared not in ALLOWED_DECLARED_MIME:
        return False
    if sniffed is None:
        return declared in _UNSNIFFABLE_DECLARED
    if declared == sniffed:
        return True
    declared_major = declared.split("/")[0]
    sniffed_major = sniffed.split("/")[0]
    return declared_major == sniffed_major and declared_major in _MEDIA_MAJORS


def verify_sha256(data: bytes, expected: str) -> bool:
    """Return True when the SHA-256 digest of data matches the expected hex string."""
    return hashlib.sha256(data).hexdigest() == expected.lower()


def max_size_for_category(category: str) -> int:
    """Return the maximum upload size in bytes for the given category."""
    override = settings.MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY
    if override and category in override:
        return override[category]
    return settings.MEDIA_MAX_UPLOAD_SIZE_BYTES
