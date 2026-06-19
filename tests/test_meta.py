"""Tests for the service /media/meta + /ping routes (item 6).

create_app (fastapi-m8 >= 2.1.0) auto-mounts these from ConsumerServiceSettings;
this verifies media-service supplies valid values and the routes are reachable
unauthenticated.
"""

from fastapi.testclient import TestClient

from media_service.main import app

client = TestClient(app)


def test_meta_route_exposes_service_contract() -> None:
    resp = client.get("/media/meta")
    assert resp.status_code == 200
    assert resp.json() == {
        "service": "M8TestApp",
        "version": "0.0.8",
        "api_version": "v1",
        "contract": {
            "name": "media-service-m8",
            "version": "0.0",
            "range": ">=0.0.8 <0.1.0",
        },
    }


def test_meta_route_is_cacheable() -> None:
    resp = client.get("/media/meta")
    assert resp.headers["Cache-Control"] == "public, max-age=300"


def test_ping_route_prefix_independent() -> None:
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ping_route_reachable_under_media_prefix() -> None:
    resp = client.get("/media/ping")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ping_schema_carries_single_operation() -> None:
    ping_routes = [
        route for route in app.routes if getattr(route, "path", "").endswith("/ping")
    ]
    schema_paths = [
        route.path
        for route in ping_routes
        if getattr(route, "include_in_schema", False)
    ]

    assert {route.path for route in ping_routes} == {"/ping", "/media/ping"}
    assert schema_paths == ["/ping"]
