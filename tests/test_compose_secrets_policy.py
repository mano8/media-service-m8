"""Static compose-policy tests for item 6.1: _FILE mount secrets in the production overlay.

Plan reference: 6.1 — Wire _FILE mount secrets into the media production overlay.
Secrets sourced from /run/secrets/ via Docker secrets + *_FILE env vars so they
never appear in `docker inspect`.

Validates:
- docker-compose.production.yml exists and has a top-level `secrets:` block.
- Each Docker secret maps to a host file under ./secrets/<name>.txt.
- The production overlay declares all five _FILE categories called out in plan 6.1:
    DB_PASSWORD_FILE, REDIS_PASSWORD_FILE (MEDIA_REDIS_PASSWORD_FILE),
    MEDIA_INTERNAL_SERVICE_TOKEN_FILE, MEDIA_SHARE_SIGNING_SECRET_FILE,
    and MinIO credential files (MINIO_ACCESS_KEY_FILE / MINIO_SECRET_KEY_FILE).
- No literal `changethis` placeholder appears in the overlay YAML itself.
- Production env examples omit the plaintext secret fields that are _FILE-sourced
  (the values must not appear as uncommented KEY=VALUE lines).
- The production Traefik config replaces localhost Host rules with FQDN placeholders.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from yaml import SafeLoader


# Docker Compose v2.24+ uses non-standard YAML merge tags (!override, !reset).
# Register them so PyYAML's SafeLoader can parse the production overlay file.
# !override: replace the node entirely (return value as-is).
# !reset:    clear the node (empty sequence → [], empty mapping → {}).
def _override_constructor(loader: SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


def _reset_constructor(loader: SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


SafeLoader.add_constructor("!override", _override_constructor)
SafeLoader.add_constructor("!reset", _reset_constructor)

_COMPOSE_DIR = Path(__file__).parent.parent / "docker_compose" / "hardened_media_m8"
_PRODUCTION_OVERLAY = _COMPOSE_DIR / "docker-compose.production.yml"
_TRAEFIK_PROD = _COMPOSE_DIR / "traefik" / "production_dynamic_conf.yml"

_AUTH_ENV_PROD = _COMPOSE_DIR / "auth.env.production.example"
_MEDIA_ENV_PROD = _COMPOSE_DIR / "media.env.production.example"
_WORKER_ENV_PROD = _COMPOSE_DIR / "worker.env.production.example"

_PRODUCTION_ENV_EXAMPLES = [_AUTH_ENV_PROD, _MEDIA_ENV_PROD, _WORKER_ENV_PROD]

# ── Secrets that must be declared in the top-level `secrets:` block ───────────

_REQUIRED_SECRETS = {
    "db_password",
    "auth_redis_password",
    "media_redis_password",
    "media_internal_service_token",
    "media_share_signing_secret",
    "minio_access_key",
    "minio_secret_key",
    "private_api_secret",
    "refresh_secret_key",
    "event_signing_key",
    "session_secret",
    "tokens_encryption_key",
}

# ── _FILE env vars required on each service (plan 6.1 explicit list) ─────────

# DB_PASSWORD_FILE
_DB_FILE_VAR = "DB_PASSWORD_FILE"

# REDIS_PASSWORD_FILE (auth service) / MEDIA_REDIS_PASSWORD_FILE (media services)
_AUTH_REDIS_FILE_VAR = "REDIS_PASSWORD_FILE"
_MEDIA_REDIS_FILE_VAR = "MEDIA_REDIS_PASSWORD_FILE"

# MEDIA_INTERNAL_SERVICE_TOKEN_FILE
_TOKEN_FILE_VAR = "MEDIA_INTERNAL_SERVICE_TOKEN_FILE"

# MEDIA_SHARE_SIGNING_SECRET_FILE
_SHARE_FILE_VAR = "MEDIA_SHARE_SIGNING_SECRET_FILE"

# MinIO credential files
_MINIO_KEY_FILE_VAR = "MINIO_ACCESS_KEY_FILE"
_MINIO_SECRET_FILE_VAR = "MINIO_SECRET_KEY_FILE"

# Plaintext secret keys that must NOT be set in production env examples.
# Each value is the env var name that should be absent / commented out.
_SECRET_FIELDS_ABSENT_IN_AUTH = {
    "DB_PASSWORD",
    "REDIS_PASSWORD",
    "PRIVATE_API_SECRET",
    "REFRESH_SECRET_KEY",
    "EVENT_SIGNING_KEY",
    "SESSION_SECRET",
    "TOKENS_ENCRYPTION_KEY",
}

_SECRET_FIELDS_ABSENT_IN_MEDIA = {
    "DB_PASSWORD",
    "MEDIA_REDIS_PASSWORD",
    "MEDIA_INTERNAL_SERVICE_TOKEN",
    "MEDIA_SHARE_SIGNING_SECRET",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "PRIVATE_API_SECRET",
    "REFRESH_SECRET_KEY",
    "EVENT_SIGNING_KEY",
}

_SECRET_FIELDS_ABSENT_IN_WORKER = {
    "MEDIA_INTERNAL_SERVICE_TOKEN",
    "MEDIA_REDIS_PASSWORD",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
}


def _load_overlay() -> dict:
    return yaml.safe_load(_PRODUCTION_OVERLAY.read_text())


def _service_env(overlay: dict, service: str) -> dict:
    """Return the `environment:` block of a service as a dict (handles list or dict form)."""
    raw = overlay.get("services", {}).get(service, {}).get("environment") or {}
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for item in raw:
            if "=" in item:
                k, _, v = item.partition("=")
                result[k.strip()] = v.strip()
            else:
                result[item.strip()] = ""
        return result
    return dict(raw)


def _active_lines(env_file: Path) -> set[str]:
    """Return the set of KEY= prefixes for non-comment, non-blank lines in env_file."""
    active: set[str] = set()
    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key = stripped.split("=", 1)[0].strip()
        if key:
            active.add(key)
    return active


# ── 1. Overlay file existence ─────────────────────────────────────────────────


class TestProductionOverlayExists:
    def test_overlay_file_exists(self):
        assert _PRODUCTION_OVERLAY.exists(), (
            "docker-compose.production.yml missing from hardened_media_m8 — "
            "create it to enable plan 6.1 _FILE secret wiring."
        )

    def test_production_traefik_config_exists(self):
        assert _TRAEFIK_PROD.exists(), (
            "traefik/production_dynamic_conf.yml missing — required by "
            "docker-compose.production.yml for FQDN host rules."
        )

    @pytest.mark.parametrize("env_file", _PRODUCTION_ENV_EXAMPLES)
    def test_production_env_examples_exist(self, env_file: Path):
        assert env_file.exists(), (
            f"{env_file.name} missing — production env example required "
            "for plan 6.1 (non-secret config without plaintext secrets)."
        )


# ── 2. Docker secrets top-level block ────────────────────────────────────────


class TestDockerSecretsBlock:
    def test_secrets_block_declared(self):
        overlay = _load_overlay()
        assert "secrets" in overlay, (
            "No top-level `secrets:` block in docker-compose.production.yml — "
            "Docker secrets are required for _FILE mount wiring."
        )

    def test_required_secrets_declared(self):
        declared = set(_load_overlay().get("secrets", {}).keys())
        missing = _REQUIRED_SECRETS - declared
        assert not missing, (
            f"Missing secrets in docker-compose.production.yml `secrets:` block: "
            f"{sorted(missing)}"
        )

    def test_each_secret_maps_to_file_source(self):
        secrets = _load_overlay().get("secrets", {})
        for name in _REQUIRED_SECRETS:
            entry = secrets.get(name, {})
            assert "file" in entry, (
                f"Secret `{name}` in docker-compose.production.yml has no `file:` "
                "source — production secrets must reference operator-provisioned files."
            )
            assert entry["file"].startswith("./secrets/"), (
                f"Secret `{name}` file path should be under ./secrets/: {entry['file']!r}"
            )


# ── 3. Service-level secrets mounts ──────────────────────────────────────────


class TestServiceSecretsMounts:
    def _service_secrets(self, service: str) -> list[str]:
        overlay = _load_overlay()
        raw = overlay.get("services", {}).get(service, {}).get("secrets") or []
        return [s if isinstance(s, str) else s.get("source", "") for s in raw]

    @pytest.mark.parametrize("service", ["auth_user_service"])
    def test_auth_service_mounts_db_secret(self, service: str):
        assert "db_password" in self._service_secrets(service), (
            f"{service} must mount the `db_password` Docker secret."
        )

    @pytest.mark.parametrize("service", ["auth_user_service"])
    def test_auth_service_mounts_redis_secret(self, service: str):
        assert "auth_redis_password" in self._service_secrets(service), (
            f"{service} must mount the `auth_redis_password` Docker secret."
        )

    @pytest.mark.parametrize("service", ["media_service", "media_service_worker"])
    def test_media_service_mounts_media_internal_token(self, service: str):
        assert "media_internal_service_token" in self._service_secrets(service), (
            f"{service} must mount the `media_internal_service_token` Docker secret."
        )

    @pytest.mark.parametrize("service", ["media_service", "media_service_worker"])
    def test_media_service_mounts_share_signing_secret(self, service: str):
        assert "media_share_signing_secret" in self._service_secrets(service), (
            f"{service} must mount the `media_share_signing_secret` Docker secret."
        )

    @pytest.mark.parametrize(
        "service", ["media_service", "media_service_worker", "media_worker"]
    )
    def test_service_mounts_minio_credentials(self, service: str):
        secs = self._service_secrets(service)
        assert "minio_access_key" in secs, (
            f"{service} must mount the `minio_access_key` Docker secret."
        )
        assert "minio_secret_key" in secs, (
            f"{service} must mount the `minio_secret_key` Docker secret."
        )


# ── 4. _FILE env vars — plan 6.1 explicit categories ─────────────────────────


class TestFileMountEnvVars:
    """Verify the five _FILE categories from plan 6.1 are wired in the overlay."""

    def _env(self, service: str) -> dict:
        return _service_env(_load_overlay(), service)

    # 6.1 category: DB_PASSWORD_FILE
    @pytest.mark.parametrize(
        "service", ["auth_user_service", "media_service", "media_service_worker"]
    )
    def test_db_password_file_wired(self, service: str):
        env = self._env(service)
        assert _DB_FILE_VAR in env, (
            f"{service} must declare {_DB_FILE_VAR} in the production overlay "
            "(plan 6.1: DB password sourced from /run/secrets/)."
        )
        assert env[_DB_FILE_VAR].startswith("/run/secrets/"), (
            f"{service} {_DB_FILE_VAR} must point to /run/secrets/: {env[_DB_FILE_VAR]!r}"
        )

    # 6.1 category: REDIS_PASSWORD_FILE
    def test_auth_redis_password_file_wired(self):
        env = self._env("auth_user_service")
        assert _AUTH_REDIS_FILE_VAR in env, (
            f"auth_user_service must declare {_AUTH_REDIS_FILE_VAR} in the production "
            "overlay (plan 6.1: Redis password sourced from /run/secrets/)."
        )
        assert env[_AUTH_REDIS_FILE_VAR].startswith("/run/secrets/"), (
            f"{_AUTH_REDIS_FILE_VAR} must point to /run/secrets/: {env[_AUTH_REDIS_FILE_VAR]!r}"
        )

    @pytest.mark.parametrize(
        "service", ["media_service", "media_service_worker", "media_worker"]
    )
    def test_media_redis_password_file_wired(self, service: str):
        env = self._env(service)
        assert _MEDIA_REDIS_FILE_VAR in env, (
            f"{service} must declare {_MEDIA_REDIS_FILE_VAR} in the production overlay "
            "(plan 6.1: media Redis password sourced from /run/secrets/)."
        )
        assert env[_MEDIA_REDIS_FILE_VAR].startswith("/run/secrets/"), (
            f"{service} {_MEDIA_REDIS_FILE_VAR} must point to /run/secrets/."
        )

    # 6.1 category: MEDIA_INTERNAL_SERVICE_TOKEN_FILE
    @pytest.mark.parametrize(
        "service", ["media_service", "media_service_worker", "media_worker"]
    )
    def test_media_internal_service_token_file_wired(self, service: str):
        env = self._env(service)
        assert _TOKEN_FILE_VAR in env, (
            f"{service} must declare {_TOKEN_FILE_VAR} in the production overlay "
            "(plan 6.1: internal service token sourced from /run/secrets/)."
        )
        assert env[_TOKEN_FILE_VAR].startswith("/run/secrets/"), (
            f"{service} {_TOKEN_FILE_VAR} must point to /run/secrets/."
        )

    # 6.1 category: MEDIA_SHARE_SIGNING_SECRET_FILE
    @pytest.mark.parametrize("service", ["media_service", "media_service_worker"])
    def test_media_share_signing_secret_file_wired(self, service: str):
        env = self._env(service)
        assert _SHARE_FILE_VAR in env, (
            f"{service} must declare {_SHARE_FILE_VAR} in the production overlay "
            "(plan 6.1: share signing secret sourced from /run/secrets/)."
        )
        assert env[_SHARE_FILE_VAR].startswith("/run/secrets/"), (
            f"{service} {_SHARE_FILE_VAR} must point to /run/secrets/."
        )

    # 6.1 category: MinIO credential files
    @pytest.mark.parametrize(
        "service", ["media_service", "media_service_worker", "media_worker"]
    )
    def test_minio_access_key_file_wired(self, service: str):
        env = self._env(service)
        assert _MINIO_KEY_FILE_VAR in env, (
            f"{service} must declare {_MINIO_KEY_FILE_VAR} in the production overlay "
            "(plan 6.1: MinIO credentials sourced from /run/secrets/)."
        )
        assert env[_MINIO_KEY_FILE_VAR].startswith("/run/secrets/"), (
            f"{service} {_MINIO_KEY_FILE_VAR} must point to /run/secrets/."
        )

    @pytest.mark.parametrize(
        "service", ["media_service", "media_service_worker", "media_worker"]
    )
    def test_minio_secret_key_file_wired(self, service: str):
        env = self._env(service)
        assert _MINIO_SECRET_FILE_VAR in env, (
            f"{service} must declare {_MINIO_SECRET_FILE_VAR} in the production overlay "
            "(plan 6.1: MinIO credentials sourced from /run/secrets/)."
        )
        assert env[_MINIO_SECRET_FILE_VAR].startswith("/run/secrets/"), (
            f"{service} {_MINIO_SECRET_FILE_VAR} must point to /run/secrets/."
        )


# ── 5. No literal `changethis` in the overlay YAML ───────────────────────────


class TestNoLiteralSecretsInOverlay:
    def test_overlay_has_no_changethis_placeholder(self):
        raw = _PRODUCTION_OVERLAY.read_text()
        # The word "changethis" should not appear as a value — it is acceptable only
        # in comments (as a reminder). Check non-comment lines only.
        for lineno, line in enumerate(raw.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "changethis" not in stripped.lower(), (
                f"docker-compose.production.yml line {lineno} contains a literal "
                f"'changethis' placeholder — secrets must come from /run/secrets/: "
                f"{line!r}"
            )


# ── 6. Production env examples omit plaintext secret fields ──────────────────


class TestProductionEnvExamplesOmitSecrets:
    """Secret fields must be absent (or comment-only) in production env examples.

    They are sourced from /run/secrets/ at runtime via *_FILE env vars injected
    by docker-compose.production.yml — setting them here would leak them into
    `docker inspect` output.
    """

    def test_auth_env_production_omits_secret_fields(self):
        active = _active_lines(_AUTH_ENV_PROD)
        leaking = _SECRET_FIELDS_ABSENT_IN_AUTH & active
        assert not leaking, (
            f"auth.env.production.example sets these secret fields as plaintext: "
            f"{sorted(leaking)}. Remove them — they are sourced from /run/secrets/ "
            "via *_FILE env vars in docker-compose.production.yml."
        )

    def test_media_env_production_omits_secret_fields(self):
        active = _active_lines(_MEDIA_ENV_PROD)
        leaking = _SECRET_FIELDS_ABSENT_IN_MEDIA & active
        assert not leaking, (
            f"media.env.production.example sets these secret fields as plaintext: "
            f"{sorted(leaking)}. Remove them — they are sourced from /run/secrets/ "
            "via *_FILE env vars in docker-compose.production.yml."
        )

    def test_worker_env_production_omits_secret_fields(self):
        active = _active_lines(_WORKER_ENV_PROD)
        leaking = _SECRET_FIELDS_ABSENT_IN_WORKER & active
        assert not leaking, (
            f"worker.env.production.example sets these secret fields as plaintext: "
            f"{sorted(leaking)}. Remove them — they are sourced from /run/secrets/ "
            "via *_FILE env vars in docker-compose.production.yml."
        )


# ── 7. Production env examples set ENVIRONMENT=production ─────────────────────


class TestProductionEnvSetsEnvironment:
    @pytest.mark.parametrize(
        "env_file,label",
        [
            (_AUTH_ENV_PROD, "auth"),
            (_MEDIA_ENV_PROD, "media"),
        ],
    )
    def test_environment_is_production(self, env_file: Path, label: str):
        active = _active_lines(env_file)
        # ENVIRONMENT must appear as an active (non-commented) line
        assert "ENVIRONMENT" in active, (
            f"{env_file.name} must set ENVIRONMENT=production (not commented out)."
        )
        # Confirm the value is actually 'production'
        for line in env_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("ENVIRONMENT="):
                assert stripped == "ENVIRONMENT=production", (
                    f"{env_file.name}: expected ENVIRONMENT=production, got {stripped!r}"
                )
                return

    @pytest.mark.parametrize(
        "env_file,label",
        [
            (_AUTH_ENV_PROD, "auth"),
            (_MEDIA_ENV_PROD, "media"),
        ],
    )
    def test_strict_production_mode_enabled(self, env_file: Path, label: str):
        content = env_file.read_text()
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("STRICT_PRODUCTION_MODE="):
                assert stripped == "STRICT_PRODUCTION_MODE=true", (
                    f"{env_file.name}: expected STRICT_PRODUCTION_MODE=true, "
                    f"got {stripped!r}"
                )
                return
        pytest.fail(
            f"{env_file.name} must set STRICT_PRODUCTION_MODE=true "
            "(not commented out) for plan 6.1 production posture."
        )


# ── 8. Production Traefik config uses FQDN host rules ─────────────────────────


class TestProductionTraefikConfig:
    def _load(self) -> dict:
        return yaml.safe_load(_TRAEFIK_PROD.read_text())

    def test_no_localhost_host_rule(self):
        raw = _TRAEFIK_PROD.read_text()
        for lineno, line in enumerate(raw.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "Host(`localhost`)" not in stripped, (
                f"production_dynamic_conf.yml line {lineno} contains a "
                "Host(`localhost`) rule — replace with real FQDN in production: "
                f"{line!r}"
            )

    def test_tls_floor_is_tls13(self):
        conf = self._load()
        min_ver = (
            conf.get("tls", {})
            .get("options", {})
            .get("default", {})
            .get("minVersion", "")
        )
        assert min_ver == "VersionTLS13", (
            f"production_dynamic_conf.yml TLS floor should be VersionTLS13, "
            f"got {min_ver!r}. Production stacks must not accept TLS 1.2."
        )

    def test_security_contract_path_exclusions_present(self):
        raw = _TRAEFIK_PROD.read_text()
        for path in ("/user/metrics", "/user/private", "/media/metrics"):
            assert path in raw, (
                f"production_dynamic_conf.yml must preserve the security-contract "
                f"path exclusion for {path!r} — remove it and metrics / private API "
                "become reachable from the internet."
            )

    def test_all_backends_resolve_to_internal_urls(self):
        conf = self._load()
        for name, svc in (conf.get("http", {}).get("services") or {}).items():
            servers = svc.get("loadBalancer", {}).get("servers") or []
            for s in servers:
                url = s.get("url", "")
                assert url.startswith("http://"), (
                    f"production_dynamic_conf.yml backend {name!r} URL {url!r} "
                    "must be an internal http:// address (traffic stays on Docker network)."
                )
