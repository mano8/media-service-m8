"""MIME helpers for image-variant processing (imgtools-free)."""

# Raster images the variant pipeline can decode/transform. Vector/markup types
# (e.g. image/svg+xml) are intentionally excluded — they are never processable
# images and are blocked at upload anyway.
_PROCESSABLE_IMAGE_MIMES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "image/heic",
        "image/avif",
    }
)

#: Output format (``ext``) → served content type for a generated variant.
_FORMAT_CONTENT_TYPES: dict[str, str] = {
    "WEBP": "image/webp",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "AVIF": "image/avif",
}


def is_processable_image(mime: str) -> bool:
    """Return True when an object's MIME type can be turned into image variants."""
    return mime in _PROCESSABLE_IMAGE_MIMES


def content_type_for_format(fmt: str) -> str:
    """Return the content type for an output format (``ext``), defaulting binary."""
    return _FORMAT_CONTENT_TYPES.get(fmt.upper(), "application/octet-stream")
