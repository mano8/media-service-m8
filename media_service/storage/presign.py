"""Presigned URL helpers."""

import re
from urllib.parse import quote

from media_service.storage.client import ObjectStorage

# Control chars, double-quote and backslash must never reach a header value:
# they let a user-supplied filename break out of the quoted parameter or inject
# CR/LF-delimited headers.
_UNSAFE_DISPOSITION_CHARS = re.compile(r'[\x00-\x1f\x7f"\\]')


def _safe_content_disposition(filename: str) -> str:
    """Build an injection-safe ``Content-Disposition`` header value.

    Follows RFC 6266 / RFC 5987: an ASCII ``filename`` fallback with all unsafe
    characters stripped, plus a percent-encoded ``filename*`` for full-fidelity
    UTF-8. User-supplied names can no longer escape the quoted value or smuggle
    additional headers via quotes or CR/LF.
    """
    base = filename.strip().replace("\\", "/").split("/")[-1]
    ascii_fallback = (
        _UNSAFE_DISPOSITION_CHARS.sub("_", base)
        .encode("ascii", "ignore")
        .decode("ascii")
    ) or "download"
    encoded = quote(base, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def create_upload_url(
    *,
    storage: ObjectStorage,
    bucket: str,
    object_key: str,
    content_type: str,
    max_size_bytes: int,
    expires_seconds: int,
) -> tuple[str, dict[str, str]]:
    """Create a presigned upload form (URL + fields) constrained by policy.

    Uses an S3 POST policy rather than a bare presigned PUT so storage enforces
    the size cap and ``Content-Type`` at upload time, instead of letting an
    oversized or garbage object land and be rejected only at ``complete``.
    """
    return storage.presigned_post_object(
        bucket=bucket,
        object_key=object_key,
        content_type=content_type,
        max_size_bytes=max_size_bytes,
        expires_seconds=expires_seconds,
    )


def create_download_url(
    *,
    storage: ObjectStorage,
    bucket: str,
    object_key: str,
    expires_seconds: int,
    filename: str | None = None,
) -> str:
    """Create a presigned download URL."""
    headers = None
    if filename:
        headers = {"response-content-disposition": _safe_content_disposition(filename)}
    return storage.presigned_get_object(
        bucket=bucket,
        object_key=object_key,
        expires_seconds=expires_seconds,
        response_headers=headers,
    )
