"""Static compose-policy tests for Traefik routing contract (item 9.4).

Design B (item 9.4): /media/health is intentionally exposed on the public HTTPS
entrypoint because the ungated response body is a constant {"status":"ok"} —
no dependency state ever leaks to anonymous callers. The detail body is still
credential-gated (item 9.3, HEALTH_DETAIL_CREDENTIAL, fail-closed).

Assertions:
- /media/health is NOT excluded from the media-public-router rule in any stack.
- /media/metrics IS excluded from the media-public-router rule in every stack.
- /user/health is NOT excluded from the auth-public-router rule in any stack
  (issuer ungated body is a constant liveness response — same Design B contract).
- /user/private IS excluded from the auth-public-router rule in every stack
  (inter-service gate must remain invisible on the public entrypoint).
- /user/metrics IS excluded from the auth-public-router rule in every stack.

These tests parse the YAML files directly — no running Docker required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_COMPOSE_DIR = Path(__file__).parent.parent / "docker_compose"

# All stacks that have a public-facing media-public-router.
# The production overlay uses a separate production_dynamic_conf.yml.
_DEV_DYNAMIC_CONFS = [
    pytest.param(
        _COMPOSE_DIR / "dev_media_m8" / "traefik" / "dynamic_conf.yml",
        id="dev_media_m8",
    ),
    pytest.param(
        _COMPOSE_DIR / "dev_local_media_m8" / "traefik" / "dynamic_conf.yml",
        id="dev_local_media_m8",
    ),
    pytest.param(
        _COMPOSE_DIR / "worspace_dev_media_m8" / "traefik" / "dynamic_conf.yml",
        id="worspace_dev_media_m8",
    ),
    pytest.param(
        _COMPOSE_DIR / "hardened_media_m8" / "traefik" / "dynamic_conf.yml",
        id="hardened_media_m8",
    ),
]

_PRODUCTION_DYNAMIC_CONF = pytest.param(
    _COMPOSE_DIR / "hardened_media_m8" / "traefik" / "production_dynamic_conf.yml",
    id="production",
)

_ALL_DYNAMIC_CONFS = _DEV_DYNAMIC_CONFS + [_PRODUCTION_DYNAMIC_CONF]


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _router_rule(conf: dict, router_name: str) -> str:
    return conf["http"]["routers"][router_name]["rule"]


# ── /media/health — publicly exposed (9.4) ───────────────────────────────────


class TestMediaHealthPubliclyExposed:
    """9.4: /media/health must NOT appear in the media-public-router exclusion."""

    @pytest.mark.parametrize("conf_path", _ALL_DYNAMIC_CONFS)
    def test_media_health_not_excluded_from_public_router(self, conf_path: Path):
        conf = _load(conf_path)
        rule = _router_rule(conf, "media-public-router")
        assert "/media/health" not in rule, (
            f"{conf_path.name}: /media/health must not appear in the "
            "media-public-router exclusion (9.4 Design B — ungated body is "
            "a constant liveness response, detail is credential-gated by 9.3)"
        )


# ── /media/metrics — internal only (scrape credential) ───────────────────────


class TestMediaMetricsInternalOnly:
    """/media/metrics must remain excluded from the public entrypoint."""

    @pytest.mark.parametrize("conf_path", _ALL_DYNAMIC_CONFS)
    def test_media_metrics_excluded_from_public_router(self, conf_path: Path):
        conf = _load(conf_path)
        rule = _router_rule(conf, "media-public-router")
        assert "/media/metrics" in rule, (
            f"{conf_path.name}: /media/metrics must be excluded from the "
            "media-public-router (Prometheus scrape endpoint — internal only)"
        )


# ── /user/health — publicly exposed (9.4 Design B, symmetric with /media/health) ──


class TestUserHealthPubliclyExposed:
    """9.4: /user/health must NOT appear in the auth-public-router exclusion."""

    @pytest.mark.parametrize("conf_path", _ALL_DYNAMIC_CONFS)
    def test_user_health_not_excluded_from_auth_public_router(self, conf_path: Path):
        conf = _load(conf_path)
        rule = _router_rule(conf, "auth-public-router")
        assert "/user/health" not in rule, (
            f"{conf_path.name}: /user/health must not appear in the "
            "auth-public-router exclusion (9.4 Design B — issuer ungated body is "
            "a constant liveness response, detail is credential-gated by 9.3)"
        )


# ── /user/private and /user/metrics — internal only ──────────────────────────


class TestAuthInternalPathsRemainExcluded:
    """Auth inter-service and metrics paths must never reach the public entrypoint."""

    @pytest.mark.parametrize("conf_path", _ALL_DYNAMIC_CONFS)
    def test_user_private_excluded_from_auth_public_router(self, conf_path: Path):
        conf = _load(conf_path)
        rule = _router_rule(conf, "auth-public-router")
        assert "/user/private" in rule, (
            f"{conf_path.name}: /user/private must be excluded from the "
            "auth-public-router (inter-service API — internal only)"
        )

    @pytest.mark.parametrize("conf_path", _ALL_DYNAMIC_CONFS)
    def test_user_metrics_excluded_from_auth_public_router(self, conf_path: Path):
        conf = _load(conf_path)
        rule = _router_rule(conf, "auth-public-router")
        assert "/user/metrics" in rule, (
            f"{conf_path.name}: /user/metrics must be excluded from the "
            "auth-public-router (Prometheus scrape endpoint — internal only)"
        )
