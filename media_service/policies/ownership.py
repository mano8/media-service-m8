"""Ownership policy helpers for media objects."""

from uuid import UUID


def is_owner(*, owner_user_id: UUID, current_user_id: UUID) -> bool:
    """Return whether the current user owns the media object."""
    return owner_user_id == current_user_id
