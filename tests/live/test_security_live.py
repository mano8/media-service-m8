"""Live security tests for the hardened_media_m8 compose stack.

Run against a running stack:
    pytest tests/live/ -v

These tests verify Traefik-level routing blocks for internal-only endpoints.
They do NOT test application logic — only that Traefik correctly returns 404
for paths that must never be reachable from the public internet.

OPERATOR NOTE: a non-404 failure means a Traefik misconfiguration.
See the SECURITY CONTRACT comment in docker_compose/hardened_media_m8/traefik/dynamic_conf.yml.
"""

import os
import uuid

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


class TestMedia_InternalCallbacks:
    """Media /v1/internal/* worker callbacks must not be routed through Traefik's
    public entrypoint (item 11.1).

    These endpoints are gated at the app layer by MEDIA_INTERNAL_SERVICE_TOKEN,
    but a stolen worker token must not be replayable through the public domain.
    Traefik must return 404 at ingress regardless of the bearer presented.

    OPERATOR NOTE: a non-404 status means Traefik is misconfigured —
    PathPrefix(`/media/v1/internal`) is missing from the exclusion list in
    media-public-router (dynamic_conf.yml).

    An optional real token can be supplied via LIVE_TEST_MEDIA_INTERNAL_TOKEN to
    prove that even a valid worker token is refused at the public ingress.
    """

    # A representative internal callback: apply-scan-result on a random object.
    _URL = f"{HTTPS_BASE}/media/v1/internal/objects/{uuid.uuid4()}/scan-result"
    _BODY = {"scan_status": "CLEAN"}

    def _post(self, **kwargs) -> requests.Response:
        try:
            return requests.post(self._URL, timeout=TIMEOUT, verify=False, **kwargs)  # noqa: S501
        except requests.exceptions.SSLError:
            pytest.skip("SSL error — check cert setup")

    def _assert_blocked(self, r: requests.Response) -> None:
        assert r.status_code == 404, _TRAEFIK_MISCONFIG_MSG.format(
            path="/media/v1/internal",
            router="media-public-router",
            status=r.status_code,
        )

    def test_internal_callback_blocked_no_bearer(self):
        """GOOD: Traefik returns 404 — /media/v1/internal not reachable publicly."""
        self._assert_blocked(self._post(json=self._BODY))

    def test_internal_callback_blocked_wrong_bearer(self):
        """GOOD: Traefik returns 404 regardless of the (wrong) bearer value."""
        r = self._post(json=self._BODY, headers={"Authorization": "Bearer wrong"})
        self._assert_blocked(r)

    def test_internal_callback_blocked_with_configured_bearer(self):
        """GOOD: even a valid worker token is refused at the public ingress."""
        token = os.environ.get("LIVE_TEST_MEDIA_INTERNAL_TOKEN")
        if not token:
            pytest.skip("LIVE_TEST_MEDIA_INTERNAL_TOKEN not set")
        r = self._post(json=self._BODY, headers={"Authorization": f"Bearer {token}"})
        self._assert_blocked(r)

    def test_internal_callback_absent_from_openapi(self):
        """Internal callback routes must not appear in the public OpenAPI schema."""
        r = requests.get(f"{MEDIA_BASE}/openapi.json", timeout=TIMEOUT)
        paths = r.json().get("paths", {})
        internal_paths = [p for p in paths if "/internal/" in p]
        assert not internal_paths, (
            f"[APP MISCONFIGURATION] Internal routes exposed in OpenAPI: "
            f"{internal_paths}. Ensure the internal router uses include_in_schema=False."
        )
