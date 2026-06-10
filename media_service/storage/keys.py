"""Object-key helpers.

Media metadata should store bucket + object_key, never generated URLs.
"""

from uuid import UUID


def build_object_key(
    *,
    owner_user_id: UUID,
    media_id: UUID,
    category: str,
    filename: str,
    tenant_id: UUID | None = None,
) -> str:
    """Build a stable object key for an original upload."""
    if tenant_id:
        prefix = f"tenants/{tenant_id}/users/{owner_user_id}"
    else:
        prefix = f"users/{owner_user_id}"
    safe_category = category.strip().lower().replace(" ", "_")
    safe_filename = filename.strip().replace("\\", "/").split("/")[-1]
    # After path-stripping, the last segment can still be empty or a bare
    # dot-reference (e.g. "foo/", "foo/.."). S3 keys are literal so this is not
    # a traversal vector, but it produces cosmetically broken keys; fall back to
    # a stable name so every object resolves to a real, unambiguous key.
    if safe_filename in ("", ".", ".."):
        safe_filename = "file"
    return f"{prefix}/{safe_category}/{media_id}/original/{safe_filename}"
