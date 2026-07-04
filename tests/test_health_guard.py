"""Items 1.4, 9.3, and 9.4 — /health detail-gating at the app layer (media-service-m8).

Item 1.4: create_app wires the health endpoint via fastapi-m8; the detail body
is gated by X-Internal-Token against a dedicated HEALTH_DETAIL_CREDENTIAL
(fail-closed — only the shallow {"status": ...} is returned when unset).

Item 9.3: HEALTH_DETAIL_CREDENTIAL is decoupled from PRIVATE_API_SECRET.
PRIVATE_API_SECRET must never open the detail body. Reuse of PRIVATE_API_SECRET
as either operational credential is a fatal startup ConfigurationError.

Item 9.4 (Design B): the ungated /health body is a constant liveness response —
always {"status": "ok"} with HTTP 200, regardless of dependency health. No
degraded state ever leaks to anonymous callers; detail requires the dedicated
credential. This makes /media/health safe to expose on the public Traefik
entrypoint (see docker_compose/*/traefik/dynamic_conf.yml).
"""

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import SecretStr

from auth_sdk_m8.core.exceptions import ConfigurationError
from auth_sdk_m8.security.guards import make_internal_token_authorizer
from fastapi_m8 import HealthCheckResult, HealthConfig, HealthStatus, create_app

from media_service.core.config import Settings, settings

_INTERNAL_SECRET = "test-internal-token-XYZ"


def _make_app():
    """Minimal app wired with a known internal-token authorizer (explicit path)."""
    return create_app(
        settings,
        APIRouter(),
        health=HealthConfig(
            detail_authorizer=make_internal_token_authorizer(_INTERNAL_SECRET)
        ),
    )


def _make_default_app(s=None):
    """App using HEALTH_DETAIL_CREDENTIAL from settings (no explicit authorizer)."""
    return create_app(s or settings, APIRouter(), health=HealthConfig())


def _make_settings(**overrides):
    """Settings instance built on the test os.environ + specific overrides."""
    return Settings(**overrides)


# ── Item 1.4 — explicit detail_authorizer path ────────────────────────────────


def test_anonymous_caller_gets_shallow_status_only():
    """No X-Internal-Token → only {"status": ...} returned (no checks/detail)."""
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        resp = client.get(f"{settings.API_PREFIX}/health/")

    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "status" in body
    assert "checks" not in body
    assert "service" not in body


def test_correct_internal_token_reveals_full_detail():
    """Correct X-Internal-Token → full detail body including checks."""
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        resp = client.get(
            f"{settings.API_PREFIX}/health/",
            headers={"X-Internal-Token": _INTERNAL_SECRET},
        )

    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "status" in body
    assert "checks" in body
    assert "service" in body


def test_wrong_internal_token_gets_shallow_status_only():
    """Wrong X-Internal-Token → shallow only (200, not 401 — health always answers)."""
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        resp = client.get(
            f"{settings.API_PREFIX}/health/",
            headers={"X-Internal-Token": "wrong-secret"},
        )

    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "status" in body
    assert "checks" not in body


def test_health_always_answers_even_without_secret_configured():
    """No PRIVATE_API_SECRET configured (None) → shallow only, no 401."""
    app = create_app(
        settings,
        APIRouter(),
        health=HealthConfig(detail_authorizer=make_internal_token_authorizer(None)),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"{settings.API_PREFIX}/health/")

    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "status" in body
    assert "checks" not in body


# ── Item 9.3 — HEALTH_DETAIL_CREDENTIAL decoupled from PRIVATE_API_SECRET ────


def test_health_detail_hidden_when_credential_unset():
    """No HEALTH_DETAIL_CREDENTIAL in settings → detail never shown (fail-closed)."""
    with TestClient(_make_default_app(), raise_server_exceptions=False) as client:
        resp = client.get(
            f"{settings.API_PREFIX}/health/",
            headers={"X-Internal-Token": "anyvalue"},
        )
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "checks" not in body
    assert "service" not in body


