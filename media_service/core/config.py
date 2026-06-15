"""Configuration settings for media_service.

Media-specific fields only — auth, observability, and consumer wiring
are all inherited from ConsumerServiceSettings (fastapi-m8).
"""

from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from auth_sdk_m8.utils.paths import find_dotenv
from fastapi_m8 import ConsumerServiceSettings

# pylint: disable=invalid-name


class Settings(ConsumerServiceSettings):
    """media_service settings — extends ConsumerServiceSettings."""

    ENV_FILE_DIR: Path = Path(__file__).resolve().parent

    model_config = SettingsConfigDict(
        env_file=find_dotenv(Path(__file__).resolve().parent),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="forbid",
    )

    secret_fields = ConsumerServiceSettings.secret_fields + [
        "MINIO_SECRET_KEY",
        "MEDIA_REDIS_PASSWORD",
        "MEDIA_INTERNAL_SERVICE_TOKEN",
    ]

    # ── Internal service auth ─────────────────────────────────────────────────
    # Shared bearer token the worker presents on internal callbacks. Compared
    # with secrets.compare_digest; must be a high-entropy random value in prod.
    MEDIA_INTERNAL_SERVICE_TOKEN: SecretStr

    # ── MinIO ────────────────────────────────────────────────────────────────
    MINIO_HOST: str = "minio"
    MINIO_PORT: int = Field(default=9000, ge=1, le=65535)
    MINIO_USE_SSL: bool = False
    MINIO_REGION: str = "eu-west-1"
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET_PUBLIC: str = "public-media"
    MINIO_BUCKET_PRIVATE: str = "private-media"
    MINIO_BUCKET_SENSITIVE: str = "sensitive-media"
    MINIO_BUCKET_TEMP: str = "temp-media"
    MINIO_BUCKET_ARCHIVE: str = "archive-media"
    MINIO_PRESIGNED_URL_EXPIRE_SECONDS: int = Field(default=300, ge=1)
    MEDIA_MAX_UPLOAD_SIZE_BYTES: int = Field(default=104_857_600, ge=1)
    MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY: dict[str, int] = Field(
        default_factory=dict
    )

    # ── Storage quotas ───────────────────────────────────────────────────────
    # Default ceilings applied to every owner/tenant scope without an explicit
    # admin override. ``None`` means unlimited (no enforcement).
    MEDIA_DEFAULT_QUOTA_BYTES: Optional[int] = Field(default=None, ge=1)
    MEDIA_DEFAULT_QUOTA_OBJECTS: Optional[int] = Field(default=None, ge=1)

    # ── Lifecycle / retention (Phase 14 maintenance worker) ──────────────────
    # Not secrets — literal defaults. The service-owned arq worker uses these to
    # drive scheduled hard-purge, stale-upload expiry, and orphan reconciliation.
    # Age (days) a soft-deleted object must exceed before its bytes + row are
    # hard-deleted; bounded per run by the batch limit.
    MEDIA_RETENTION_PURGE_DAYS: int = Field(default=30, ge=1)
    MEDIA_PURGE_BATCH_LIMIT: int = Field(default=500, ge=1)
    # Safety window: objects/keys younger than this are skipped by the reconciler
    # so an in-flight upload (row or bytes mid-creation) is never flagged orphan.
    MEDIA_RECONCILE_GRACE_MINUTES: int = Field(default=60, ge=0)
    MEDIA_RECONCILE_BATCH_LIMIT: int = Field(default=1000, ge=1)
    # Cron cadence: hard-purge runs daily at this hour; stale-upload expiry runs
    # hourly at this minute.
    MEDIA_PURGE_CRON_HOUR: int = Field(default=3, ge=0, le=23)
    MEDIA_STALE_CRON_MINUTE: int = Field(default=15, ge=0, le=59)

    # ── Media Redis ──────────────────────────────────────────────────────────
    MEDIA_REDIS_HOST: str = "media_redis_cache"
    MEDIA_REDIS_PORT: int = Field(default=6379, ge=1, le=65535)
    MEDIA_REDIS_USER: str = "appuser"
    MEDIA_REDIS_PASSWORD: Optional[SecretStr] = None
    MEDIA_REDIS_NAMESPACE: str = "media"


try:
    settings = Settings()
except Exception as exc:  # pragma: no cover
    raise RuntimeError(f"Configuration validation error:\n {exc}") from exc
