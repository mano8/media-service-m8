"""Static compose-policy tests for the Docker socket / Traefik provider (item 0.3).

Decision (2026-06-24): **no** stack — hardened *or* dev — mounts the raw Docker
socket. Every media stack routes exclusively through the Traefik **file
provider**; the Docker provider (which requires the host root-equivalent socket)
is gone everywhere. Backends are declared statically in ``dynamic_conf.yml`` and
resolve over the Docker network's embedded DNS by container name, so routes still
resolve without the socket.

These tests parse the YAML files directly — no running Docker required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_COMPOSE_DIR = Path(__file__).parent.parent / "docker_compose"
_STACKS = ("dev_media_m8", "worspace_dev_media_m8", "hardened_media_m8")
_SOCKET = "/var/run/docker.sock"

# Container-DNS backends every stack's file-provider config must declare; each
# must resolve to a real compose service so routing works without the socket.
_EXPECTED_BACKENDS = {
    "auth-service": "http://auth_user_service:8000",
    "media-service": "http://media_service:8000",
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _compose(stack: str) -> Path:
    return _COMPOSE_DIR / stack / "docker-compose.yml"


def _traefik(stack: str) -> Path:
    return _COMPOSE_DIR / stack / "traefik" / "traefik.yml"


def _dynamic(stack: str) -> Path:
    return _COMPOSE_DIR / stack / "traefik" / "dynamic_conf.yml"


def _service_volumes(service: dict) -> list[str]:
    return [v for v in service.get("volumes") or [] if isinstance(v, str)]


# ---------------------------------------------------------------------------
# Socketless — no stack mounts the Docker socket
# ---------------------------------------------------------------------------


class TestNoStackMountsDockerSocket:
    @pytest.mark.parametrize("stack", _STACKS)
    def test_no_service_mounts_docker_socket(self, stack: str):
        services = _load(_compose(stack))["services"]
        for name, service in services.items():
            for mount in _service_volumes(service):
                assert _SOCKET not in mount, (
                    f"{stack}:{name} mounts the Docker socket ({mount}) — the "
                    "Docker API is equivalent to host root. Route via the Traefik "
                    "file provider instead."
                )


# ---------------------------------------------------------------------------
# File provider only — no Docker provider, no discovery labels
# ---------------------------------------------------------------------------


class TestFileProviderOnly:
    @pytest.mark.parametrize("stack", _STACKS)
    def test_traefik_uses_file_provider_only(self, stack: str):
        providers = _load(_traefik(stack))["providers"]
        assert "file" in providers, f"{stack}: file provider missing"
        assert "docker" not in providers, (
            f"{stack}: Traefik must not enable the `docker` provider — it "
            "requires the host root-equivalent Docker socket."
        )

    @pytest.mark.parametrize("stack", _STACKS)
    def test_no_traefik_discovery_labels(self, stack: str):
        services = _load(_compose(stack))["services"]
        for name, service in services.items():
            labels = service.get("labels") or []
            keys = labels.keys() if isinstance(labels, dict) else labels
            assert not any(str(k).startswith("traefik") for k in keys), (
                f"{stack}:{name} declares a traefik discovery label — the file "
                "provider makes it unnecessary."
            )


# ---------------------------------------------------------------------------
# Routes still resolve via the file provider
# ---------------------------------------------------------------------------


class TestRoutesStillResolve:
    @pytest.mark.parametrize("stack", _STACKS)
    def test_routers_resolve_to_defined_services(self, stack: str):
        conf = _load(_dynamic(stack))["http"]
        defined = set(conf["services"])
        for name, router in conf["routers"].items():
            service = router["service"]
            # api@internal is Traefik's built-in dashboard service, not declared.
            if service == "api@internal":
                continue
            assert service in defined, (
                f"{stack}: router {name} targets undeclared service {service}"
            )

    @pytest.mark.parametrize("stack", _STACKS)
    def test_backends_use_container_dns(self, stack: str):
        conf = _load(_dynamic(stack))["http"]
        compose_services = set(_load(_compose(stack))["services"])
        for name, expected_url in _EXPECTED_BACKENDS.items():
            servers = conf["services"][name]["loadBalancer"]["servers"]
            urls = [s["url"] for s in servers]
            assert urls == [expected_url], (stack, name, urls)
            host = expected_url.removeprefix("http://").split(":", 1)[0]
            assert host in compose_services, f"{stack}: {host} is not a compose service"
