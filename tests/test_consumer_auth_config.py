"""Tests for 9.1 per-consumer internal-auth adoption (media-service-m8 consumer side).

Verifies:
- fastapi-m8 floor is >=3.1.0 in requirements_base.txt (enables build_internal_auth).
- INTERNAL_CLIENT_ID is documented in every media.env.example (per-consumer mode).
- PRIVATE_API_CONSUMERS is documented in every auth.env.example (issuer-side config).
- build_internal_auth produces legacy/bootstrap headers based on INTERNAL_CLIENT_ID.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from auth_sdk_m8.security.consumer_auth import (
    INTERNAL_CLIENT_HEADER,
    INTERNAL_TOKEN_HEADER,
)
from fastapi_m8._internal_auth import build_internal_auth

_ROOT = Path(__file__).parent.parent
_REQS_BASE = _ROOT / "media_service" / "requirements_base.txt"
_STACKS = ["dev_media_m8", "hardened_media_m8", "worspace_dev_media_m8"]
_MEDIA_ENV_EXAMPLES = [
    _ROOT / "docker_compose" / s / "media.env.example" for s in _STACKS
]
_AUTH_ENV_EXAMPLES = [
    _ROOT / "docker_compose" / s / "auth.env.example" for s in _STACKS
]
_HARDENED_DIR = _ROOT / "docker_compose" / "hardened_media_m8"
_MEDIA_PROD_EXAMPLE = _HARDENED_DIR / "media.env.production.example"
_AUTH_PROD_EXAMPLE = _HARDENED_DIR / "auth.env.production.example"


# ── Dependency floor ──────────────────────────────────────────────────────────


def test_fastapi_m8_floor_is_4_2_2():
    """requirements_base.txt must pin fastapi-m8 floor at >=4.2.2 (A7).

    Raised from the 3.1.0-era floor this test used to assert: 4.2.2 is the
    version constraints.txt/constraints-all.txt actually resolve against
    (auth-sdk-m8>=3.1.2), and the reader/writer/admin/superuser tier guards
    this repo depends on (Depends(auth.get_current_active_superuser) in
    app/routes/admin.py) only exist from the 4.x line.
    """
    content = _REQS_BASE.read_text()
    for line in content.splitlines():
        if line.startswith("fastapi-m8"):
            assert re.search(r">=\s*4\.2\.2", line), (
                f"fastapi-m8 floor must be >=4.2.2 for the tier-guard API: {line!r}"
            )
            return
    pytest.fail("fastapi-m8 not found in requirements_base.txt")


# ── Env example audits ────────────────────────────────────────────────────────


@pytest.mark.parametrize("env_file", _MEDIA_ENV_EXAMPLES, ids=_STACKS)
def test_media_env_example_documents_internal_client_id(env_file: Path) -> None:
    """Every media.env.example must document INTERNAL_CLIENT_ID (9.1 consumer side)."""
    assert "INTERNAL_CLIENT_ID" in env_file.read_text(), (
        f"INTERNAL_CLIENT_ID not found in {env_file.relative_to(_ROOT)}"
    )


@pytest.mark.parametrize("env_file", _MEDIA_ENV_EXAMPLES, ids=_STACKS)
def test_media_env_example_service_token_exchange_commented_out(
    env_file: Path,
) -> None:
    """SERVICE_TOKEN_EXCHANGE_ENABLED must be commented out (opt-in, not default-on)."""
    content = env_file.read_text()
    assert "SERVICE_TOKEN_EXCHANGE_ENABLED" in content, (
        f"SERVICE_TOKEN_EXCHANGE_ENABLED not documented in {env_file.relative_to(_ROOT)}"
    )
    for line in content.splitlines():
        stripped = line.strip()
        if "SERVICE_TOKEN_EXCHANGE_ENABLED" in stripped and not stripped.startswith(
            "#"
        ):
            pytest.fail(
                f"SERVICE_TOKEN_EXCHANGE_ENABLED must be commented out "
                f"(opt-in only) in {env_file.relative_to(_ROOT)}: {line!r}"
            )


@pytest.mark.parametrize("env_file", _AUTH_ENV_EXAMPLES, ids=_STACKS)
def test_auth_env_example_documents_private_api_consumers(env_file: Path) -> None:
    """Every auth.env.example must document PRIVATE_API_CONSUMERS (9.1 issuer side)."""
    assert "PRIVATE_API_CONSUMERS" in env_file.read_text(), (
        f"PRIVATE_API_CONSUMERS not found in {env_file.relative_to(_ROOT)}"
    )


@pytest.mark.parametrize("env_file", _AUTH_ENV_EXAMPLES, ids=_STACKS)
def test_auth_env_example_private_api_consumers_is_active(
    env_file: Path,
) -> None:
    """PRIVATE_API_CONSUMERS must be active (the bundled issuer is fa-auth-m8
    >= 1.0.0 — per-consumer, no legacy single-secret fallback, so the registry
    is required for the media-service consumer to authenticate)."""
    active = [
        line
        for line in env_file.read_text().splitlines()
        if "PRIVATE_API_CONSUMERS=" in line and not line.strip().startswith("#")
    ]
    assert active, (
        f"PRIVATE_API_CONSUMERS must be active (uncommented) in "
        f"{env_file.relative_to(_ROOT)} — the 1.0.0 issuer fails closed without it"
    )
    assert "media-service" in active[0], (
        f"PRIVATE_API_CONSUMERS must register the 'media-service' consumer id in "
        f"{env_file.relative_to(_ROOT)}: {active[0]!r}"
    )


# ── build_internal_auth behaviour ────────────────────────────────────────────


def _mock_settings(
    *,
    client_id: str | None = None,
    exchange: bool = False,
    secret: str = "bootstrap-secret-Xyz1!",
) -> MagicMock:
    s = MagicMock()
    s.INTERNAL_CLIENT_ID = client_id
    s.SERVICE_TOKEN_EXCHANGE_ENABLED = exchange
    s.SERVICE_TOKEN_SCOPES = None
    s.SERVICE_TOKEN_REFRESH_LEEWAY_SECONDS = 30
    pap = MagicMock()
    pap.get_secret_value.return_value = secret
    s.PRIVATE_API_SECRET = pap
    s.INTROSPECTION_URL = "http://auth:8000/user/private/v1/jti-status"
    return s


def test_legacy_mode_sends_x_internal_token_only() -> None:
    """INTERNAL_CLIENT_ID unset → X-Internal-Token only (legacy single-secret mode)."""
    provider = build_internal_auth(_mock_settings(client_id=None))
    headers = asyncio.run(provider.headers())
    assert INTERNAL_TOKEN_HEADER in headers
    assert INTERNAL_CLIENT_HEADER not in headers


def test_legacy_mode_forwards_private_api_secret_value() -> None:
    """Legacy mode must forward the raw PRIVATE_API_SECRET as X-Internal-Token."""
    provider = build_internal_auth(_mock_settings(client_id=None, secret="my-secret"))
    assert asyncio.run(provider.headers())[INTERNAL_TOKEN_HEADER] == "my-secret"


def test_bootstrap_mode_emits_client_id_and_token() -> None:
    """INTERNAL_CLIENT_ID set → X-Internal-Client + X-Internal-Token (bootstrap)."""
    provider = build_internal_auth(_mock_settings(client_id="media-service"))
    headers = asyncio.run(provider.headers())
    assert headers.get(INTERNAL_CLIENT_HEADER) == "media-service"
    assert INTERNAL_TOKEN_HEADER in headers


def test_bootstrap_mode_client_id_is_correct() -> None:
    """X-Internal-Client header carries the configured INTERNAL_CLIENT_ID verbatim."""
    provider = build_internal_auth(_mock_settings(client_id="media-service"))
    assert asyncio.run(provider.headers())[INTERNAL_CLIENT_HEADER] == "media-service"


# ── Production overlay — 11.2b ────────────────────────────────────────────────


def test_media_production_example_has_internal_client_id() -> None:
    """media.env.production.example must have INTERNAL_CLIENT_ID=media-service active."""
    content = _MEDIA_PROD_EXAMPLE.read_text()
    active = [
        line
        for line in content.splitlines()
        if "INTERNAL_CLIENT_ID=" in line and not line.strip().startswith("#")
    ]
    assert active, "INTERNAL_CLIENT_ID must be active in media.env.production.example"
    assert "media-service" in active[0], (
        f"INTERNAL_CLIENT_ID must be 'media-service' in production overlay: {active[0]!r}"
    )


def test_media_production_example_no_introspection_without_client_id() -> None:
    """INTROSPECTION_URL set without INTERNAL_CLIENT_ID active is disallowed by fastapi-m8>=3.3."""
    content = _MEDIA_PROD_EXAMPLE.read_text()
    has_introspection = any(
        "INTROSPECTION_URL=" in line and not line.strip().startswith("#")
        for line in content.splitlines()
    )
    has_client_id = any(
        "INTERNAL_CLIENT_ID=" in line and not line.strip().startswith("#")
        for line in content.splitlines()
    )
    if has_introspection:
        assert has_client_id, (
            "media.env.production.example sets INTROSPECTION_URL but no INTERNAL_CLIENT_ID — "
            "fastapi-m8>=3.3 raises at settings construction (11.2b)"
        )


def test_auth_production_example_requires_consumers_registry() -> None:
    """auth.env.production.example must have PRIVATE_API_CONSUMERS_FILE or PRIVATE_API_CONSUMERS active.

    11.2a made an empty consumer registry a fatal startup error in production/strict mode;
    the example must show a concrete, required credential source.
    """
    content = _AUTH_PROD_EXAMPLE.read_text()
    active_registry = [
        line
        for line in content.splitlines()
        if ("PRIVATE_API_CONSUMERS_FILE=" in line or "PRIVATE_API_CONSUMERS=" in line)
        and not line.strip().startswith("#")
    ]
    assert active_registry, (
        "auth.env.production.example must have PRIVATE_API_CONSUMERS_FILE or "
        "PRIVATE_API_CONSUMERS active — required in production/strict (11.2a)"
    )


def test_auth_production_example_consumers_comment_says_required() -> None:
    """The consumers section comment must say 'required', not 'recommended'."""
    content = _AUTH_PROD_EXAMPLE.read_text()
    assert "required" in content.lower(), (
        "auth.env.production.example consumers section must document that "
        "PRIVATE_API_CONSUMERS is REQUIRED in production (11.2a)"
    )
    assert "recommended" not in content.lower(), (
        "auth.env.production.example must not say 'recommended' for PRIVATE_API_CONSUMERS "
        "— it is required since 11.2a"
    )
