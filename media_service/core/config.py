"""Configuration settings for media_service.

Media-specific fields only — auth, observability, and consumer wiring
are all inherited from ConsumerServiceSettings (fastapi-m8).
"""

from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from auth_sdk_m8.utils.paths import find_dotenv
from fastapi_m8 import ConsumerServiceSettings

from media_service import __version__

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

    # ── Service/contract metadata (GET {API_PREFIX}/meta) ─────────────────────
    # fastapi-m8 >= 2.0.0 requires these; declared here (not env) so the service
    # version tracks the package and the contract id matches the astro-media
    # plugin. Overridable from the environment for non-default deployments.
    SERVICE_VERSION: str = __version__
    CONTRACT_NAME: str = "media-service-m8"
    # Contract major.minor tracks the package (0.0.x) — pre-1.0, not the
    # aspirational "1.0"; kept in lockstep with astro-media-m8's contract pin.
    CONTRACT_VERSION: str = "0.0"
    CONTRACT_RANGE: str = ">=0.0.9 <0.1.0"

    secret_fields = ConsumerServiceSettings.secret_fields + [
        "MINIO_SECRET_KEY",
        "MEDIA_REDIS_PASSWORD",
        "MEDIA_INTERNAL_SERVICE_TOKEN",
        "MEDIA_SHARE_SIGNING_SECRET",
    ]

    # ── Internal service auth ─────────────────────────────────────────────────
    # Shared bearer token the worker presents on internal callbacks. Compared
    # with secrets.compare_digest; must be a high-entropy random value in prod.
    MEDIA_INTERNAL_SERVICE_TOKEN: SecretStr

    # ── Share links ───────────────────────────────────────────────────────────
    # Independent HMAC key for signing share-link tokens. Owned by this service
    # (never reuse an auth-sdk token secret like ACCESS_SECRET_KEY, whose
    # lifecycle and contract belong to the auth layer). Required, so a missing
    # key fails settings validation at startup rather than minting unverifiable
    # links. Rotate independently; rotation invalidates outstanding links.
    MEDIA_SHARE_SIGNING_SECRET: SecretStr
    # Lifetime applied when a caller omits ``expires_in`` on create (default 7d).
    MEDIA_SHARE_DEFAULT_EXPIRES_SECONDS: int = Field(default=604_800, ge=1)
    # Operator ceiling on a caller-requested ``expires_in`` (default 30d); a
    # request above this is rejected at create time. Bounds how long a leaked
    # link stays useful.
    MEDIA_SHARE_MAX_EXPIRES_SECONDS: int = Field(default=2_592_000, ge=1)

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

    # ── SHA-256 upload verification ──────────────────────────────────────────
    # When a client supplies an expected SHA-256 on complete, the object is
    # streamed from storage and hashed in chunks of this many bytes, so a large
    # (but size-capped) object is never buffered whole in memory (default 1 MiB).
    MEDIA_SHA256_VERIFY_CHUNK_SIZE: int = Field(default=1_048_576, ge=1)
    # Process-wide ceiling on concurrent SHA-256 verifications. Bounds how many
    # objects are streamed + hashed at once so a burst of completions cannot
    # fan out into unbounded concurrent full-object reads.
    MEDIA_SHA256_VERIFY_MAX_CONCURRENCY: int = Field(default=4, ge=1)

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

    # ── Events / outbox webhook delivery (Phase 16) ──────────────────────────
    # Not secrets — literal defaults. The service-owned arq worker's
    # ``deliver_outbox`` cron drains PENDING outbox rows and POSTs each as a
    # signed OutboxEventPayload to matching subscribers. A subscription's signing
    # ``secret`` is per-row (DB), never a global env secret.
    # Second-of-minute the delivery cron fires; it runs once per minute so events
    # drain promptly (delivery is latency-sensitive, unlike the housekeeping crons).
    OUTBOX_DELIVERY_CRON_SECOND: int = Field(default=0, ge=0, le=59)
    # Max events claimed per delivery run (bounds work + outbound request volume).
    OUTBOX_BATCH_LIMIT: int = Field(default=100, ge=1)
    # Poison-message cap: after this many failed attempts an event is marked
    # terminally FAILED and never retried again.
    OUTBOX_MAX_ATTEMPTS: int = Field(default=8, ge=1)
    # Exponential-backoff base: a failed event's retry delay is
    # ``base * 2 ** (attempts - 1)`` seconds.
    OUTBOX_BACKOFF_BASE_SECONDS: int = Field(default=30, ge=1)
    # Per-request timeout (seconds) for a single subscriber POST.
    OUTBOX_DELIVERY_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)
    # SSRF allowlist: exact hostnames a webhook subscriber may target even when
    # they resolve to a private/internal address (and exempt from the
    # production HTTPS rule). Use for trusted in-cluster subscribers — e.g.
    # ["media_worker"]. Everything else is gated by core.ssrf: loopback/
    # link-local/metadata are always blocked; private ranges and plain http
    # are rejected only under production/strict (Docker-network targets stay
    # reachable in local/dev).
    MEDIA_WEBHOOK_ALLOWED_INTERNAL_HOSTS: list[str] = Field(default_factory=list)

    # ── Rate-limiter Redis-error policy ──────────────────────────────────────
    # Controls what happens when the Redis backing the rate limiter is
    # unreachable. "fail_open" (default) lets traffic through so a Redis outage
    # never blocks media uploads. "fail_closed" returns HTTP 503 on Redis
    # error, preventing unenforced bursts during outages. Recommended:
    # fail_closed in production, fail_open in dev/local.
    MEDIA_RATE_LIMIT_FAILURE_MODE: Literal["fail_open", "fail_closed"] = "fail_open"

    # ── Media Redis ──────────────────────────────────────────────────────────
    MEDIA_REDIS_HOST: str = "media_redis_cache"
    MEDIA_REDIS_PORT: int = Field(default=6379, ge=1, le=65535)
    MEDIA_REDIS_USER: str = "media"
    MEDIA_REDIS_PASSWORD: Optional[SecretStr] = None
    MEDIA_REDIS_NAMESPACE: str = "media"


try:
    settings = Settings()
except Exception as exc:  # pragma: no cover
    raise RuntimeError(f"Configuration validation error:\n {exc}") from exc