def test_health_private_api_secret_does_not_open_detail():
    """PRIVATE_API_SECRET as X-Internal-Token must NOT unlock /health detail (9.3 no-reuse)."""
    s = _make_settings(PRIVATE_API_SECRET=SecretStr("private-secret"))
    app = _make_default_app(s)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(
            f"{s.API_PREFIX}/health/",
            headers={"X-Internal-Token": "private-secret"},
        )
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "checks" not in body
    assert "service" not in body


@pytest.mark.anyio
async def test_health_detail_credential_reuse_as_private_secret_rejected():
    """HEALTH_DETAIL_CREDENTIAL == PRIVATE_API_SECRET is a fatal startup error (9.3)."""
    s = _make_settings(
        PRIVATE_API_SECRET=SecretStr("shared-secret"),
        HEALTH_DETAIL_CREDENTIAL=SecretStr("shared-secret"),
    )
    app = create_app(s, APIRouter())
    with pytest.raises(ConfigurationError, match="HEALTH_DETAIL_CREDENTIAL"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.anyio
async def test_metrics_scrape_credential_reuse_as_private_secret_rejected():
    """METRICS_SCRAPE_CREDENTIAL == PRIVATE_API_SECRET is a fatal startup error (9.3)."""
    s = _make_settings(
        PRIVATE_API_SECRET=SecretStr("shared-secret"),
        METRICS_SCRAPE_CREDENTIAL=SecretStr("shared-secret"),
    )
    app = create_app(s, APIRouter())
    with pytest.raises(ConfigurationError, match="METRICS_SCRAPE_CREDENTIAL"):
        async with app.router.lifespan_context(app):
            pass


@pytest.mark.anyio
async def test_distinct_credentials_do_not_raise():
    """Distinct HEALTH_DETAIL_CREDENTIAL and METRICS_SCRAPE_CREDENTIAL are accepted (9.3)."""
    s = _make_settings(
        PRIVATE_API_SECRET=SecretStr("private-secret"),
        HEALTH_DETAIL_CREDENTIAL=SecretStr("health-secret"),
        METRICS_SCRAPE_CREDENTIAL=SecretStr("metrics-secret"),
    )
    app = create_app(s, APIRouter())
    async with app.router.lifespan_context(app):
        pass  # no ConfigurationError raised


# ── Item 9.4 — ungated /health body is a constant liveness response ───────────


def test_ungated_body_is_constant_status_ok():
    """Anonymous request → exactly {"status": "ok"} with HTTP 200 (no degraded leak, 9.4)."""
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        resp = client.get(f"{settings.API_PREFIX}/health/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ungated_body_constant_even_with_failing_check():
    """Failing health checks do not leak status into the ungated response (9.4)."""

    async def _fail() -> HealthCheckResult:
        return HealthCheckResult(name="db", status=HealthStatus.FAIL, message="down")

    app = create_app(
        settings,
        APIRouter(),
        health=HealthConfig(
            checks=[_fail],
            detail_authorizer=make_internal_token_authorizer(_INTERNAL_SECRET),
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"{settings.API_PREFIX}/health/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ungated_body_never_exposes_detail_keys_even_with_failing_check():
    """No checks/service keys in ungated body even when checks are failing (9.4)."""

    async def _fail() -> HealthCheckResult:
        return HealthCheckResult(name="redis", status=HealthStatus.FAIL, message="down")

    app = create_app(
        settings,
        APIRouter(),
        health=HealthConfig(
            checks=[_fail],
            detail_authorizer=make_internal_token_authorizer(_INTERNAL_SECRET),
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"{settings.API_PREFIX}/health/")
    body = resp.json()
    assert "checks" not in body
    assert "service" not in body


def test_detail_still_exposed_to_credential_holder_under_failing_checks():
    """Credential holder still sees full detail (including FAIL status) via 9.3 gate (9.4)."""

    async def _fail() -> HealthCheckResult:
        return HealthCheckResult(name="db", status=HealthStatus.FAIL, message="down")

    app = create_app(
        settings,
        APIRouter(),
        health=HealthConfig(
            checks=[_fail],
            detail_authorizer=make_internal_token_authorizer(_INTERNAL_SECRET),
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(
            f"{settings.API_PREFIX}/health/",
            headers={"X-Internal-Token": _INTERNAL_SECRET},
        )
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "fail"
    assert "checks" in body
