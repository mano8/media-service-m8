"""Business logic for media object metadata and access URLs."""

import base64
import binascii
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, or_
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from auth_sdk_m8.schemas.user import UserModel

from media_service.core.config import settings
from media_service.core.quotas import record_object_removed
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectPublic,
    MediaObjectStatus,
    MediaVisibility,
    utcnow,
)
from media_service.schemas.objects import (
    DownloadUrlResponse,
    MediaObjectUpdate,
    ObjectListParams,
    ObjectListResponse,
)
from media_service.metrics import inc_download_url_generated
from media_service.storage.buckets import bucket_for_visibility
from media_service.storage.client import ObjectStorage
from media_service.storage.presign import create_download_url

_logger = logging.getLogger(__name__)

_SORT_COLUMNS: dict[str, Any] = {
    "created_at": MediaObject.created_at,
    "size_bytes": MediaObject.size_bytes,
}


def _encode_cursor(*, sort_by: str, obj: MediaObject) -> str:
    """Encode the (sort_value, id) pair of an object into an opaque cursor."""
    value = getattr(obj, sort_by)
    raw = value.isoformat() if isinstance(value, datetime) else value
    payload = json.dumps({"v": raw, "id": str(obj.id)})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(*, sort_by: str, cursor: str) -> tuple[Any, uuid.UUID]:
    """Decode an opaque cursor back into a (sort_value, id) pair."""
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
        payload = json.loads(decoded)
        last_id = uuid.UUID(str(payload["id"]))
        raw = payload["v"]
        value = datetime.fromisoformat(raw) if sort_by == "created_at" else int(raw)
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor."
        ) from exc
    return value, last_id


def _keyset_predicate(
    sort_col: Any, value: Any, last_id: uuid.UUID, *, descending: bool
) -> Any:
    """Build a keyset predicate for rows strictly after the cursor position."""
    id_col = col(MediaObject.id)
    if descending:
        return or_(sort_col < value, and_(sort_col == value, id_col < last_id))
    return or_(sort_col > value, and_(sort_col == value, id_col > last_id))


def _scoped_query(
    current_user: UserModel, params: ObjectListParams
) -> SelectOfScalar[MediaObject]:
    """Build the base query with owner scoping and soft-delete handling."""
    statement = select(MediaObject)
    if current_user.is_superuser:
        if params.owner_user_id is not None:
            statement = statement.where(
                col(MediaObject.owner_user_id) == params.owner_user_id
            )
        if not params.include_deleted:
            statement = statement.where(col(MediaObject.deleted_at).is_(None))
        return statement
    # Non-superusers see objects they are entitled to read: their own, anything
    # PUBLIC, and TENANT objects within their own (non-null) tenant. This mirrors
    # the per-object rule in require_visibility_access so the listing never
    # surfaces a row the caller could not also fetch by id.
    owner_id = uuid.UUID(str(current_user.id))
    visibility_clauses = [
        col(MediaObject.owner_user_id) == owner_id,
        col(MediaObject.visibility) == MediaVisibility.PUBLIC,
    ]
    user_tenant = _user_tenant_id(current_user)
    if user_tenant is not None:
        visibility_clauses.append(
            and_(
                col(MediaObject.visibility) == MediaVisibility.TENANT,
                col(MediaObject.tenant_id) == user_tenant,
            )
        )
    statement = statement.where(or_(*visibility_clauses))
    return statement.where(col(MediaObject.deleted_at).is_(None))


def _apply_filters(
    statement: SelectOfScalar[MediaObject], params: ObjectListParams
) -> SelectOfScalar[MediaObject]:
    """Apply optional attribute filters to the listing query."""
    if params.category is not None:
        statement = statement.where(col(MediaObject.category) == params.category)
    if params.visibility is not None:
        statement = statement.where(col(MediaObject.visibility) == params.visibility)
    if params.status is not None:
        statement = statement.where(col(MediaObject.status) == params.status)
    if params.mime_prefix is not None:
        statement = statement.where(
            col(MediaObject.mime_type).like(f"{params.mime_prefix}%")
        )
    if params.created_from is not None:
        statement = statement.where(col(MediaObject.created_at) >= params.created_from)
    if params.created_to is not None:
        statement = statement.where(col(MediaObject.created_at) <= params.created_to)
    if params.q is not None:
        # autoescape treats %/_ in the user term as literals (no SQLi here since
        # the value is bound, but unescaped wildcards would broaden the match).
        statement = statement.where(
            col(MediaObject.original_filename).contains(params.q, autoescape=True)
        )
    return statement


