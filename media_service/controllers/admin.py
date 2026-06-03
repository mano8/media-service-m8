"""Business logic for admin endpoints: storage stats and stale upload management."""

from datetime import datetime

from sqlalchemy import func, update
from sqlmodel import Session, col, select

from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
)
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus
from media_service.schemas.admin import (
    PurgeStaleResponse,
    StaleUploadSession,
    StaleUploadsResponse,
    StorageStatsByCategory,
    StorageStatsByStatus,
    StorageStatsResponse,
)


class AdminController:
    """Superuser-only operations for storage inspection and housekeeping."""

    @staticmethod
    def get_storage_stats(*, session: Session) -> StorageStatsResponse:
        """Return object counts and byte totals grouped by status and category."""
        by_status_rows = session.exec(
            select(
                MediaObject.status,
                func.count().label("count"),
                func.coalesce(func.sum(MediaObject.size_bytes), 0).label("total_bytes"),
            )
            .where(col(MediaObject.deleted_at).is_(None))
            .group_by(MediaObject.status)
        ).all()

        by_category_rows = session.exec(
            select(
                MediaObject.category,
                func.count().label("count"),
                func.coalesce(func.sum(MediaObject.size_bytes), 0).label("total_bytes"),
            )
            .where(col(MediaObject.deleted_at).is_(None))
            .group_by(MediaObject.category)
        ).all()

        deleted_count = (
            session.scalar(
                select(func.count())
                .select_from(MediaObject)
                .where(col(MediaObject.deleted_at).isnot(None))
            )
            or 0
        )

        by_status = [
            StorageStatsByStatus(
                status=MediaObjectStatus(status),
                count=count,
                total_bytes=total_bytes,
            )
            for status, count, total_bytes in by_status_rows
        ]
        by_category = [
            StorageStatsByCategory(
                category=MediaCategory(category),
                count=count,
                total_bytes=total_bytes,
            )
            for category, count, total_bytes in by_category_rows
        ]

        return StorageStatsResponse(
            by_status=by_status,
            by_category=by_category,
            total_objects=sum(s.count for s in by_status),
            total_bytes=sum(s.total_bytes for s in by_status),
            deleted_objects=deleted_count,
        )

    @staticmethod
    def get_stale_uploads(*, session: Session) -> StaleUploadsResponse:
        """Return upload sessions that are past expiry and still INITIATED."""
        now = datetime.utcnow()
        sessions = session.exec(
            select(UploadSession).where(
                UploadSession.status == UploadSessionStatus.INITIATED,  # type: ignore[arg-type]
                col(UploadSession.expires_at) < now,
            )
        ).all()
        return StaleUploadsResponse(
            count=len(sessions),
            sessions=[
                StaleUploadSession(
                    id=s.id,
                    owner_user_id=s.owner_user_id,
                    category=MediaCategory(s.category),
                    visibility=MediaVisibility(s.visibility),
                    storage_bucket=s.storage_bucket,
                    object_key=s.object_key,
                    expires_at=s.expires_at,
                    created_at=s.created_at,
                )
                for s in sessions
            ],
        )

    @staticmethod
    def purge_stale_uploads(*, session: Session) -> PurgeStaleResponse:
        """Mark all stale INITIATED sessions as EXPIRED. Returns the count purged."""
        now = datetime.utcnow()
        result = session.execute(
            update(UploadSession)
            .where(
                UploadSession.status == UploadSessionStatus.INITIATED,  # type: ignore[arg-type]
                col(UploadSession.expires_at) < now,
            )
            .values(status=UploadSessionStatus.EXPIRED)
        )
        session.commit()
        return PurgeStaleResponse(purged=result.rowcount)  # type: ignore[attr-defined]
