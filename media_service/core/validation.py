"""Content validation helpers for uploaded objects."""

import hashlib
import threading
import unicodedata
from collections.abc import Iterable
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


# Process-wide guard bounding concurrent SHA-256 verifications. Each streaming
# verification holds a slot while it reads + hashes an object, so a burst of
# large completions cannot fan out into unbounded concurrent full-object reads.
# Sized from settings once at import (one limit per process).
_VERIFY_SEMAPHORE = threading.BoundedSemaphore(
    settings.MEDIA_SHA256_VERIFY_MAX_CONCURRENCY
)


def sha256_verification_guard() -> threading.BoundedSemaphore:
    """Return the process-wide concurrency guard for SHA-256 verification.

    Use as a context manager around a streaming verification so that no more than
    ``MEDIA_SHA256_VERIFY_MAX_CONCURRENCY`` run at once.
    """
    return _VERIFY_SEMAPHORE


def verify_sha256_stream(chunks: Iterable[bytes], expected: str) -> bool:
    """Return True when the streamed SHA-256 digest matches the expected hex string.

    Hashes incrementally from an iterable of byte chunks so the full object is
    never held in memory at once.
    """
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest() == expected.lower()


def max_size_for_category(category: str) -> int:
    """Return the maximum upload size in bytes for the given category."""
    override = settings.MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY
    if override and category in override:
        return override[category]
    return settings.MEDIA_MAX_UPLOAD_SIZE_BYTES


# ── filenames ────────────────────────────────────────────────────────────────
#
# A client-supplied filename is untrusted input that ends up in five different
# sinks — the object key (a URL path on every presigned link), the
# ``Content-Disposition`` header, a zip entry name in an export archive, the
# listing/search surface, and, on the client's own disk, a filesystem path.
# Each sink has its own encoder, but the *policy* on what a name may contain
# is decided once, here, at the trust boundary (``SEC-VALIDATE-UNTRUSTED-INPUT``):
# the classic portable-filename rule. A name that passes is safe in every sink
# on every platform; the sink encoders stay as defence in depth.

#: Longest accepted ``original_filename`` — the column width on ``MediaObject``
#: and ``UploadSession`` and the common filesystem component limit.
MAX_FILENAME_LENGTH = 255

#: Characters refused outright, beyond the Unicode-category rule below: the
#: two path separators and the set no mainstream filesystem accepts in a name
#: (Windows' reserved set, which is the superset). ``;`` ``%`` ``#`` are
#: allowed here — legitimate and common — and handled by the sink that cares
#: (``storage/keys.py`` keeps them out of the URL path).
FORBIDDEN_FILENAME_CHARS: frozenset[str] = frozenset('/\\<>:"|?*')

#: What a name is replaced with when nothing usable is left.
FALLBACK_FILENAME = "file"


def is_forbidden_filename_char(ch: str) -> bool:
    """Return True for a character no filename may carry.

    Besides :data:`FORBIDDEN_FILENAME_CHARS`, every Unicode "Other" category
    is refused: ``Cc`` controls (NUL, CR/LF, DEL — header and key injection),
    ``Cf`` format characters (zero-width joiners and the bidi overrides such
    as U+202E that reverse the visible extension of ``photo<U+202E>gnp.exe``),
    ``Cs`` surrogates, ``Co`` private use and ``Cn`` unassigned.
    """
    return ch in FORBIDDEN_FILENAME_CHARS or unicodedata.category(ch).startswith("C")


def validate_filename(value: str) -> str:
    """Return the NFC-normalised, trimmed filename or raise ``ValueError``.

    The reject-side of the policy, for the API boundary (upload initiate,
    metadata update): the caller can rename, so a bad name is a 422 with the
    offending characters named, never a silent rewrite.
    """
    name = unicodedata.normalize("NFC", value).strip()
    if not name or set(name) <= {"."}:
        raise ValueError("filename must not be empty or made only of dots")
    if len(name) > MAX_FILENAME_LENGTH:
        raise ValueError(f"filename is longer than {MAX_FILENAME_LENGTH} characters")
    bad = sorted({ch for ch in name if is_forbidden_filename_char(ch)})
    if bad:
        shown = " ".join(repr(ch) for ch in bad)
        raise ValueError(
            "filename contains characters that are not allowed "
            f'(path separators, < > : " | ? *, or control/format characters): {shown}'
        )
    return name


def sanitize_filename(value: str | None) -> str:
    """Map any string onto a name :func:`validate_filename` accepts.

    The normalise-side of the same policy, for data that is imported rather
    than typed — a manifest row in a transfer archive — where refusing the
    whole document over a name is the wrong trade. Path segments before the
    last separator are dropped (a name is never a path), every forbidden
    character becomes ``_``, and an empty or all-dots result falls back to
    :data:`FALLBACK_FILENAME`. The result always satisfies
    :func:`validate_filename`.
    """
    name = unicodedata.normalize("NFC", value or "").strip()
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if is_forbidden_filename_char(ch) else ch for ch in name)
    name = name.strip()[:MAX_FILENAME_LENGTH].strip()
    if not name or set(name) <= {"."}:
        return FALLBACK_FILENAME
    return name
