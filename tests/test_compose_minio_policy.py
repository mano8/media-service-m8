"""Static compose-policy tests for MinIO host-port exposure (item 0.2) and the
browser-direct presigned upload/download ingress (Phase 4).

These tests parse the YAML files directly — no running Docker required.

Policy:
  hardened_media_m8  — MinIO must have NO `ports:` block at all (internal-only).
                     — Traefik storage router must be on websecure (TLS) with tls:{},
                       route by Host (not bare /), exclude /minio paths, and use a
                       minio-storage backend with passHostHeader:true at http://minio:9000.
                     — MINIO_API_CORS_ALLOW_ORIGIN must be set and must NOT be *.
                     — media.env.example must declare MINIO_PUBLIC_ENDPOINT starting with https://.
  dev_media_m8       — MinIO ports must be loopback-bound only (no 0.0.0.0 bind).
                     — MINIO_API_CORS_ALLOW_ORIGIN must be set and must NOT be *.
                     — media.env.example must declare MINIO_PUBLIC_ENDPOINT starting with loopback.
  worspace_dev_media_m8 — same CORS + env.example assertions as dev.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_COMPOSE_DIR = Path(__file__).parent.parent / "docker_compose"
_HARDENED = _COMPOSE_DIR / "hardened_media_m8" / "docker-compose.yml"
_HARDENED_TRAEFIK = _COMPOSE_DIR / "hardened_media_m8" / "traefik" / "dynamic_conf.yml"
_HARDENED_ENV = _COMPOSE_DIR / "hardened_media_m8" / "media.env.example"
_DEV = _COMPOSE_DIR / "dev_media_m8" / "docker-compose.yml"
_DEV_ENV = _COMPOSE_DIR / "dev_media_m8" / "media.env.example"
_WORSPACE = _COMPOSE_DIR / "worspace_dev_media_m8" / "docker-compose.yml"
_WORSPACE_ENV = _COMPOSE_DIR / "worspace_dev_media_m8" / "media.env.example"

_LOOPBACK_RE = re.compile(r"^127\.")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _env_vars(path: Path) -> dict[str, str]:
    """Parse a KEY=value env-example file into a dict (skips comments/blanks)."""
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


# ---------------------------------------------------------------------------
# hardened_media_m8 — host-port policy
# ---------------------------------------------------------------------------


class TestHardenedMinioNoHostPorts:
    """In the hardened stack MinIO must not publish any host port."""

    def test_minio_has_no_ports_block(self):
        compose = _load(_HARDENED)
        minio = compose["services"]["minio"]
        assert "ports" not in minio, (
            "hardened_media_m8: minio must not have a `ports:` block — "
            "it must be reachable only on the Docker network (minio:9000). "
            f"Got: {minio.get('ports')}"
        )


# ---------------------------------------------------------------------------
# hardened_media_m8 — Traefik storage router (Phase 4)
# ---------------------------------------------------------------------------


class TestHardenedTraefikStorageRouter:
    """The hardened stack must expose the S3 data path via a Traefik storage router
    that is TLS-only, Host-pinned, and explicitly excludes admin/console paths."""

    def _traefik(self) -> dict:
        return _load(_HARDENED_TRAEFIK)

    def test_minio_storage_router_exists(self):
        routers = self._traefik()["http"]["routers"]
        assert "media-storage-router" in routers, (
            "hardened_media_m8: traefik/dynamic_conf.yml must define a "
            "'media-storage-router' router for browser-direct presigned ops."
        )

    def test_storage_router_on_websecure_entrypoint(self):
        router = self._traefik()["http"]["routers"]["media-storage-router"]
        entry_points = router.get("entryPoints", [])
        assert "websecure" in entry_points, (
            "hardened_media_m8: media-storage-router must use the 'websecure' "
            f"(TLS) entrypoint, not {entry_points!r}. The 'api' entrypoint is "
            "HTTP-only and must NOT be used for the public storage endpoint."
        )

    def test_storage_router_has_tls(self):
        router = self._traefik()["http"]["routers"]["media-storage-router"]
        assert "tls" in router, (
            "hardened_media_m8: media-storage-router must carry 'tls: {}' — "
            "MINIO_PUBLIC_ENDPOINT is https:// and the route must be TLS-only."
        )

    def test_storage_router_rule_uses_host(self):
        router = self._traefik()["http"]["routers"]["media-storage-router"]
        rule = router.get("rule", "")
        assert "Host(" in rule, (
            f"hardened_media_m8: media-storage-router rule must match by Host(), "
            f"not a bare PathPrefix. Got: {rule!r}"
        )

    def test_storage_router_excludes_minio_admin_paths(self):
        router = self._traefik()["http"]["routers"]["media-storage-router"]
        rule = router.get("rule", "")
        assert "!PathPrefix(`/minio`)" in rule, (
            "hardened_media_m8: media-storage-router rule must include "
            "'!PathPrefix(`/minio`)' to block admin API and console access. "
            f"Got: {rule!r}"
        )

    def test_minio_storage_service_exists(self):
        services = self._traefik()["http"]["services"]
        assert "minio-storage" in services, (
            "hardened_media_m8: traefik/dynamic_conf.yml must define a "
            "'minio-storage' Traefik service."
        )

    def test_minio_storage_backend_url(self):
        lb = self._traefik()["http"]["services"]["minio-storage"]["loadBalancer"]
        urls = [s["url"] for s in lb.get("servers", [])]
        assert "http://minio:9000" in urls, (
            "hardened_media_m8: minio-storage backend must point to "
            f"'http://minio:9000'. Got: {urls!r}"
        )

    def test_minio_storage_pass_host_header(self):
        lb = self._traefik()["http"]["services"]["minio-storage"]["loadBalancer"]
        assert lb.get("passHostHeader") is True, (
            "hardened_media_m8: minio-storage loadBalancer must set "
            "'passHostHeader: true' — GET SigV4 signatures bind the Host header "
            "and the proxy must forward it unchanged for signatures to validate."
        )

    def test_storage_router_service_is_minio_storage(self):
        router = self._traefik()["http"]["routers"]["media-storage-router"]
        assert router.get("service") == "minio-storage", (
            "hardened_media_m8: media-storage-router must route to the "
            f"'minio-storage' service. Got: {router.get('service')!r}"
        )


# ---------------------------------------------------------------------------
# CORS policy — all stacks (Phase 4)
# ---------------------------------------------------------------------------


class TestMinioCorsNotWildcard:
    """Every stack's minio service must set MINIO_API_CORS_ALLOW_ORIGIN and
    it must NOT be the wildcard '*'."""

    @pytest.mark.parametrize(
        "stack_name,compose_path",
        [
            ("hardened_media_m8", _HARDENED),
            ("dev_media_m8", _DEV),
            ("worspace_dev_media_m8", _WORSPACE),
        ],
    )
    def test_cors_origin_is_set(self, stack_name: str, compose_path: Path):
        minio_env = _load(compose_path)["services"]["minio"].get("environment", {})
        assert "MINIO_API_CORS_ALLOW_ORIGIN" in minio_env, (
            f"{stack_name}: minio must set MINIO_API_CORS_ALLOW_ORIGIN "
            "(scoped to the UI origin, never *)."
        )

    @pytest.mark.parametrize(
        "stack_name,compose_path",
        [
            ("hardened_media_m8", _HARDENED),
            ("dev_media_m8", _DEV),
            ("worspace_dev_media_m8", _WORSPACE),
        ],
    )
    def test_cors_origin_is_not_wildcard(self, stack_name: str, compose_path: Path):
        minio_env = _load(compose_path)["services"]["minio"].get("environment", {})
        value = str(minio_env.get("MINIO_API_CORS_ALLOW_ORIGIN", ""))
        assert value != "*", (
            f"{stack_name}: MINIO_API_CORS_ALLOW_ORIGIN must NOT be '*' — "
            "scope it to the specific UI origin."
        )


# ---------------------------------------------------------------------------
# MINIO_PUBLIC_ENDPOINT in env.example — all stacks (Phase 4)
# ---------------------------------------------------------------------------


class TestMinioPublicEndpointEnvExample:
    """Every stack's media.env.example must declare MINIO_PUBLIC_ENDPOINT.
    Dev/worspace stacks must point at loopback; hardened must use https://."""

    def test_hardened_declares_public_endpoint(self):
        env = _env_vars(_HARDENED_ENV)
        assert "MINIO_PUBLIC_ENDPOINT" in env, (
            "hardened_media_m8: media.env.example must declare MINIO_PUBLIC_ENDPOINT."
        )

    def test_hardened_public_endpoint_is_https(self):
        env = _env_vars(_HARDENED_ENV)
        value = env.get("MINIO_PUBLIC_ENDPOINT", "")
        assert value.startswith("https://"), (
            "hardened_media_m8: MINIO_PUBLIC_ENDPOINT must start with 'https://' — "
            f"the storage router is on websecure (TLS). Got: {value!r}"
        )

    def test_dev_declares_public_endpoint(self):
        env = _env_vars(_DEV_ENV)
        assert "MINIO_PUBLIC_ENDPOINT" in env, (
            "dev_media_m8: media.env.example must declare MINIO_PUBLIC_ENDPOINT."
        )

    def test_dev_public_endpoint_is_loopback(self):
        env = _env_vars(_DEV_ENV)
        value = env.get("MINIO_PUBLIC_ENDPOINT", "")
        assert "127." in value, (
            "dev_media_m8: MINIO_PUBLIC_ENDPOINT must point at loopback (127.x.x.x) "
            f"for the dev stack. Got: {value!r}"
        )

    def test_worspace_declares_public_endpoint(self):
        env = _env_vars(_WORSPACE_ENV)
        assert "MINIO_PUBLIC_ENDPOINT" in env, (
            "worspace_dev_media_m8: media.env.example must declare MINIO_PUBLIC_ENDPOINT."
        )

    def test_worspace_public_endpoint_is_loopback(self):
        env = _env_vars(_WORSPACE_ENV)
        value = env.get("MINIO_PUBLIC_ENDPOINT", "")
        assert "127." in value, (
            "worspace_dev_media_m8: MINIO_PUBLIC_ENDPOINT must point at loopback (127.x.x.x) "
            f"for the dev stack. Got: {value!r}"
        )


