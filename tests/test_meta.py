"""Tests for the service /media/meta + /ping routes (item 6).

create_app (fastapi-m8 >= 3.0.0) auto-mounts these from ConsumerServiceSettings;
this verifies media-service supplies valid values and the routes are reachable
unauthenticated.

SDK 2.0.0 introduced single-mount /ping: the route is mounted exactly once at
{prefix}/ping (/media/ping here).  The bare root /ping is NOT registered when
a prefix is configured, so GET /ping returns 404.
"""

from fastapi.testclient import TestClient

from media_service import __version__
from media_service.core.config import settings
from media_service.main import app

client = TestClient(app)


def test_meta_route_exposes_service_contract() -> None:
    # `version` is read from the package rather than repeated as a literal: a
    # hard-coded copy has to be hand-edited on every release and silently
    # asserts nothing about the service once it drifts (the A30 stale-literal
    # shape). The contract block stays literal on purpose — it is the published
    # HTTP contract, which moves independently of the package version when the
    # served HTTP surface changes, so a change there should fail this test.
    resp = client.get("/media/meta")
    assert resp.status_code == 200
    assert resp.json() == {
        "service": "M8TestApp",
        "version": __version__,
        "api_version": "v1",
        "contract": {
            "name": "media-service-m8",
            "version": "1.1",
            "range": ">=2.0.0 <3.0.0",
        },
    }


def test_meta_version_tracks_the_package_version() -> None:
    """`SERVICE_VERSION` must be the package version, not a second copy of it."""
    assert settings.SERVICE_VERSION == __version__
    assert client.get("/media/meta").json()["version"] == __version__


def test_meta_route_is_cacheable() -> None:
    resp = client.get("/media/meta")
    assert resp.headers["Cache-Control"] == "public, max-age=300"


def test_ping_route_not_at_bare_root() -> None:
    # SDK 2.0.0 single-mount: /ping is registered at {prefix}/ping only.
    # The bare root /ping is not mounted when a prefix is configured.
    resp = client.get("/ping")
    assert resp.status_code == 404


def test_ping_route_reachable_under_media_prefix() -> None:
    resp = client.get("/media/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ping_schema_carries_single_operation() -> None:
    # SDK 2.0.0: exactly one ping route at /media/ping.
    assert client.get("/media/ping").status_code == 200
    assert client.get("/ping").status_code == 404

    # Schema contract via the public OpenAPI document, not the internal
    # ``app.routes`` list: FastAPI 0.137+ no longer flattens included routers
    # onto ``app.routes`` (they become nested ``_IncludedRouter`` entries with
    # ``path=None``), so walking ``app.routes`` for ``route.path`` finds nothing.
    schema_paths = app.openapi()["paths"]
    assert "/media/ping" in schema_paths  # single advertised route
    assert "/ping" not in schema_paths  # no bare root ping
