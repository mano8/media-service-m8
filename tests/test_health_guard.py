"""Item 1.4 — /health detail-gating at the app layer (media-service-m8).

create_app wires the health endpoint via fastapi-m8; the detail body is gated
by the ``X-Internal-Token`` header against ``PRIVATE_API_SECRET``.  Shallow
``{"status": ...}`` answers everyone; the per-check detail is only returned to
callers presenting the correct shared secret.
"""

from fastapi import APIRouter
from fastapi.testclient import TestClient

from auth_sdk_m8.security.guards import make_internal_token_authorizer
from fastapi_m8 import HealthConfig, create_app

from media_service.core.config import settings

_INTERNAL_SECRET = "test-internal-token-XYZ"


def _make_app():
    """Minimal app wired with a known internal-token authorizer."""
    return create_app(
        settings,
        APIRouter(),
        health=HealthConfig(
            detail_authorizer=make_internal_token_authorizer(_INTERNAL_SECRET)
        ),
    )


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
