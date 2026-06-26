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


# ── Dependency floor ──────────────────────────────────────────────────────────


def test_fastapi_m8_floor_is_3_1():
    """requirements_base.txt must pin fastapi-m8 floor at >=3.1.0 for 9.1 auth."""
    content = _REQS_BASE.read_text()
    for line in content.splitlines():
        if line.startswith("fastapi-m8"):
            assert re.search(r">=\s*3\.[1-9]", line), (
                f"fastapi-m8 floor must be >=3.1.0 for per-consumer auth: {line!r}"
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
