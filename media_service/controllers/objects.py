"""Business logic for media object metadata and access URLs."""

import base64
import binascii
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, false, func, or_
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from fastapi_m8 import UserModel

from media_service.controllers.category import (
    assigned_category_refs,
    branch_category_ids,
    category_refs_by_object,
    resolve_category_ids,
)
from media_service.core.config import settings
from media_service.core.outbox import (
    EVENT_OBJECT_DELETED,
    EVENT_OBJECT_READY,
    EVENT_SCAN_FAILED,
    record_event,
)
from media_service.core.quotas import record_object_removed
from media_service.core.tenancy import user_tenant_id
from media_service.db_models.media_object_categories import MediaObjectCategoryLink
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectPublic,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
    utcnow,
)
from media_service.schemas.objects import (
    DownloadNotAvailableDetail,
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
    "original_filename": func.coalesce(MediaObject.original_filename, ""),
    "category": MediaObject.category,
    "status": MediaObject.status,
    "created_at": MediaObject.created_at,
    "size_bytes": MediaObject.size_bytes,
}


def _download_not_available_message(scan_status: ScanStatus) -> str:
    """Human copy for the download-guard 409, distinct for a scan rejection."""
    if scan_status in (ScanStatus.INFECTED, ScanStatus.QUARANTINED):
        return "Object is not available for download: it failed the virus scan."
    return "Object is not available for download until it passes scanning."


def _cursor_sort_value(*, sort_by: str, obj: MediaObject) -> Any:
    """Return the cursor-safe object value for a supported sort field."""
    value = getattr(obj, sort_by)
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return ""
    return getattr(value, "value", value)


def _sort_column(sort_by: str) -> Any:
    """Return the SQL expression used for sorting."""
    column = _SORT_COLUMNS[sort_by]
    if sort_by == "original_filename":
        return column
    return col(column)


def _encode_cursor(*, sort_by: str, obj: MediaObject) -> str:
    """Encode the (sort_value, id) pair of an object into an opaque cursor."""
    raw = _cursor_sort_value(sort_by=sort_by, obj=obj)
    payload = json.dumps({"v": raw, "id": str(obj.id)})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(*, sort_by: str, cursor: str) -> tuple[Any, uuid.UUID]:
    """Decode an opaque cursor back into a (sort_value, id) pair."""
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
        payload = json.loads(decoded)
        last_id = uuid.UUID(str(payload["id"]))
        raw = payload["v"]
        decoded_value: Any
        if sort_by == "created_at":
            decoded_value = datetime.fromisoformat(raw)
        elif sort_by == "size_bytes":
            decoded_value = int(raw)
        else:
            decoded_value = str(raw)
        value = decoded_value
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
    current_user: UserModel | None, params: ObjectListParams
) -> SelectOfScalar[MediaObject]:
    """Build the base query with owner scoping and soft-delete handling.

    ``current_user is None`` is the anonymous caller (A16): the listing narrows
    to live ``PUBLIC`` rows and nothing else, so the public read surface can
    never widen past what an unauthenticated visitor is entitled to see.
    """
    statement = select(MediaObject)
    if current_user is None:
        return statement.where(
            col(MediaObject.visibility) == MediaVisibility.PUBLIC,
            col(MediaObject.deleted_at).is_(None),
        )
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
    user_tenant = user_tenant_id(current_user)
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


