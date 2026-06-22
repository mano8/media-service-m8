"""Live-test configuration for the media-service-m8 hardened_media_m8 compose stack."""

from __future__ import annotations

from pathlib import Path

from security_tests_m8 import configure_from_env

EXAMPLE_ROOT = Path(__file__).resolve().parents[2]
HARDENED_STACK_ROOT = EXAMPLE_ROOT.parent / "hardened_media_m8"

configure_from_env(
    auth_base_url="http://localhost:9000/user",
    service_base_url="http://localhost:9000/media",
    service_base_urls={"media": "http://localhost:9000/media"},
    default_service="media",
    timeout=10,
    repo_root=HARDENED_STACK_ROOT,
    deployment_root=HARDENED_STACK_ROOT,
    public_base_url="https://localhost:4430",
    public_tls_verify=False,
    fail_fast_preflight=True,
    forbid_bootstrap_superuser=True,
    protected_endpoints={
        # Authenticated GET endpoints (relative to the /media API prefix) with no
        # required path params. Env LIVE_TEST_PROTECTED_ENDPOINTS overrides this.
        "media": [
            "/category/",
            "/dashboard/users/activity/",
            "/dashboard/users/activity/current/",
            "/v1/objects",
            "/v1/presets",
            "/v1/admin/storage/stats",
            "/v1/admin/uploads/stale",
            "/v1/admin/maintenance/orphans",
            "/v1/admin/subscriptions",
        ],
    },
)
