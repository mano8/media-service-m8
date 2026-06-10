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
    ]

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
