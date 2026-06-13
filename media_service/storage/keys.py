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
    return f"{prefix}/{safe_category}/{media_id}/original/{_safe_filename(filename)}"


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
        f"/variants/{safe_variant}/{_safe_filename(filename)}"
    )