def _apply_category_filter(
    session: Session,
    current_user: UserModel | None,
    statement: SelectOfScalar[MediaObject],
    params: ObjectListParams,
) -> SelectOfScalar[MediaObject]:
    """Narrow the listing to a user-category branch, or to unfiled media.

    Applied **after** ``_scoped_query`` and only ever as an additional
    ``where`` over a correlated ``EXISTS`` on the link table (`U4`): every
    clause here can subtract rows from what the caller was already entitled to
    see and none can add one, so the branch filter is structurally incapable of
    widening visibility — an anonymous caller passing ``category_id`` still
    sees at most the public catalogue.

    It is separate from :func:`_apply_filters` because it is the one filter
    that needs a session and a principal: which categories a branch covers is a
    scoped question, answered by
    :func:`media_service.controllers.category.branch_category_ids`, not a
    column comparison.

    ``EXISTS`` rather than a join: an object filed into several categories of
    the same branch must appear once, and a join would return it once per
    matching link row, breaking both ``count`` and the keyset cursor.
    """
    filed = select(MediaObjectCategoryLink).where(
        col(MediaObjectCategoryLink.media_object_id) == col(MediaObject.id)
    )
    if params.uncategorized:
        return statement.where(~filed.exists())
    if params.category_id is None:
        return statement
    branch_ids = branch_category_ids(
        session,
        current_user,
        params.category_id,
        include_descendants=params.include_descendants,
    )
    if not branch_ids:
        # Only the anonymous caller reaches this: an authenticated one either
        # resolves the branch or is refused 404/403 above. Say "no rows"
        # explicitly rather than leaning on an empty ``IN ()``.
        return statement.where(false())
    return statement.where(
        filed.where(col(MediaObjectCategoryLink.category_id).in_(branch_ids)).exists()
    )


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


