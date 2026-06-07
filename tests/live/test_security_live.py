"""Live security tests for the hardened_media_m8 compose stack.

Run against a running stack:
    pytest tests/live/ -v

These tests verify Traefik-level routing blocks for internal-only endpoints.
They do NOT test application logic — only that Traefik correctly returns 404
for paths that must never be reachable from the public internet.

OPERATOR NOTE: a non-404 failure means a Traefik misconfiguration.
See the SECURITY CONTRACT comment in docker_compose/hardened_media_m8/traefik/dynamic_conf.yml.
"""

import pytest
import requests

AUTH_BASE = "http://localhost:9000/user"
MEDIA_BASE = "http://localhost:9000/media"
HTTPS_BASE = "https://localhost:4430"
TIMEOUT = 10

_TRAEFIK_MISCONFIG_MSG = (
    "TRAEFIK MISCONFIGURATION: {path!r} is not excluded from {router} "
    "in dynamic_conf.yml. Got {status}, expected 404. "
    "Fix: add PathPrefix(`{path}`) to the exclusion list and restart Traefik. "
    "See the SECURITY CONTRACT comment in "
    "docker_compose/hardened_media_m8/traefik/dynamic_conf.yml."
)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH SERVICE — private and internal endpoints
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuth_PrivateAPI:
    """Auth /private/ must not be routed through Traefik's public entrypoint.

    OPERATOR NOTE: if any test below fails with a non-404 status, it means
    Traefik is misconfigured — PathPrefix(`/user/private`) is missing from
    the exclusion list in auth-public-router (dynamic_conf.yml).
    """

    _URL = f"{HTTPS_BASE}/user/private/users/"
    _BODY = {
        "email": "pvt@redteam-test.com",
        "password": "Test!123",
        "full_name": "T",
        "is_verified": False,
    }

    def _post(self, **kwargs) -> requests.Response:
        try:
            return requests.post(self._URL, timeout=TIMEOUT, verify=False, **kwargs)  # noqa: S501
        except requests.exceptions.SSLError:
            pytest.skip("SSL error — check cert setup")

    def test_private_route_blocked_by_traefik(self):
        """GOOD: Traefik returns 404 — /user/private/ not reachable from the internet."""
        r = self._post(json=self._BODY)
        assert r.status_code == 404, _TRAEFIK_MISCONFIG_MSG.format(
            path="/user/private",
            router="auth-public-router",
            status=r.status_code,
        )

    def test_private_route_blocked_with_wrong_token(self):
        """GOOD: Traefik returns 404 regardless of token value."""
        r = self._post(json=self._BODY, headers={"X-Internal-Token": "wrong"})
        assert r.status_code == 404, _TRAEFIK_MISCONFIG_MSG.format(
            path="/user/private",
            router="auth-public-router",
            status=r.status_code,
        )

    def test_private_endpoint_absent_from_openapi(self):
        """Private routes must not appear in the public OpenAPI schema."""
        r = requests.get(f"{AUTH_BASE}/openapi.json", timeout=TIMEOUT)
        paths = r.json().get("paths", {})
        private_paths = [p for p in paths if "/private/" in p]
        assert not private_paths, (
            f"[TRAEFIK/APP MISCONFIGURATION] Private routes exposed in OpenAPI: "
            f"{private_paths}. Ensure include_in_schema=False on the private router."
        )


class TestAuth_MetricsAPI:
    """Auth /metrics must not be routed through Traefik's public entrypoint.

    OPERATOR NOTE: if any test below fails with a non-404 status, it means
    Traefik is misconfigured — PathPrefix(`/user/metrics`) is missing from
    the exclusion list in auth-public-router (dynamic_conf.yml).
    """

    _URL = f"{HTTPS_BASE}/user/metrics"

    def test_metrics_blocked_by_traefik(self):
        """GOOD: Traefik returns 404 — /user/metrics not reachable from the internet."""
        try:
            r = requests.get(self._URL, timeout=TIMEOUT, verify=False)  # noqa: S501
        except requests.exceptions.SSLError:
            pytest.skip("SSL error — check cert setup")
        assert r.status_code == 404, _TRAEFIK_MISCONFIG_MSG.format(
            path="/user/metrics",
            router="auth-public-router",
            status=r.status_code,
        )

    def test_metrics_absent_from_openapi(self):
        """Metrics endpoint must not appear in the public OpenAPI schema."""
        r = requests.get(f"{AUTH_BASE}/openapi.json", timeout=TIMEOUT)
        paths = r.json().get("paths", {})
        metrics_paths = [p for p in paths if "/metrics" in p]
        assert not metrics_paths, (
            f"[APP MISCONFIGURATION] Metrics route exposed in OpenAPI: "
            f"{metrics_paths}. Ensure include_in_schema=False on the metrics endpoint."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# MEDIA SERVICE — internal endpoints
# ═══════════════════════════════════════════════════════════════════════════════


class TestMedia_MetricsAPI:
    """Media /metrics and /health must not be routed through Traefik's public entrypoint.

    OPERATOR NOTE: if any test below fails with a non-404 status, it means
    Traefik is misconfigured — PathPrefix(`/media/metrics`) or
    PathPrefix(`/media/health`) is missing from the exclusion list in
    media-public-router (dynamic_conf.yml).
    """

    _METRICS_URL = f"{HTTPS_BASE}/media/metrics"
    _HEALTH_URL = f"{HTTPS_BASE}/media/health/"

    def test_metrics_blocked_by_traefik(self):
        """GOOD: Traefik returns 404 — /media/metrics not reachable from the internet."""
        try:
            r = requests.get(self._METRICS_URL, timeout=TIMEOUT, verify=False)  # noqa: S501
        except requests.exceptions.SSLError:
            pytest.skip("SSL error — check cert setup")
        assert r.status_code == 404, _TRAEFIK_MISCONFIG_MSG.format(
            path="/media/metrics",
            router="media-public-router",
            status=r.status_code,
        )

    def test_health_blocked_by_traefik(self):
        """GOOD: Traefik returns 404 — /media/health not reachable from the internet."""
        try:
            r = requests.get(self._HEALTH_URL, timeout=TIMEOUT, verify=False)  # noqa: S501
        except requests.exceptions.SSLError:
            pytest.skip("SSL error — check cert setup")
        assert r.status_code == 404, _TRAEFIK_MISCONFIG_MSG.format(
            path="/media/health",
            router="media-public-router",
            status=r.status_code,
        )

    def test_metrics_absent_from_openapi(self):
        """Metrics endpoint must not appear in the public OpenAPI schema."""
        r = requests.get(f"{MEDIA_BASE}/openapi.json", timeout=TIMEOUT)
        paths = r.json().get("paths", {})
        metrics_paths = [p for p in paths if "/metrics" in p]
        assert not metrics_paths, (
            f"[APP MISCONFIGURATION] Metrics route exposed in OpenAPI: "
            f"{metrics_paths}. Ensure include_in_schema=False on the metrics endpoint."
        )

    def test_health_absent_from_openapi(self):
        """Health endpoint must not appear in the public OpenAPI schema."""
        r = requests.get(f"{MEDIA_BASE}/openapi.json", timeout=TIMEOUT)
        paths = r.json().get("paths", {})
        health_paths = [p for p in paths if "/health" in p]
        assert not health_paths, (
            f"[APP MISCONFIGURATION] Health route exposed in OpenAPI: "
            f"{health_paths}. Ensure include_in_schema=False on the health endpoint."
        )
