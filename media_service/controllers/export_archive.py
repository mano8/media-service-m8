"""Shared archive layout helpers for delegated collection exports (`P2 U11`).

Archive assembly belongs to the DB-free ``media-worker-m8`` process. The
service owns only the authoritative selection and layout: it sanitises every
entry name before placing the immutable storage references in the shared SDK
payload, and the import path reads the same constants back.
"""

from media_service.db_models.media_objects import MediaObject
from media_service.storage.keys import _safe_filename

#: Entry carrying the exported collection's metadata.
MANIFEST_ENTRY = "manifest.json"

#: Directory prefix under which object bytes are filed inside the archive.
FILES_PREFIX = "files"


def archive_entry_name(obj: MediaObject) -> str:
    """Return the traversal-safe in-archive path for one object's bytes."""
    filename = _safe_filename(obj.original_filename or "file")
    return f"{FILES_PREFIX}/{obj.id}/{filename}"