def require_visibility_access(obj: MediaObject, current_user: UserModel | None) -> None:
    """Authorize read/download access to ``obj`` by its visibility policy.

    Superusers and the owner always pass. Otherwise: ``PUBLIC`` is readable by
    anyone — including an anonymous caller (A16) — ``TENANT`` only by callers in
    the same (non-null) tenant; ``PRIVATE``/``SENSITIVE`` by nobody else. Raises
    403 when an authenticated caller is denied.

    An anonymous caller is denied with **404, not 403**: a 403 would tell an
    unauthenticated visitor that a given id exists while a missing id answers
    404, turning the public surface into an existence oracle over every private
    object. Authenticated denials keep their 403 — that caller already
    distinguishes the two cases through ``_fetch_object``.
    """
    if current_user is None:
        if obj.visibility == MediaVisibility.PUBLIC:
            return
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media object not found."
        )
    owner_id = uuid.UUID(str(current_user.id))
    if current_user.is_superuser or obj.owner_user_id == owner_id:
        return
    if obj.visibility == MediaVisibility.PUBLIC:
        return
    if obj.visibility == MediaVisibility.TENANT:
        user_tenant = user_tenant_id(current_user)
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
    current_user: UserModel | None,
    object_id: uuid.UUID,
) -> MediaObject:
    """Fetch a MediaObject for read/download, enforcing visibility access.

    Read path only. Widening this to admit anonymous ``PUBLIC`` reads must never
    widen a write: ``_load_object`` above stays owner-or-superuser and takes a
    non-optional principal, so a public object can be read by a stranger and
    still not be patched or deleted by one.
    """
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
        current_user: UserModel | None,
        params: ObjectListParams,
    ) -> ObjectListResponse:
        """Return a filtered, cursor-paginated page of media objects.

        ``current_user is None`` lists the public catalogue (A16). The
        user-category filters are applied last, after the visibility scoping
        they may only narrow — see :func:`_apply_category_filter`.
        """
        statement = _apply_filters(_scoped_query(current_user, params), params)
        statement = _apply_category_filter(session, current_user, statement, params)
        sort_col = _sort_column(params.sort_by)
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
        # One joined load for the whole page, not one query per object (`U4`).
        refs_by_object = category_refs_by_object(
            session, current_user, [o.id for o in items]
        )
        return ObjectListResponse(
            items=[
                MediaObjectPublic.model_validate(
                    o, update={"categories": refs_by_object.get(o.id, [])}
                )
                for o in items
            ],
            next_cursor=next_cursor,
            count=len(items),
        )

    @staticmethod
    def get_object(
        *,
        session: Session,
        current_user: UserModel | None,
        object_id: uuid.UUID,
    ) -> MediaObjectPublic:
        """Return public metadata for a media object."""
        obj = _load_object_for_read(session, current_user, object_id)
        return MediaObjectPublic.model_validate(obj)

    @staticmethod
    def download_url(
        *,
        session: Session,
        current_user: UserModel | None,
        object_id: uuid.UUID,
        storage: ObjectStorage,
    ) -> DownloadUrlResponse:
        """Generate a presigned download URL for a media object."""
        obj = _load_object_for_read(session, current_user, object_id)
        # Bytes are non-downloadable until an antivirus scan clears them; an
        # unscanned/infected object must never hand out a working URL.
        if obj.scan_status != ScanStatus.CLEAN:
            detail = DownloadNotAvailableDetail(
                scan_status=obj.scan_status,
                message=_download_not_available_message(obj.scan_status),
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail.model_dump(mode="json"),
            )
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

        ``category_ids`` replaces the object's whole user-category filing
        (`U4`): it is a set, not a delta, so re-filing and unfiling are the same
        operation. Every id is resolved in the caller's scope *before* any
        relocation runs, so a request that is about to be refused never moves
        bytes first.
        """
        obj = _load_object(session, current_user, object_id)
        update_data = update.model_dump(exclude_unset=True)
        # Not a column on MediaObject — the filing is link rows — so it comes
        # out before ``sqlmodel_update`` sees it.
        category_ids = update_data.pop("category_ids", None)
        categories = (
            resolve_category_ids(session, current_user, category_ids)
            if category_ids is not None
            else None
        )
        old_bucket = _relocate_for_visibility(
            storage, obj, update_data.get("visibility")
        )
        new_bucket = obj.storage_bucket
        object_key = obj.object_key
        obj.sqlmodel_update(update_data)
        if categories is not None:
            obj.user_categories = list(categories)
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
        return MediaObjectPublic.model_validate(
            obj,
            update={
                "categories": assigned_category_refs(
                    session, current_user, obj.user_categories
                )
            },
        )

    @staticmethod
    def apply_scan_result(
        *,
        session: Session,
        object_id: uuid.UUID,
        scan_status: ScanStatus,
    ) -> MediaObjectPublic:
        """Apply a worker antivirus verdict to an object (idempotent).

        ``CLEAN`` promotes the object to ``READY`` and downloadable; any other
        verdict (the worker purges infected bytes before calling back) marks it
        ``QUARANTINED``. Re-applying the same verdict is a no-op.
        """
        obj = _fetch_object(session, object_id, include_deleted=True)
        if scan_status == ScanStatus.CLEAN:
            obj.scan_status = ScanStatus.CLEAN
            obj.status = MediaObjectStatus.READY
            record_event(
                session,
                event_type=EVENT_OBJECT_READY,
                object_id=obj.id,
                payload={
                    "status": str(obj.status),
                    "scan_status": str(obj.scan_status),
                },
            )
        else:
            obj.scan_status = ScanStatus.QUARANTINED
            record_event(
                session,
                event_type=EVENT_SCAN_FAILED,
                object_id=obj.id,
                payload={"scan_status": str(obj.scan_status)},
            )
        obj.updated_at = utcnow()
        session.add(obj)
        # The outbox row is staged on this session, so it commits atomically with
        # the verdict below (or rolls back with it) — no verdict goes un-notified.
        session.commit()
        session.refresh(obj)
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
        # Notify subscribers in the same transaction as the soft-delete; the early
        # return above keeps a repeat delete from emitting a duplicate event.
        record_event(
            session,
            event_type=EVENT_OBJECT_DELETED,
            object_id=obj.id,
            payload={"visibility": str(obj.visibility)},
        )
        session.commit()
        if obj.visibility == MediaVisibility.PUBLIC:
            _best_effort_remove(
                storage,
                bucket=obj.storage_bucket,
                object_key=obj.object_key,
                context="soft-delete",
            )
