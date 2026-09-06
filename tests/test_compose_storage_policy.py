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
  dev_media_m8, dev_local_media_m8, worspace_dev_media_m8 — backend is
                       SeaweedFS too (`T20-dev-stacks-port`, applying
                       `T15`-`T17` to the three dev stacks). The `storage`
                       service must publish exactly one loopback-bound host
                       port for the S3 gateway (never 0.0.0.0) and no other
                       port — the admin/filer surfaces are loopback-bound
                       inside the container, same as hardened, and are never
                       published even in dev. S3_CORS_ALLOW_ORIGIN (.env) must
                       be set and must NOT be a wildcard; media.env.example
                       must declare S3_PUBLIC_ENDPOINT starting with loopback.

Only the env-var lookups this file asserts on for the *application* side moved
to `S3_*` (T10-T12) before this step; `hardened_media_m8`'s own storage
container/bootstrap vocabulary moved to SeaweedFS terms in `T18`, and the dev
stacks follow here in `T20` — MinIO/`minio`/`MINIO_API_CORS_ALLOW_ORIGIN`
literals are gone from every stack this suite covers.
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
_DEV_DOTENV = _COMPOSE_DIR / "dev_media_m8" / ".env.example"
_DEV_LOCAL = _COMPOSE_DIR / "dev_local_media_m8" / "docker-compose.yml"
_DEV_LOCAL_ENV = _COMPOSE_DIR / "dev_local_media_m8" / "media.env.example"
_DEV_LOCAL_DOTENV = _COMPOSE_DIR / "dev_local_media_m8" / ".env.example"
_WORSPACE = _COMPOSE_DIR / "worspace_dev_media_m8" / "docker-compose.yml"
_WORSPACE_ENV = _COMPOSE_DIR / "worspace_dev_media_m8" / "media.env.example"
_WORSPACE_DOTENV = _COMPOSE_DIR / "worspace_dev_media_m8" / ".env.example"

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
# CORS policy — dev stacks (SeaweedFS, T20)
# ---------------------------------------------------------------------------


