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
    return f"{prefix}/{safe_category}/{media_id}/original/{safe_filename}"
