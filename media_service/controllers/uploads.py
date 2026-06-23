"""Business logic for the presigned upload flow."""

import logging
import uuid
from datetime import timedelta

from fastapi import HTTPException, status
from sqlmodel import Session

from auth_sdk_m8.schemas.user import UserModel

from media_service.core.config import settings
from media_service.core.quotas import check_quota, record_object_added
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectPublic,
    MediaObjectStatus,
)
from media_service.db_models.upload_sessions import UploadSession, UploadSessionStatus
from media_service.db_models.media_objects import utcnow
from media_service.schemas.uploads import (
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadInitiateRequest,
    UploadInitiateResponse,
)
from media_service.core.validation import (
    is_allowed_declared_mime,
    max_size_for_category,
    mime_consistent,
    sha256_verification_guard,
    sniff_mime,
    verify_sha256_stream,
)
from media_service.metrics import (
    inc_upload_completed,
    inc_upload_failed,
    inc_upload_initiated,
    inc_upload_rejected,
)
from media_service.storage.buckets import bucket_for_visibility
from media_service.storage.client import ObjectStorage
from media_service.storage.keys import build_object_key
from media_service.storage.presign import create_upload_url


_logger = logging.getLogger(__name__)


def _load_owned_session(
    session: Session, current_user: UserModel, session_id: uuid.UUID
) -> UploadSession:
    """Fetch an UploadSession, enforcing ownership for non-superusers."""
    upload_session = session.get(UploadSession, session_id)
    if upload_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Upload session not found.",
        )
    owner_id = uuid.UUID(str(current_user.id))
    if not current_user.is_superuser and upload_session.owner_user_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions."
        )
    return upload_session


def _ensure_completable(session: Session, upload_session: UploadSession) -> None:
    """Reject completion if the session is not INITIATED or has expired."""
    if upload_session.status != UploadSessionStatus.INITIATED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Upload session is already {upload_session.status}.",
        )
    now = utcnow().replace(tzinfo=None)
    expires = upload_session.expires_at
    if expires.tzinfo is not None:
        expires = expires.replace(tzinfo=None)
    if now > expires:
        upload_session.status = UploadSessionStatus.EXPIRED
        session.add(upload_session)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload session has expired.",
        )


def _reject_upload(
    *,
    session: Session,
    storage: ObjectStorage,
    upload_session: UploadSession,
    reason: str,
    mime_type: str,
    size_bytes: int,
    etag: str | None,
) -> None:
    """Abort the session, persist a REJECTED MediaObject for audit, and raise 422."""
    media_object = MediaObject(
        id=upload_session.id,
        owner_user_id=upload_session.owner_user_id,
        tenant_id=upload_session.tenant_id,
        category=upload_session.category,
        visibility=upload_session.visibility,
        storage_bucket=upload_session.storage_bucket,
        object_key=upload_session.object_key,
        original_filename=upload_session.original_filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        etag=etag,
        status=MediaObjectStatus.REJECTED,
    )
    upload_session.status = UploadSessionStatus.ABORTED
    session.add(media_object)
    session.add(upload_session)
    session.commit()
    # The REJECTED row keeps the audit trail; the stored bytes must not linger
    # in the (possibly public) bucket. Remove them best-effort, like abort.
    try:
        storage.remove_object(
            bucket=upload_session.storage_bucket,
            object_key=upload_session.object_key,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("storage.remove_object failed during reject: %s", exc)
    inc_upload_rejected(reason)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Upload rejected: {reason}.",
    )


