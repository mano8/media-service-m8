"""Business logic for lifecycle, retention and orphan reconciliation.

These are the DB-coupled housekeeping jobs the service-owned arq worker runs on
a schedule (and that superusers can trigger on demand). Sync ``@staticmethod``s
mirroring the rest of the controller layer; the only async surface is
``maintenance_worker``, which calls straight into these.

Two cross-cutting hazards are handled here deliberately:

* **Naive-vs-aware datetimes.** ``DateTime(timezone=True)`` columns read back
  *naive* under SQLite but *aware* under Postgres, so every age comparison is
  pushed into SQL with an **aware** cutoff (``utcnow()``) — never compared in
  Python — to behave identically on both backends.
* **Double quota-debit.** Quota was already debited when the object was
  soft-deleted, so the hard-purge must **not** call ``record_object_removed``
  again.
"""

import logging
from datetime import timedelta

from sqlmodel import Session, col, select

from media_service.controllers.admin import AdminController
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    utcnow,
)
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus
from media_service.schemas.admin import PurgeStaleResponse
from media_service.schemas.maintenance import (
    HardPurgeResponse,
    OrphanRecord,
    OrphanReport,
)
from media_service.storage.client import ObjectStorage

_logger = logging.getLogger(__name__)


class MaintenanceController:
    """Scheduled / on-demand DB-coupled housekeeping for media objects."""

    @staticmethod
    def hard_purge_expired(
        *,
        session: Session,
        storage: ObjectStorage,
        older_than: timedelta,
        limit: int,
    ) -> HardPurgeResponse:
        """Hard-delete soft-deleted objects whose grace period has elapsed.

        This is the *true* hard-delete the API never performs (it only
        soft-deletes). Selects DELETED rows whose ``deleted_at`` is older than
        the cutoff — comparison done in SQL with an aware cutoff — bounded by
        ``limit``. For each, re-asserts the invariant at execution time (a guard
        against a restore landing between SELECT and DELETE), best-effort removes
        the bytes from the bucket *as stored* (archived objects live in their own
        bucket, so the bucket is never re-derived from visibility), then deletes
        the row. Quota is **not** re-debited (already done at soft-delete).
        """
        cutoff = utcnow() - older_than
        candidates = session.exec(
            select(MediaObject)
            .where(
                MediaObject.status == MediaObjectStatus.DELETED,  # type: ignore[arg-type]
                col(MediaObject.deleted_at) < cutoff,
            )
            .order_by(col(MediaObject.deleted_at))
            .limit(limit)
        ).all()

        purged = 0
        for obj in candidates:
            # Re-assert at execution time: a concurrent restore clears
            # ``deleted_at`` / flips the status back, in which case skip it.
            if obj.status != MediaObjectStatus.DELETED or obj.deleted_at is None:
                continue
            MaintenanceController._best_effort_remove(
                storage, bucket=obj.storage_bucket, object_key=obj.object_key
            )
            _logger.info(
                "media.hard_purge",
                extra={
                    "object_id": str(obj.id),
                    "bucket": obj.storage_bucket,
                    "object_key": obj.object_key,
                    "owner_user_id": str(obj.owner_user_id),
                    "deleted_at": obj.deleted_at.isoformat(),
                    "cutoff": cutoff.isoformat(),
                },
            )
            session.delete(obj)
            purged += 1

        session.commit()
        return HardPurgeResponse(purged=purged)

    @staticmethod
    def expire_stale_uploads(
        *, session: Session, storage: ObjectStorage
    ) -> PurgeStaleResponse:
        """Auto-expire stale INITIATED uploads (single code path with admin).

        Thin pass-through to :meth:`AdminController.purge_stale_uploads` so the
        scheduled hourly job and the manual admin endpoint share one
        implementation.
        """
        return AdminController.purge_stale_uploads(session=session, storage=storage)

    @staticmethod
    def reconcile_orphans(
        *,
        session: Session,
        storage: ObjectStorage,
        buckets: list[str],
        grace: timedelta,
        limit: int,
        repair: bool = False,
    ) -> OrphanReport:
        """Reconcile storage bytes against DB rows in both directions.

        * **DB-orphans** — a live row whose bytes are missing (``stat_object``
          fails). Report-only: deleting the row is an operator decision.
        * **Storage-orphans** — a stored key with no row. Removed only when
          ``repair`` is set.

        Rows younger than ``grace`` and keys belonging to still-INITIATED upload
        sessions are excluded so an in-flight upload is never flagged.
        """
        cutoff = utcnow() - grace

        db_orphans: list[OrphanRecord] = []
        live_rows = session.exec(
            select(MediaObject)
            .where(
                col(MediaObject.deleted_at).is_(None),
                col(MediaObject.created_at) < cutoff,
            )
            .order_by(col(MediaObject.created_at))
            .limit(limit)
        ).all()
        for obj in live_rows:
            try:
                storage.stat_object(
                    bucket=obj.storage_bucket, object_key=obj.object_key
                )
            except Exception as exc:  # noqa: BLE001
                _logger.info(
                    "media.orphan.db_row_without_bytes",
                    extra={
                        "object_id": str(obj.id),
                        "bucket": obj.storage_bucket,
                        "object_key": obj.object_key,
                        "error": str(exc),
                    },
                )
                db_orphans.append(
                    OrphanRecord(
                        bucket=obj.storage_bucket,
                        object_key=obj.object_key,
                        object_id=obj.id,
                        owner_user_id=obj.owner_user_id,
                    )
                )

        # Keys still being uploaded (INITIATED) are not orphans yet.
        pending_keys = {
            (s.storage_bucket, s.object_key)
            for s in session.exec(
                select(UploadSession).where(
                    UploadSession.status == UploadSessionStatus.INITIATED  # type: ignore[arg-type]
                )
            ).all()
        }

        storage_orphans: list[OrphanRecord] = []
        repaired = 0
        for bucket in buckets:
            for key in storage.list_object_keys(bucket=bucket):
                if len(storage_orphans) >= limit:
                    break
                if (bucket, key) in pending_keys:
                    continue
                row = session.exec(
                    select(MediaObject).where(
                        MediaObject.storage_bucket == bucket,  # type: ignore[arg-type]
                        MediaObject.object_key == key,  # type: ignore[arg-type]
                    )
                ).first()
                if row is not None:
                    continue
                storage_orphans.append(OrphanRecord(bucket=bucket, object_key=key))
                if repair:
                    MaintenanceController._best_effort_remove(
                        storage, bucket=bucket, object_key=key
                    )
                    repaired += 1

        return OrphanReport(
            db_orphans=db_orphans,
            storage_orphans=storage_orphans,
            db_orphan_count=len(db_orphans),
            storage_orphan_count=len(storage_orphans),
            repaired=repaired,
        )

    @staticmethod
    def _best_effort_remove(
        storage: ObjectStorage, *, bucket: str, object_key: str
    ) -> None:
        """Delete stored bytes, logging and swallowing storage errors."""
        try:
            storage.remove_object(bucket=bucket, object_key=object_key)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "media.maintenance.remove_failed %s/%s: %s", bucket, object_key, exc
            )
