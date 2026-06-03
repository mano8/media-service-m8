"""Business logic for media object metadata and access URLs."""

import uuid
from datetime import timedelta

from fastapi import HTTPException, status
from sqlmodel import Session

from auth_sdk_m8.schemas.user import UserModel

from media_service.core.config import settings
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectPublic,
    MediaObjectStatus,
    utcnow,
)
from media_service.schemas.objects import DownloadUrlResponse, MediaObjectUpdate
from media_service.metrics import inc_download_url_generated
from media_service.storage.client import ObjectStorage
from media_service.storage.presign import create_download_url


def _load_object(
    session: Session,
    current_user: UserModel,
    object_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> MediaObject:
    """Fetch a MediaObject, enforcing ownership for non-superusers.

    Raises 404 for missing records (or soft-deleted unless include_deleted=True).
    """
    obj = session.get(MediaObject, object_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media object not found."
        )
    if not include_deleted and obj.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media object not found."
        )
    owner_id = uuid.UUID(str(current_user.id))
    if not current_user.is_superuser and obj.owner_user_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions."
        )
    return obj


class ObjectsController:
    """Handle media object metadata and access URLs."""

    @staticmethod
    def get_object(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
    ) -> MediaObjectPublic:
        """Return public metadata for a media object."""
        obj = _load_object(session, current_user, object_id)
        return MediaObjectPublic.model_validate(obj)

    @staticmethod
    def download_url(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
        storage: ObjectStorage,
    ) -> DownloadUrlResponse:
        """Generate a presigned download URL for a media object."""
        obj = _load_object(session, current_user, object_id)
        expires = settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS
        url = create_download_url(
            storage=storage,
            bucket=obj.storage_bucket,
            object_key=obj.object_key,
            expires_seconds=expires,
            filename=obj.original_filename,
        )
        expires_at = utcnow() + timedelta(seconds=expires)
        inc_download_url_generated()
        return DownloadUrlResponse(url=url, expires_at=expires_at)

    @staticmethod
    def update_object(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
        update: MediaObjectUpdate,
    ) -> MediaObjectPublic:
        """Patch allowed metadata fields on a media object."""
        obj = _load_object(session, current_user, object_id)
        update_data = update.model_dump(exclude_unset=True)
        obj.sqlmodel_update(update_data)
        obj.updated_at = utcnow()
        session.add(obj)
        session.commit()
        session.refresh(obj)
        return MediaObjectPublic.model_validate(obj)

    @staticmethod
    def delete_object(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
    ) -> None:
        """Soft-delete a media object (idempotent)."""
        obj = _load_object(session, current_user, object_id, include_deleted=True)
        if obj.deleted_at is not None:
            return
        obj.deleted_at = utcnow()
        obj.status = MediaObjectStatus.DELETED
        obj.updated_at = utcnow()
        session.add(obj)
        session.commit()