class TestDevStorageCorsBootstrap:
    """Every dev stack's storage-init bootstrap must read S3_CORS_ALLOW_ORIGIN
    (.env) and it must NOT be the wildcard '*' — same contract as
    TestHardenedStorageCorsBootstrap, applied to the three dev stacks."""

    @pytest.mark.parametrize(
        "stack_name,dotenv_path",
        [
            ("dev_media_m8", _DEV_DOTENV),
            ("dev_local_media_m8", _DEV_LOCAL_DOTENV),
            ("worspace_dev_media_m8", _WORSPACE_DOTENV),
        ],
    )
    def test_cors_origin_is_set(self, stack_name: str, dotenv_path: Path):
        env = _env_vars(dotenv_path)
        assert env.get("S3_CORS_ALLOW_ORIGIN"), (
            f"{stack_name}: .env.example must declare S3_CORS_ALLOW_ORIGIN "
            "(scoped to the UI origin, never *)."
        )

    @pytest.mark.parametrize(
        "stack_name,dotenv_path",
        [
            ("dev_media_m8", _DEV_DOTENV),
            ("dev_local_media_m8", _DEV_LOCAL_DOTENV),
            ("worspace_dev_media_m8", _WORSPACE_DOTENV),
        ],
    )
    def test_cors_origin_is_not_wildcard(self, stack_name: str, dotenv_path: Path):
        value = _env_vars(dotenv_path).get("S3_CORS_ALLOW_ORIGIN", "")
        assert "*" not in value, (
            f"{stack_name}: S3_CORS_ALLOW_ORIGIN must NOT contain '*' — "
            f"scope it to the specific UI origin(s). Got: {value!r}"
        )

    @pytest.mark.parametrize(
        "stack_name,compose_path",
        [
            ("dev_media_m8", _DEV),
            ("dev_local_media_m8", _DEV_LOCAL),
            ("worspace_dev_media_m8", _WORSPACE),
        ],
    )
    def test_storage_init_guards_wildcard_origin(
        self, stack_name: str, compose_path: Path
    ):
        entrypoint = _load(compose_path)["services"]["storage-init"]["entrypoint"]
        script = entrypoint[-1]
        assert "S3_CORS_ALLOW_ORIGIN" in script and "*" in script, (
            f"{stack_name}: storage-init must still refuse to apply a "
            "wildcard S3_CORS_ALLOW_ORIGIN at bootstrap time — this guard is "
            "the last line of defense if the .env value is ever misconfigured."
        )
        assert "exit 1" in script, (
            f"{stack_name}: storage-init's wildcard-origin guard must abort "
            "the bootstrap (exit 1), not merely warn."
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

    def test_dev_local_declares_public_endpoint(self):
        env = _env_vars(_DEV_LOCAL_ENV)
        assert "S3_PUBLIC_ENDPOINT" in env, (
            "dev_local_media_m8: media.env.example must declare S3_PUBLIC_ENDPOINT."
        )

    def test_dev_local_public_endpoint_is_loopback(self):
        env = _env_vars(_DEV_LOCAL_ENV)
        value = env.get("S3_PUBLIC_ENDPOINT", "")
        assert "127." in value, (
            "dev_local_media_m8: S3_PUBLIC_ENDPOINT must point at loopback (127.x.x.x) "
            f"for the dev stack. Got: {value!r}"
        )


# ---------------------------------------------------------------------------
# dev stacks — storage host-port policy (T20)
# ---------------------------------------------------------------------------

_DEV_STACKS = [
    ("dev_media_m8", _DEV),
    ("dev_local_media_m8", _DEV_LOCAL),
    ("worspace_dev_media_m8", _WORSPACE),
]


class TestDevStorageLoopbackOnly:
    """In every dev stack the storage backend must publish exactly one
    loopback-bound (127.0.0.1) host port — the S3 gateway — never 0.0.0.0, and
    never a second port for the admin/filer surfaces (those are loopback-bound
    *inside* the container by the boot command, same as hardened_media_m8)."""

    def _storage_ports(self, compose_path: Path) -> list[str]:
        return _load(compose_path)["services"]["storage"].get("ports", [])

    @pytest.mark.parametrize("stack_name,compose_path", _DEV_STACKS)
    def test_storage_has_ports_block(self, stack_name: str, compose_path: Path):
        """Dev stacks must still expose the S3 gateway for local tooling."""
        assert self._storage_ports(compose_path), (
            f"{stack_name}: storage has no `ports:` block — the dev stack "
            "should expose the S3 gateway on loopback for local mc/aws-cli access."
        )

    @pytest.mark.parametrize("stack_name,compose_path", _DEV_STACKS)
    def test_storage_publishes_only_the_s3_gateway(
        self, stack_name: str, compose_path: Path
    ):
        ports = self._storage_ports(compose_path)
        assert ports == ["127.0.0.1:9005:8333"], (
            f"{stack_name}: storage must publish exactly the loopback S3 "
            f"gateway mapping '127.0.0.1:9005:8333' and nothing else — the "
            f"admin/filer surfaces must never be published, even in dev. "
            f"Got: {ports}"
        )

    @pytest.mark.parametrize("stack_name,compose_path", _DEV_STACKS)
    def test_no_storage_port_on_all_interfaces(
        self, stack_name: str, compose_path: Path
    ):
        for mapping in self._storage_ports(compose_path):
            parts = str(mapping).split(":")
            if len(parts) == 3:
                host_ip = parts[0]
                assert _LOOPBACK_RE.match(host_ip), (
                    f"{stack_name}: storage port {mapping!r} binds on "
                    f"{host_ip!r}, not loopback — change to "
                    "127.0.0.1:<host>:<container>."
                )
            elif len(parts) == 2:
                pytest.fail(
                    f"{stack_name}: storage port {mapping!r} has no explicit "
                    "host IP (defaults to 0.0.0.0). Change to "
                    "127.0.0.1:<host>:<container>."
                )
