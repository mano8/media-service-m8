"""Static compose-policy tests for the storage backend's host-port exposure
(item 0.2), the browser-direct presigned upload/download ingress (Phase 4),
and the SeaweedFS hardening/bootstrap invariants closed in Wave 3
(`T15`-`T18`).

These tests parse the YAML files directly — no running Docker required. Each
test named after a contract invariant id (`media-sdk-m8/tests/conformance/
CONTRACT.md`) is that invariant's proof for this suite: delete the setting it
checks and the test must fail, per that contract's own acceptance rule.

Policy:
  hardened_media_m8  — backend is SeaweedFS (`T15`-`T17`). The `storage`
                       service must have NO `ports:` block at all
                       (internal-only, S1) and must carry the same hardening
                       as every other service in the stack — no-new-privileges,
                       cap_drop: ALL, read_only, deploy.resources.limits (S15).
                       Its boot command must bind every admin surface
                       (master/volume/filer/webdav) to loopback and advertise
                       loopback too, leaving only the S3 gateway reachable from
                       siblings.
                     — Traefik storage router must be on websecure (TLS) with
                       tls:{}, route by Host (not bare /), exclude the two
                       non-S3 liveness paths SeaweedFS serves on the S3 port
                       (`/healthz`, `/status`), and use a media-storage backend
                       with passHostHeader:true at http://storage:8333 (S6).
                     — S3_CORS_ALLOW_ORIGIN (root .env, read by storage-init)
                       must be set and must NOT be a wildcard, and the
                       bootstrap script itself must still refuse to apply a
                       wildcard origin (S3).
                     — media.env.example must declare S3_PUBLIC_ENDPOINT
                       starting with https://.
  dev_media_m8       — backend is still MinIO (`T20` migrates the dev stacks;
                       out of scope here). MinIO ports must be loopback-bound
                       only (no 0.0.0.0 bind); MINIO_API_CORS_ALLOW_ORIGIN must
                       be set and must NOT be *; media.env.example must declare
                       S3_PUBLIC_ENDPOINT starting with loopback.
  worspace_dev_media_m8 — same MinIO CORS + env.example assertions as dev.

Only the env-var lookups this file asserts on for the *application* side moved
to `S3_*` (T10-T12) before this step; `hardened_media_m8`'s own storage
container/bootstrap vocabulary moves to SeaweedFS terms here (T18), while
`dev_media_m8`/`worspace_dev_media_m8` stay on MinIO/`minio`/
`MINIO_API_CORS_ALLOW_ORIGIN` literals until `T20`.
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
_HARDENED_DOTENV = _COMPOSE_DIR / "hardened_media_m8" / ".env.example"
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
# hardened_media_m8 — host-port policy (S1)
# ---------------------------------------------------------------------------


class TestHardenedStorageNoHostPorts:
    """In the hardened stack the storage backend must not publish any host
    port — it is reachable only on the Docker network (S1)."""

    def test_hardened_storage_publishes_no_host_ports(self):
        compose = _load(_HARDENED)
        storage = compose["services"]["storage"]
        assert "ports" not in storage, (
            "hardened_media_m8: storage must not have a `ports:` block — "
            "it must be reachable only on the Docker network (storage:8333). "
            f"Got: {storage.get('ports')}"
        )


# ---------------------------------------------------------------------------
# hardened_media_m8 — storage backend admin-surface binding (input to S2,
# proved live against a real container by T3-run-seaweedfs / T1-conformance-
# harness; this is the static half — the boot command itself must still ask
# for loopback binding)
# ---------------------------------------------------------------------------


class TestHardenedStorageAdminSurfaceLoopbackOnly:
    """The storage service's boot command must bind every admin surface
    (master/volume/filer/webdav) to loopback and advertise loopback too,
    leaving only the S3 gateway reachable from sibling containers."""

    def _command(self) -> list[str]:
        compose = _load(_HARDENED)
        return compose["services"]["storage"].get("command", [])

    def test_storage_command_binds_admin_surfaces_to_loopback(self):
        command = self._command()
        assert "-ip.bind=127.0.0.1" in command, (
            "hardened_media_m8: storage command must include "
            f"'-ip.bind=127.0.0.1' to bind master/volume/filer/webdav to the "
            f"container's own loopback. Got: {command!r}"
        )

    def test_storage_command_advertises_loopback(self):
        command = self._command()
        assert "-ip=localhost" in command, (
            "hardened_media_m8: storage command must include '-ip=localhost' "
            "so the address components advertise agrees with -ip.bind — "
            "without it the filer's own chunk upload to the volume server "
            "dials the routable address and every write fails (measured as "
            f"D1 in the migration matrix). Got: {command!r}"
        )

    def test_storage_command_exposes_only_s3_gateway_to_siblings(self):
        command = self._command()
        assert "-s3.ip.bind=0.0.0.0" in command, (
            "hardened_media_m8: storage command must include "
            f"'-s3.ip.bind=0.0.0.0' — the S3 gateway is the only surface "
            f"siblings (Traefik included) may reach. Got: {command!r}"
        )
        assert "-ip.bind=0.0.0.0" not in command, (
            "hardened_media_m8: storage command must not bind the admin "
            f"address components to 0.0.0.0. Got: {command!r}"
        )


# ---------------------------------------------------------------------------
# hardened_media_m8 — Traefik storage router (Phase 4 / S6)
# ---------------------------------------------------------------------------


class TestHardenedTraefikStorageRouter:
    """The hardened stack must expose the S3 data path via a Traefik storage
    router that is TLS-only, Host-pinned, forwards the original Host, and
    explicitly excludes the two non-S3 liveness paths SeaweedFS serves on the
    S3 gateway port (S6)."""

    def _traefik(self) -> dict:
        return _load(_HARDENED_TRAEFIK)

    def test_media_storage_router_exists(self):
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
            "S3_PUBLIC_ENDPOINT is https:// and the route must be TLS-only."
        )

    def test_storage_router_rule_uses_host(self):
        router = self._traefik()["http"]["routers"]["media-storage-router"]
        rule = router.get("rule", "")
        assert "Host(" in rule, (
            f"hardened_media_m8: media-storage-router rule must match by Host(), "
            f"not a bare PathPrefix. Got: {rule!r}"
        )

    def test_storage_router_excludes_non_s3_liveness_paths(self):
        router = self._traefik()["http"]["routers"]["media-storage-router"]
        rule = router.get("rule", "")
        assert "PathPrefix(`/healthz`)" in rule and "PathPrefix(`/status`)" in rule, (
            "hardened_media_m8: media-storage-router rule must exclude the two "
            "non-S3 liveness paths SeaweedFS serves unauthenticated on the S3 "
            f"gateway port (`/healthz`, `/status`). Got: {rule!r}"
        )
        assert re.search(r"!\s*\(", rule), (
            "hardened_media_m8: the liveness-path exclusion must be a negated "
            f"group, not merely mentioned in the rule. Got: {rule!r}"
        )

    def test_media_storage_service_exists(self):
        services = self._traefik()["http"]["services"]
        assert "media-storage" in services, (
            "hardened_media_m8: traefik/dynamic_conf.yml must define a "
            "'media-storage' Traefik service."
        )

    def test_media_storage_backend_url(self):
        lb = self._traefik()["http"]["services"]["media-storage"]["loadBalancer"]
        urls = [s["url"] for s in lb.get("servers", [])]
        assert "http://storage:8333" in urls, (
            "hardened_media_m8: media-storage backend must point to the S3 "
            f"gateway 'http://storage:8333'. Got: {urls!r}"
        )

    def test_media_storage_pass_host_header(self):
        lb = self._traefik()["http"]["services"]["media-storage"]["loadBalancer"]
        assert lb.get("passHostHeader") is True, (
            "hardened_media_m8: media-storage loadBalancer must set "
            "'passHostHeader: true' — GET SigV4 signatures bind the Host header "
            "and the proxy must forward it unchanged for signatures to validate."
        )

    def test_storage_router_service_is_media_storage(self):
        router = self._traefik()["http"]["routers"]["media-storage-router"]
        assert router.get("service") == "media-storage", (
            "hardened_media_m8: media-storage-router must route to the "
            f"'media-storage' service. Got: {router.get('service')!r}"
        )

    def test_public_storage_route_is_tls_and_host_pinned(self):
        """Contract id S6, proved in one place: the route is TLS-only,
        Host-pinned, and forwards the Host unchanged to the backend. Deleting
        any one of `tls`, the `Host()` rule term, or `passHostHeader: true`
        must fail this test."""
        traefik = self._traefik()
        router = traefik["http"]["routers"]["media-storage-router"]
        service = traefik["http"]["services"][router.get("service", "")]
        lb = service["loadBalancer"]
        rule = router.get("rule", "")
        assert "websecure" in router.get("entryPoints", [])
        assert "tls" in router
        assert "Host(" in rule
        assert lb.get("passHostHeader") is True


# ---------------------------------------------------------------------------
# hardened_media_m8 — storage container hardening (S15)
# ---------------------------------------------------------------------------


class TestHardenedStorageServiceHardening:
    """The storage container must carry the same hardening as every other
    service in the hardened stack: no-new-privileges, cap_drop: ALL,
    read_only, and deploy.resources.limits (S15) — a gap that was open before
    this migration."""

    def _storage(self) -> dict:
        compose = _load(_HARDENED)
        return compose["services"]["storage"]

    def test_storage_service_carries_standard_hardening(self):
        storage = self._storage()

        security_opt = storage.get("security_opt", [])
        assert "no-new-privileges:true" in security_opt, (
            "hardened_media_m8: storage must set "
            f"'security_opt: [no-new-privileges:true]'. Got: {security_opt!r}"
        )

        assert storage.get("cap_drop") == ["ALL"], (
            "hardened_media_m8: storage must set 'cap_drop: [ALL]'. "
            f"Got: {storage.get('cap_drop')!r}"
        )

        assert storage.get("read_only") is True, (
            "hardened_media_m8: storage must set 'read_only: true'. "
            f"Got: {storage.get('read_only')!r}"
        )

        limits = storage.get("deploy", {}).get("resources", {}).get("limits", {})
        assert limits.get("cpus") and limits.get("memory"), (
            "hardened_media_m8: storage must set "
            "'deploy.resources.limits.{cpus,memory}'. "
            f"Got: {limits!r}"
        )


# ---------------------------------------------------------------------------
# CORS policy — dev / worspace stacks, still MinIO (T20 migrates these)
# ---------------------------------------------------------------------------


class TestMinioCorsNotWildcard:
    """Every still-MinIO dev stack must set MINIO_API_CORS_ALLOW_ORIGIN and
    it must NOT be the wildcard '*'. hardened_media_m8 no longer has a minio
    service — its CORS bootstrap is TestHardenedStorageCorsBootstrap below."""

    @pytest.mark.parametrize(
        "stack_name,compose_path",
        [
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
# hardened_media_m8 — storage-init CORS bootstrap (S3)
# ---------------------------------------------------------------------------


class TestHardenedStorageCorsBootstrap:
    """SeaweedFS has no MINIO_API_CORS_ALLOW_ORIGIN equivalent — CORS is a
    per-bucket PutBucketCors call the storage-init one-shot issues from
    S3_CORS_ALLOW_ORIGIN (root .env). It must be set, scoped to the UI
    origin, never a wildcard, and the bootstrap script itself must still
    refuse to apply a wildcard origin even if the .env value is ever
    misconfigured (S3)."""

    def _storage_init_script(self) -> str:
        compose = _load(_HARDENED)
        entrypoint = compose["services"]["storage-init"]["entrypoint"]
        # ["/bin/sh", "-c", "<script>"] — the script is the last element.
        return entrypoint[-1]

    def test_storage_cors_is_scoped_to_ui_origin(self):
        env = _env_vars(_HARDENED_DOTENV)
        value = env.get("S3_CORS_ALLOW_ORIGIN", "")
        assert value, (
            "hardened_media_m8: .env.example must declare "
            "S3_CORS_ALLOW_ORIGIN, scoped to the UI origin(s) allowed on the "
            "presigned data path."
        )
        assert "*" not in value, (
            "hardened_media_m8: S3_CORS_ALLOW_ORIGIN must NOT contain '*' — "
            f"scope it to the specific UI origin(s). Got: {value!r}"
        )

        script = self._storage_init_script()
        assert "S3_CORS_ALLOW_ORIGIN" in script and "*" in script, (
            "hardened_media_m8: storage-init must still refuse to apply a "
            "wildcard S3_CORS_ALLOW_ORIGIN at bootstrap time — this guard is "
            "the last line of defense if the .env value is ever misconfigured."
        )
        assert "exit 1" in script, (
            "hardened_media_m8: storage-init's wildcard-origin guard must "
            "abort the bootstrap (exit 1), not merely warn."
        )


# ---------------------------------------------------------------------------
# S3_PUBLIC_ENDPOINT in env.example — all stacks (Phase 4)
# ---------------------------------------------------------------------------


class TestStoragePublicEndpointEnvExample:
    """Every stack's media.env.example must declare S3_PUBLIC_ENDPOINT.
    Dev/worspace stacks must point at loopback; hardened must use https://."""

    def test_hardened_declares_public_endpoint(self):
        env = _env_vars(_HARDENED_ENV)
        assert "S3_PUBLIC_ENDPOINT" in env, (
            "hardened_media_m8: media.env.example must declare S3_PUBLIC_ENDPOINT."
        )

    def test_hardened_public_endpoint_is_https(self):
        env = _env_vars(_HARDENED_ENV)
        value = env.get("S3_PUBLIC_ENDPOINT", "")
        assert value.startswith("https://"), (
            "hardened_media_m8: S3_PUBLIC_ENDPOINT must start with 'https://' — "
            f"the storage router is on websecure (TLS). Got: {value!r}"
        )

    def test_dev_declares_public_endpoint(self):
        env = _env_vars(_DEV_ENV)
        assert "S3_PUBLIC_ENDPOINT" in env, (
            "dev_media_m8: media.env.example must declare S3_PUBLIC_ENDPOINT."
        )

    def test_dev_public_endpoint_is_loopback(self):
        env = _env_vars(_DEV_ENV)
        value = env.get("S3_PUBLIC_ENDPOINT", "")
        assert "127." in value, (
            "dev_media_m8: S3_PUBLIC_ENDPOINT must point at loopback (127.x.x.x) "
            f"for the dev stack. Got: {value!r}"
        )

    def test_worspace_declares_public_endpoint(self):
        env = _env_vars(_WORSPACE_ENV)
        assert "S3_PUBLIC_ENDPOINT" in env, (
            "worspace_dev_media_m8: media.env.example must declare S3_PUBLIC_ENDPOINT."
        )

    def test_worspace_public_endpoint_is_loopback(self):
        env = _env_vars(_WORSPACE_ENV)
        value = env.get("S3_PUBLIC_ENDPOINT", "")
        assert "127." in value, (
            "worspace_dev_media_m8: S3_PUBLIC_ENDPOINT must point at loopback (127.x.x.x) "
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
