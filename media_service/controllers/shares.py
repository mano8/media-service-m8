"""Business logic for time-boxed, signed share links to a media object.

A share link is a small HMAC-signed authenticator over a :class:`ShareToken`
row id. Anyone holding the signed token can resolve it to a presigned download
URL, but only while the row is live (not expired, not revoked, under
``max_uses``) and only once the object has passed antivirus scanning — the same
gating the owner-facing download path enforces. Creation, listing and revocation
are owner-only; resolution is public by design.

Sync ``@staticmethod``s with ``session: Session`` mirror the rest of the
controller layer.
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from auth_sdk_m8.schemas.user import UserModel

from media_service.controllers.objects import _load_object
from media_service.core.config import settings
from media_service.db_models.media_objects import MediaObject, ScanStatus, utcnow
from media_service.db_models.share_tokens import ShareToken
from media_service.schemas.objects import DownloadUrlResponse
from media_service.schemas.shares import (
    ShareTokenCreate,
    ShareTokenListResponse,
    ShareTokenPublic,
)
from media_service.storage.client import ObjectStorage
from media_service.storage.presign import create_download_url


def _signing_key() -> bytes:
    """Return the media-owned HMAC key used to sign share tokens.

    Uses this service's own ``MEDIA_SHARE_SIGNING_SECRET`` — deliberately *not*
    an auth-sdk token secret, whose lifecycle/contract belongs to the auth layer
    and could change out from under us. The setting is required, so a missing
    key fails settings validation at startup rather than here.
    """
    return settings.MEDIA_SHARE_SIGNING_SECRET.get_secret_value().encode()


def _b64(raw: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(token_id: uuid.UUID) -> str:
    """Build the signed, shareable token string for a share row id."""
    digest = hmac.new(_signing_key(), token_id.hex.encode(), hashlib.sha256).digest()
    return f"{token_id.hex}.{_b64(digest)}"


def _verify(token: str) -> uuid.UUID:
    """Verify a signed token and return its row id, or raise 404.

    A malformed token, an unparseable id, or a signature mismatch all map to 404
    so the endpoint never confirms whether a given id exists.
    """
    parts = token.split(".")
    if len(parts) != 2:
        raise _invalid_token()
    raw_id, signature = parts
    try:
        token_id = uuid.UUID(hex=raw_id)
    except ValueError:
        raise _invalid_token() from None
    expected = _b64(
        hmac.new(_signing_key(), token_id.hex.encode(), hashlib.sha256).digest()
    )
    if not secrets.compare_digest(signature, expected):
        raise _invalid_token()
    return token_id


def _invalid_token() -> HTTPException:
    """Build the uniform 404 raised for any unresolvable token."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Share link not found."
    )


def _as_aware(value: datetime) -> datetime:
    """Coerce a possibly-naive timestamp to an aware UTC one.

    ``DateTime(timezone=True)`` columns read back naive under SQLite but aware
    under Postgres; normalising here lets expiry be compared in Python without
    raising on a naive/aware mismatch.
    """
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _to_public(share: ShareToken) -> ShareTokenPublic:
    """Project a row into its public form, embedding the signed token."""
    return ShareTokenPublic(
        id=share.id,
        media_object_id=share.media_object_id,
        token=_sign(share.id),
        expires_at=share.expires_at,
        max_uses=share.max_uses,
        uses=share.uses,
        revoked=share.revoked,
        created_at=share.created_at,
    )


class SharesController:
    """Create, list, revoke and resolve share links for media objects."""

    @staticmethod
    def create(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
        body: ShareTokenCreate,
    ) -> ShareTokenPublic:
        """Mint a share link for an object the caller owns."""
        obj = _load_object(session, current_user, object_id)
        max_expires = settings.MEDIA_SHARE_MAX_EXPIRES_SECONDS
        if body.expires_in is not None and body.expires_in > max_expires:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"expires_in must not exceed {max_expires} seconds.",
            )
        expires_in = body.expires_in or settings.MEDIA_SHARE_DEFAULT_EXPIRES_SECONDS
        share = ShareToken(
            media_object_id=obj.id,
            owner_user_id=obj.owner_user_id,
            tenant_id=obj.tenant_id,
            expires_at=utcnow() + timedelta(seconds=expires_in),
            max_uses=body.max_uses,
        )
        session.add(share)
        session.commit()
        session.refresh(share)
        return _to_public(share)

    @staticmethod
    def list_for_object(
        *,
        session: Session,
        current_user: UserModel,
        object_id: uuid.UUID,
    ) -> ShareTokenListResponse:
        """List the share links of an object the caller owns."""
        _load_object(session, current_user, object_id)
        rows = list(
            session.exec(
                select(ShareToken)
                .where(col(ShareToken.media_object_id) == object_id)
                .order_by(col(ShareToken.created_at))
            ).all()
        )
        return ShareTokenListResponse(
            items=[_to_public(r) for r in rows], count=len(rows)
        )

    @staticmethod
    def revoke(
        *,
        session: Session,
        current_user: UserModel,
        token_id: uuid.UUID,
    ) -> None:
        """Revoke a share link (idempotent); owner or superuser only."""
        share = SharesController._load_owned_share(session, current_user, token_id)
        if share.revoked:
            return
        share.revoked = True
        session.add(share)
        session.commit()

    @staticmethod
    def resolve(
        *,
        session: Session,
        token: str,
        storage: ObjectStorage,
    ) -> DownloadUrlResponse:
        """Resolve a signed token to a presigned download URL.

        Rejects expired / revoked / exhausted links (403) and objects that have
        not cleared antivirus scanning (409), then records one use and returns a
        short-lived presigned GET.
        """
        share = SharesController._load_active_share(session, token)
        obj = session.get(MediaObject, share.media_object_id)
        if obj is None or obj.deleted_at is not None:
            raise _invalid_token()
        if obj.scan_status != ScanStatus.CLEAN:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Object is not available for download until it passes scanning.",
            )
        share.uses += 1
        session.add(share)
        session.commit()
        expires = settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS
        url = create_download_url(
            storage=storage,
            bucket=obj.storage_bucket,
            object_key=obj.object_key,
            expires_seconds=expires,
            filename=obj.original_filename,
        )
        expires_at = utcnow() + timedelta(seconds=expires)
        return DownloadUrlResponse(url=url, expires_at=expires_at)

    @staticmethod
    def _load_owned_share(
        session: Session, current_user: UserModel, token_id: uuid.UUID
    ) -> ShareToken:
        """Load a share by id, enforcing owner-or-superuser access."""
        share = session.get(ShareToken, token_id)
        if share is None:
            raise _invalid_token()
        owner_id = uuid.UUID(str(current_user.id))
        if not current_user.is_superuser and share.owner_user_id != owner_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not enough permissions.",
            )
        return share

    @staticmethod
    def _load_active_share(session: Session, token: str) -> ShareToken:
        """Verify a token and load a live (non-expired/revoked/exhausted) row."""
        token_id = _verify(token)
        share = session.get(ShareToken, token_id)
        if share is None:
            raise _invalid_token()
        if share.revoked:
            raise _rejected("Share link has been revoked.")
        if _as_aware(share.expires_at) <= utcnow():
            raise _rejected("Share link has expired.")
        if share.max_uses is not None and share.uses >= share.max_uses:
            raise _rejected("Share link has reached its usage limit.")
        return share


def _rejected(detail: str) -> HTTPException:
    """Build the 403 raised when a resolvable token is no longer usable."""
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