# ---------------------------------------------------------------------------
# dev_media_m8 — host-port policy (unchanged)
# ---------------------------------------------------------------------------


class TestDevMinioLoopbackOnly:
    """In the dev stack MinIO ports must be loopback-bound (127.0.0.1), never 0.0.0.0."""

    def _minio_ports(self) -> list[str]:
        compose = _load(_DEV)
        return compose["services"]["minio"].get("ports", [])

    def test_minio_has_ports_block(self):
        """Dev stack must still expose MinIO for local tooling."""
        assert self._minio_ports(), (
            "dev_media_m8: minio has no `ports:` block — "
            "the dev stack should expose MinIO on loopback for local mc/dashboard access."
        )

    @pytest.mark.parametrize("mapping", ["127.0.0.1:9005:9000", "127.0.0.1:9006:9001"])
    def test_minio_port_is_loopback_bound(self, mapping: str):
        ports = self._minio_ports()
        assert mapping in ports, (
            f"dev_media_m8: expected loopback port mapping {mapping!r} not found. "
            f"Got: {ports}"
        )

    def test_no_minio_port_on_all_interfaces(self):
        for mapping in self._minio_ports():
            parts = str(mapping).split(":")
            if len(parts) == 3:
                host_ip = parts[0]
                assert _LOOPBACK_RE.match(host_ip), (
                    f"dev_media_m8: minio port {mapping!r} binds on {host_ip!r}, "
                    "not loopback — change to 127.0.0.1:<host>:<container>."
                )
            elif len(parts) == 2:
                pytest.fail(
                    f"dev_media_m8: minio port {mapping!r} has no explicit host IP "
                    "(defaults to 0.0.0.0). Change to 127.0.0.1:<host>:<container>."
                )
