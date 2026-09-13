"""Object-key helpers.

Media metadata should store bucket + object_key, never generated URLs.
"""

from uuid import UUID


def _owner_prefix(owner_user_id: UUID, tenant_id: UUID | None) -> str:
    """Return the tenant/owner key prefix shared by originals and variants."""
    if tenant_id:
        return f"tenants/{tenant_id}/users/{owner_user_id}"
    return f"users/{owner_user_id}"


def _safe_filename(filename: str) -> str:
    """Strip any path and fall back to a stable name for empty/dot segments."""
    base = filename.strip().replace("\\", "/").split("/")[-1]
    # After path-stripping, the last segment can still be empty or a bare
    # dot-reference (e.g. "foo/", "foo/.."). S3 keys are literal so this is not
    # a traversal vector, but it produces cosmetically broken keys; fall back to
    # a stable name so every object resolves to a real, unambiguous key.
    if base in ("", ".", ".."):
        return "file"
    return base


#: Characters that never go into an object-key segment, beyond the path
#: separators :func:`_safe_filename` already strips. The key is a POST form
#: field on upload, so any name lands in storage — but on every download and
#: share link it becomes the URL *path* of a presigned GET, where the S3
#: client percent-encodes each of these (``%3B`` ``%25`` ``%3F`` ``%23``,
#: ``%00`` for NUL), and the hardened stack's Traefik ``encodedCharacters``
#: policy (``docker_compose/*/traefik/traefik.yml``:
#: ``allowEncodedSemicolon/Percent/QuestionMark/Hash/NullCharacter: false``)
#: answers ``400`` before the request reaches storage. Such an object would
#: be fail-closed dead through the public route on any backend. The served
#: filename comes from ``original_filename`` (``storage/presign.py``), never
#: from the key, so substituting here changes nothing a client sees.
_KEY_UNSAFE_CHARS = frozenset(";%?#")


def _key_segment(filename: str) -> str:
    """Return the filename as a key segment the public route can carry.

    Path-strips via :func:`_safe_filename`, then replaces every character in
    :data:`_KEY_UNSAFE_CHARS` and every C0/DEL control character with ``_``
    (one for one, so the segment stays readable in a bucket listing).
    """
    base = _safe_filename(filename)
    return "".join(
        "_" if ch in _KEY_UNSAFE_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F else ch
        for ch in base
    )


def build_object_key(
    *,
    owner_user_id: UUID,
    media_id: UUID,
    category: str,
    filename: str,
    tenant_id: UUID | None = None,
) -> str:
    """Build a stable object key for an original upload."""
    prefix = _owner_prefix(owner_user_id, tenant_id)
    safe_category = category.strip().lower().replace(" ", "_")
    return f"{prefix}/{safe_category}/{media_id}/original/{_key_segment(filename)}"


def build_variant_key(
    *,
    owner_user_id: UUID,
    media_id: UUID,
    category: str,
    variant_name: str,
    filename: str,
    tenant_id: UUID | None = None,
) -> str:
    """Build a stable object key for a generated variant, mirroring originals."""
    prefix = _owner_prefix(owner_user_id, tenant_id)
    safe_category = category.strip().lower().replace(" ", "_")
    safe_variant = variant_name.strip().lower().replace(" ", "_")
    return (
        f"{prefix}/{safe_category}/{media_id}"
        f"/variants/{safe_variant}/{_key_segment(filename)}"
    )


def build_export_archive_key(
    *,
    owner_user_id: UUID,
    job_id: UUID,
    tenant_id: UUID | None = None,
) -> str:
    """Build the object key for an assembled archive export (`U9`).

    Shares the tenant/owner prefix every original and variant uses, so an
    export artefact is filed under the same scope as the media it carries and
    a bucket listing stays readable. The job id is the only variable part —
    an export job assembles exactly once, so the key never collides.
    """
    prefix = _owner_prefix(owner_user_id, tenant_id)
    return f"{prefix}/exports/{job_id}.zip"
