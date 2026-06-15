"""Response schemas for lifecycle / retention maintenance operations."""

import uuid

from sqlmodel import SQLModel


class HardPurgeResponse(SQLModel):
    """Result of a scheduled hard-purge of expired soft-deleted objects."""

    purged: int


class OrphanRecord(SQLModel):
    """A single reconciliation mismatch between storage bytes and DB rows.

    ``object_id``/``owner_user_id`` are populated only for DB-orphans (a row
    whose bytes are missing); for storage-orphans (bytes with no row) they stay
    ``None`` because there is no record to attribute them to.
    """

    bucket: str
    object_key: str
    object_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None


class OrphanReport(SQLModel):
    """Both directions of orphan reconciliation plus any repair outcome.

    DB-orphans are **report-only** (deleting a row is an operator decision);
    only storage-orphans (bytes with no row) are removed, and only when repair
    is explicitly requested.
    """

    db_orphans: list[OrphanRecord]
    storage_orphans: list[OrphanRecord]
    db_orphan_count: int
    storage_orphan_count: int
    repaired: int