def _user_tenant_id(current_user: UserModel) -> uuid.UUID | None:
    """Return the caller's tenant as a UUID, or ``None`` when untenanted.

    ``UserModel`` does not (yet) carry a tenant claim, so this reads it
    defensively: callers without a tenant get ``None``, which never matches a
    ``TENANT`` object (see :func:`require_visibility_access`).
    """
    raw = getattr(current_user, "tenant_id", None)
    return uuid.UUID(str(raw)) if raw is not None else None


def _fetch_object(
    session: Session,
    object_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> MediaObject:
    """Load a MediaObject by id, raising 404 for missing/soft-deleted rows."""
    obj = session.get(MediaObject, object_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media object not found."
        )
    if not include_deleted and obj.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media object not found."
        )
    return obj


def require_visibility_access(obj: MediaObject, current_user: UserModel) -> None:
    """Authorize read/download access to ``obj`` by its visibility policy.

    Superusers and the owner always pass. Otherwise: ``PUBLIC`` is readable by
    any authenticated user; ``TENANT`` only by callers in the same (non-null)
    tenant; ``PRIVATE``/``SENSITIVE`` by nobody else. Raises 403 when denied.
    """
    owner_id = uuid.UUID(str(current_user.id))
    if current_user.is_superuser or obj.owner_user_id == owner_id:
        return
    if obj.visibility == MediaVisibility.PUBLIC:
        return
    if obj.visibility == MediaVisibility.TENANT:
        user_tenant = _user_tenant_id(current_user)
        if user_tenant is not None and obj.tenant_id == user_tenant:
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions."
    )


def _load_object(
    session: Session,
    current_user: UserModel,
    object_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> MediaObject:
    """Fetch a MediaObject, enforcing ownership for non-superusers.

    Used by mutating paths (update/delete) where only the owner or a superuser
    may act. Raises 404 for missing records (or soft-deleted unless
    include_deleted=True) and 403 when a non-owner is not a superuser.
    """
    obj = _fetch_object(session, object_id, include_deleted=include_deleted)
    owner_id = uuid.UUID(str(current_user.id))
    if not current_user.is_superuser and obj.owner_user_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions."
        )
    return obj


def _load_object_for_read(
    session: Session,
    current_user: UserModel,
    object_id: uuid.UUID,
) -> MediaObject:
    """Fetch a MediaObject for read/download, enforcing visibility access."""
    obj = _fetch_object(session, object_id)
    require_visibility_access(obj, current_user)
    return obj


def _relocate_for_visibility(
    storage: ObjectStorage,
    obj: MediaObject,
    new_visibility: MediaVisibility | None,
) -> str | None:
    """Copy bytes into the bucket matching a new visibility, repointing ``obj``.

    Keeps stored bytes and ``visibility`` metadata consistent: the copy lands in
    the destination bucket before the metadata is committed. Returns the previous
    bucket (to delete once the commit succeeds) when the object actually moved,
    otherwise ``None``.
    """
    if new_visibility is None or new_visibility == obj.visibility:
        return None
    new_bucket = bucket_for_visibility(new_visibility)
    old_bucket = obj.storage_bucket
    if new_bucket == old_bucket:
        return None
    try:
        storage.copy_object(
            src_bucket=old_bucket,
            src_object_key=obj.object_key,
            dest_bucket=new_bucket,
            dest_object_key=obj.object_key,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to relocate object for the new visibility.",
        ) from exc
    obj.storage_bucket = new_bucket
    return old_bucket


def _best_effort_remove(
    storage: ObjectStorage, *, bucket: str, object_key: str, context: str
) -> None:
    """Best-effort delete of stored bytes; logs and swallows storage errors."""
    try:
        storage.remove_object(bucket=bucket, object_key=object_key)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "Failed to remove object %s/%s (%s): %s",
            bucket,
            object_key,
            context,
            exc,
        )


