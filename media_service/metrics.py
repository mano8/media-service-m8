"""Media-specific Prometheus counters."""

from typing import Optional

from prometheus_client import Counter

from auth_sdk_m8.observability.metrics import REGISTRY

_uploads_initiated: Optional[Counter] = None
_uploads_completed: Optional[Counter] = None
_uploads_failed: Optional[Counter] = None
_uploads_rejected: Optional[Counter] = None
_bytes_uploaded: Optional[Counter] = None
_download_urls_generated: Optional[Counter] = None


def setup(*, enabled: bool, api_prefix: str = "media") -> None:
    """Initialise media counters when metrics are enabled."""
    if not enabled:
        return
    _do_register(api_prefix)  # pragma: no cover


def _do_register(api_prefix: str) -> None:  # pragma: no cover
    global _uploads_initiated, _uploads_completed, _uploads_failed, _uploads_rejected
    global _bytes_uploaded, _download_urls_generated
    p = api_prefix.strip().lstrip("/").replace("-", "_").replace("/", "_")
    pfx = f"{p}_" if p else ""
    _uploads_initiated = Counter(
        f"{pfx}media_uploads_initiated_total",
        "Upload sessions initiated by category and visibility",
        ["category", "visibility"],
        registry=REGISTRY,
    )
    _uploads_completed = Counter(
        f"{pfx}media_uploads_completed_total",
        "Completed uploads by category",
        ["category"],
        registry=REGISTRY,
    )
    _uploads_failed = Counter(
        f"{pfx}media_uploads_failed_total",
        "Upload sessions aborted by user or expired",
        registry=REGISTRY,
    )
    _uploads_rejected = Counter(
        f"{pfx}media_uploads_rejected_total",
        "Uploads rejected at completion by reason",
        ["reason"],
        registry=REGISTRY,
    )
    _bytes_uploaded = Counter(
        f"{pfx}media_bytes_uploaded_total",
        "Bytes accepted at upload completion by category",
        ["category"],
        registry=REGISTRY,
    )
    _download_urls_generated = Counter(
        f"{pfx}media_download_urls_generated_total",
        "Presigned download URLs generated",
        registry=REGISTRY,
    )


def inc_upload_initiated(category: str, visibility: str) -> None:
    if _uploads_initiated is not None:
        _uploads_initiated.labels(category=category, visibility=visibility).inc()


def inc_upload_completed(category: str, size_bytes: int) -> None:
    if _uploads_completed is not None:
        _uploads_completed.labels(category=category).inc()
    if _bytes_uploaded is not None:
        _bytes_uploaded.labels(category=category).inc(size_bytes)


def inc_upload_failed() -> None:
    if _uploads_failed is not None:
        _uploads_failed.inc()


def inc_upload_rejected(reason: str) -> None:
    if _uploads_rejected is not None:
        _uploads_rejected.labels(reason=reason).inc()


def inc_download_url_generated() -> None:
    if _download_urls_generated is not None:
        _download_urls_generated.inc()
