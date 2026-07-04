"""Storage accounting and quota enforcement.

All byte/object accounting flows through this module so the running totals in
``StorageUsage`` stay consistent across every path that adds (upload complete)
or removes (delete, purge) stored bytes. Enforcement reads the same totals at
``initiate_upload`` time and refuses a projected overflow before a presigned
URL is ever handed out.
"""

import uuid

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from media_service.core.config import settings
from media_service.db_models.media_objects import utcnow
from media_service.db_models.storage_usage import StorageUsage
from media_service.metrics import inc_quota_rejected


def _find_usage(
    session: Session, *, owner_user_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> StorageUsage | None:
    """Return the usage row for a scope, or ``None`` if none exists yet.

    ``tenant_id IS NULL`` is matched explicitly so the single global-tenant row
    is always reused rather than treated as distinct (SQL NULL semantics).
    """
    statement = select(StorageUsage).where(
        col(StorageUsage.owner_user_id) == owner_user_id
    )
    if tenant_id is None:
        statement = statement.where(col(StorageUsage.tenant_id).is_(None))
    else:
        statement = statement.where(col(StorageUsage.tenant_id) == tenant_id)
    return session.exec(statement).first()


def get_or_create_usage(
    session: Session, *, owner_user_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> StorageUsage:
    """Fetch the usage row for a scope, creating an empty one if absent.

    The new row is added to the session but not committed; the caller commits it
    in the same transaction as the state change it accompanies.
    """
    usage = _find_usage(session, owner_user_id=owner_user_id, tenant_id=tenant_id)
    if usage is None:
        usage = StorageUsage(owner_user_id=owner_user_id, tenant_id=tenant_id)
        session.add(usage)
    return usage


def _lock_or_create_usage(
    session: Session, *, owner_user_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> StorageUsage:
    """Fetch the usage row ``FOR UPDATE``, creating and flushing one if absent.

    The row lock serialises concurrent completions for the same scope, so the
    actual-size quota check and its credit below are atomic and two completions
    cannot both pass when only one fits. On backends without row locks (SQLite
    in tests) this degrades to ordinary isolation. A freshly created row is
    flushed so it materialises for the lock the next caller takes.
    """
    statement = select(StorageUsage).where(
        col(StorageUsage.owner_user_id) == owner_user_id
    )
    if tenant_id is None:
        statement = statement.where(col(StorageUsage.tenant_id).is_(None))
    else:
        statement = statement.where(col(StorageUsage.tenant_id) == tenant_id)
    usage = session.exec(statement.with_for_update()).first()
    if usage is None:
        usage = StorageUsage(owner_user_id=owner_user_id, tenant_id=tenant_id)
        session.add(usage)
        session.flush()
    return usage


def effective_quota_bytes(usage: StorageUsage | None) -> int | None:
    """Resolve the byte ceiling: per-scope override else the settings default."""
    if usage is not None and usage.quota_bytes is not None:
        return usage.quota_bytes
    return settings.MEDIA_DEFAULT_QUOTA_BYTES


def effective_quota_objects(usage: StorageUsage | None) -> int | None:
    """Resolve the object-count ceiling: per-scope override else the default."""
    if usage is not None and usage.quota_objects is not None:
        return usage.quota_objects
    return settings.MEDIA_DEFAULT_QUOTA_OBJECTS


def check_quota(
    session: Session,
    *,
    owner_user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    additional_bytes: int,
) -> None:
    """Reject an upload whose projected usage would exceed a quota.

    Read-only: evaluates the current totals plus the declared upload against the
    effective ceilings. Over the byte quota raises 413; over the object-count
    quota raises 409. A missing usage row counts as zero usage.
    """
    usage = _find_usage(session, owner_user_id=owner_user_id, tenant_id=tenant_id)
    current_bytes = usage.total_bytes if usage is not None else 0
    current_count = usage.object_count if usage is not None else 0

    quota_bytes = effective_quota_bytes(usage)
    if quota_bytes is not None and current_bytes + additional_bytes > quota_bytes:
        inc_quota_rejected("bytes")
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Storage byte quota exceeded.",
        )

    quota_objects = effective_quota_objects(usage)
    if quota_objects is not None and current_count + 1 > quota_objects:
        inc_quota_rejected("objects")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Storage object-count quota exceeded.",
        )


def record_object_added(
    session: Session,
    *,
    owner_user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    size_bytes: int,
) -> None:
    """Credit a newly stored object to the scope's running totals (uncommitted)."""
    usage = get_or_create_usage(
        session, owner_user_id=owner_user_id, tenant_id=tenant_id
    )
    usage.total_bytes += size_bytes
    usage.object_count += 1
    usage.updated_at = utcnow()
    session.add(usage)


class QuotaExceededError(Exception):
    """Raised when the *actual* stored size would push a scope past its quota.

    Carries a stable ``reason`` (``"quota_bytes_exceeded"`` /
    ``"quota_objects_exceeded"``) so completion can reject and clean up the
    staged object uniformly. Deliberately not an ``HTTPException``: the upload
    completion path turns it into the same reject flow as other content
    failures, removing the stored bytes rather than leaking them over quota.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def reserve_storage_for_object(
    session: Session,
    *,
    owner_user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    size_bytes: int,
) -> None:
    """Atomically enforce the actual-size quota and credit the stored object.

    Locks the scope's usage row, re-reads the live totals, and refuses (without
    crediting) when the *actual* ``size_bytes`` would exceed the byte or
    object-count ceiling — so a caller can never bypass quota by under-declaring
    ``expected_size_bytes`` at initiate time. On success the bytes/object are
    credited inside the same locked transaction, keeping stored usage the single
    source of truth. Raises :class:`QuotaExceededError` on overflow.
    """
    usage = _lock_or_create_usage(
        session, owner_user_id=owner_user_id, tenant_id=tenant_id
    )

    quota_bytes = effective_quota_bytes(usage)
    if quota_bytes is not None and usage.total_bytes + size_bytes > quota_bytes:
        inc_quota_rejected("bytes")
        raise QuotaExceededError("quota_bytes_exceeded")

    quota_objects = effective_quota_objects(usage)
    if quota_objects is not None and usage.object_count + 1 > quota_objects:
        inc_quota_rejected("objects")
        raise QuotaExceededError("quota_objects_exceeded")

    usage.total_bytes += size_bytes
    usage.object_count += 1
    usage.updated_at = utcnow()
    session.add(usage)


def record_object_removed(
    session: Session,
    *,
    owner_user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    size_bytes: int,
) -> None:
    """Debit a removed object from the scope's totals, clamped at zero.

    Clamping keeps totals from going negative if accounting and storage ever
    drift (e.g. a row deleted out of band), so usage converges back to a sane
    floor instead of underflowing.
    """
    usage = get_or_create_usage(
        session, owner_user_id=owner_user_id, tenant_id=tenant_id
    )
    usage.total_bytes = max(0, usage.total_bytes - size_bytes)
    usage.object_count = max(0, usage.object_count - 1)
    usage.updated_at = utcnow()
    session.add(usage)