class ObjectsController:
    """Handle media object metadata and access URLs."""

    @staticmethod
    def list_objects(
        *,
        session: Session,
        current_user: UserModel,
        params: ObjectListParams,
    ) -> ObjectListResponse:
        """Return a filtered, cursor-paginated page of media objects."""
        statement = _apply_filters(_scoped_query(current_user, params), params)
        sort_col = col(_SORT_COLUMNS[params.sort_by])
        id_col = col(MediaObject.id)
        descending = params.order == "desc"
        if params.cursor is not None:
            value, last_id = _decode_cursor(
                sort_by=params.sort_by, cursor=params.cursor
            )
            statement = statement.where(
                _keyset_predicate(sort_col, value, last_id, descending=descending)
            )
        if descending:
            statement = statement.order_by(sort_col.desc(), id_col.desc())
        else:
            statement = statement.order_by(sort_col.asc(), id_col.asc())
        rows = list(session.exec(statement.limit(params.limit + 1)).all())
        has_more = len(rows) > params.limit
        items = rows[: params.limit]
        next_cursor = (
            _encode_cursor(sort_by=params.sort_by, obj=items[-1]) if has_more else None
        )
        return ObjectListResponse(
            items=[MediaObjectPublic.model_validate(o) for o in items],
            next_cursor=next_cursor,
            count=len(items),
        )

    @staticmethod
    def get_object(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
    ) -> MediaObjectPublic:
        """Return public metadata for a media object."""
        obj = _load_object_for_read(session, current_user, object_id)
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
        obj = _load_object_for_read(session, current_user, object_id)
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
        storage: ObjectStorage,
    ) -> MediaObjectPublic:
        """Patch allowed metadata fields on a media object.

        A ``visibility`` change relocates the stored bytes to the matching
        bucket so metadata never diverges from where the object actually lives.
        """
        obj = _load_object(session, current_user, object_id)
        update_data = update.model_dump(exclude_unset=True)
        old_bucket = _relocate_for_visibility(
            storage, obj, update_data.get("visibility")
        )
        new_bucket = obj.storage_bucket
        object_key = obj.object_key
        obj.sqlmodel_update(update_data)
        obj.updated_at = utcnow()
        session.add(obj)
        try:
            session.commit()
        except Exception:
            # The copy already landed in the destination bucket but no metadata
            # now points at it. For a PRIVATE->PUBLIC move that orphan would be
            # world-readable, so best-effort remove it before surfacing the error.
            session.rollback()
            if old_bucket is not None:
                _best_effort_remove(
                    storage,
                    bucket=new_bucket,
                    object_key=object_key,
                    context="visibility-relocation-commit-failure",
                )
            raise
        session.refresh(obj)
        if old_bucket is not None:
            _best_effort_remove(
                storage,
                bucket=old_bucket,
                object_key=obj.object_key,
                context="visibility-relocation",
            )
        return MediaObjectPublic.model_validate(obj)

    @staticmethod
    def delete_object(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
        storage: ObjectStorage,
    ) -> None:
        """Soft-delete a media object (idempotent).

        A PUBLIC object's bytes are world-readable at a known URL, so a metadata-
        only soft-delete would leave them exposed after the user "deleted" them.
        Remove those bytes best-effort; private/sensitive buckets are reachable
        only via presigned URLs, so their metadata soft-delete is sufficient.
        """
        obj = _load_object(session, current_user, object_id, include_deleted=True)
        if obj.deleted_at is not None:
            return
        obj.deleted_at = utcnow()
        obj.status = MediaObjectStatus.DELETED
        obj.updated_at = utcnow()
        session.add(obj)
        # Debit the freed bytes from the owner's totals in the same transaction
        # as the soft-delete, so quota headroom is reclaimed immediately.
        record_object_removed(
            session,
            owner_user_id=obj.owner_user_id,
            tenant_id=obj.tenant_id,
            size_bytes=obj.size_bytes,
        )
        session.commit()
        if obj.visibility == MediaVisibility.PUBLIC:
            _best_effort_remove(
                storage,
                bucket=obj.storage_bucket,
                object_key=obj.object_key,
                context="soft-delete",
            )