class UploadsController:
    """Handle presigned upload lifecycle: initiate, complete, abort."""

    @staticmethod
    def initiate_upload(
        *,
        session: Session,
        current_user: UserModel,
        req: UploadInitiateRequest,
        storage: ObjectStorage,
    ) -> UploadInitiateResponse:
        """Create an UploadSession and return a presigned PUT URL."""
        if not is_allowed_declared_mime(req.mime_type):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported content type: {req.mime_type}",
            )
        media_id = uuid.uuid4()
        owner_id = uuid.UUID(str(current_user.id))
        # Tenancy is taken from the authenticated principal's signed claim, never
        # from the client request body (which is ignored). Untenanted callers
        # yield None and behave exactly as before. This is what activates TENANT
        # visibility and per-tenant quota scoping end to end.
        tenant_id = current_user.tenant_id
        # Refuse before issuing a presigned URL if the declared upload would push
        # this owner past their byte or object-count quota (scoped per tenant).
        check_quota(
            session,
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            additional_bytes=req.expected_size_bytes,
        )
        object_key = build_object_key(
            owner_user_id=owner_id,
            media_id=media_id,
            category=req.category,
            filename=req.original_filename,
            tenant_id=tenant_id,
        )
        bucket = bucket_for_visibility(req.visibility)
        expires = settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS
        upload_url, upload_fields = create_upload_url(
            storage=storage,
            bucket=bucket,
            object_key=object_key,
            content_type=req.mime_type,
            max_size_bytes=max_size_for_category(str(req.category)),
            expires_seconds=expires,
        )
        expires_at = utcnow() + timedelta(seconds=expires)
        upload_session = UploadSession(
            id=media_id,
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            category=req.category,
            visibility=req.visibility,
            storage_bucket=bucket,
            object_key=object_key,
            original_filename=req.original_filename,
            expected_mime_type=req.mime_type,
            expected_size_bytes=req.expected_size_bytes,
            expires_at=expires_at,
        )
        session.add(upload_session)
        session.commit()
        inc_upload_initiated(str(req.category), str(req.visibility))
        return UploadInitiateResponse(
            session_id=media_id,
            upload_url=upload_url,
            upload_fields=upload_fields,
            expires_at=expires_at,
        )

    @staticmethod
    def complete_upload(
        *,
        session: Session,
        current_user: UserModel,
        session_id: uuid.UUID,
        req: UploadCompleteRequest,
        storage: ObjectStorage,
    ) -> UploadCompleteResponse:
        """Verify the object landed in storage and promote the session to a MediaObject."""
        upload_session = _load_owned_session(session, current_user, session_id)
        _ensure_completable(session, upload_session)

        try:
            stat = storage.stat_object(
                bucket=upload_session.storage_bucket,
                object_key=upload_session.object_key,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Object not found in storage. Upload the file before completing.",
            ) from exc

        declared_mime = upload_session.expected_mime_type
        category = str(upload_session.category)
        _reject_kw = dict(
            session=session,
            storage=storage,
            upload_session=upload_session,
            mime_type=declared_mime,
            size_bytes=stat.size,
            etag=stat.etag,
        )

        # 1. Size enforcement
        if stat.size > max_size_for_category(category):
            _reject_upload(**_reject_kw, reason="size_exceeded")

        # 2. Magic-byte MIME check
        try:
            head = storage.get_object_head(
                bucket=upload_session.storage_bucket,
                object_key=upload_session.object_key,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("get_object_head failed during mime check: %s", exc)
            head = b""
        if not mime_consistent(declared_mime, sniff_mime(head)):
            _reject_upload(**_reject_kw, reason="mime_mismatch")

        # 3. SHA-256 verification — streamed from storage so the (size-capped,
        # see step 1) object is hashed in bounded chunks and never buffered whole
        # in memory; the guard caps how many verifications run concurrently.
        if req.sha256:
            try:
                with sha256_verification_guard():
                    chunks = storage.stream_object(
                        bucket=upload_session.storage_bucket,
                        object_key=upload_session.object_key,
                        chunk_size=settings.MEDIA_SHA256_VERIFY_CHUNK_SIZE,
                    )
                    digest_ok = verify_sha256_stream(chunks, req.sha256)
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Failed to read object for SHA-256 verification.",
                ) from exc
            if not digest_ok:
                _reject_upload(**_reject_kw, reason="sha256_mismatch")

        # 4. Pin the stored Content-Type to the server-validated declared type.
        # The presigned PUT let the client choose the Content-Type, which is
        # served verbatim on direct public-bucket access; normalising it here
        # stops a validated-but-mistyped object from being served as an active
        # type (e.g. text/html) and triggering stored XSS.
        try:
            storage.set_object_content_type(
                bucket=upload_session.storage_bucket,
                object_key=upload_session.object_key,
                content_type=declared_mime,
            )
        except Exception as exc:
            _logger.warning("set_object_content_type failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Failed to finalize object content type.",
            ) from exc

        media_object = MediaObject(
            id=upload_session.id,
            owner_user_id=upload_session.owner_user_id,
            tenant_id=upload_session.tenant_id,
            category=upload_session.category,
            visibility=upload_session.visibility,
            storage_bucket=upload_session.storage_bucket,
            object_key=upload_session.object_key,
            original_filename=upload_session.original_filename,
            mime_type=upload_session.expected_mime_type,
            size_bytes=stat.size,
            etag=stat.etag,
            sha256=req.sha256,
            status=MediaObjectStatus.UPLOADED,
        )
        upload_session.status = UploadSessionStatus.COMPLETED
        upload_session.completed_at = utcnow()
        session.add(media_object)
        session.add(upload_session)
        # Credit the stored bytes to the owner's running totals in the same
        # transaction that promotes the object, so usage never diverges.
        record_object_added(
            session,
            owner_user_id=upload_session.owner_user_id,
            tenant_id=upload_session.tenant_id,
            size_bytes=stat.size,
        )
        session.commit()
        session.refresh(media_object)
        inc_upload_completed(str(upload_session.category), stat.size)
        return UploadCompleteResponse(
            media_object=MediaObjectPublic.model_validate(media_object)
        )

    @staticmethod
    def abort_upload(
        *,
        session: Session,
        current_user: UserModel,
        session_id: uuid.UUID,
        storage: ObjectStorage,
    ) -> None:
        """Cancel an upload session and remove any partial object from storage."""
        upload_session = _load_owned_session(session, current_user, session_id)

        if upload_session.status == UploadSessionStatus.COMPLETED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot abort a completed upload.",
            )

        if upload_session.status == UploadSessionStatus.INITIATED:
            try:
                storage.remove_object(
                    bucket=upload_session.storage_bucket,
                    object_key=upload_session.object_key,
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning("storage.remove_object failed during abort: %s", exc)

        upload_session.status = UploadSessionStatus.ABORTED
        session.add(upload_session)
        session.commit()
        inc_upload_failed()
