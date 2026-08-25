"""5.5 consumer-side revocation degradation matrix — media-service-m8.

Verifies that ACCESS_REVOCATION_FAILURE_MODE=fail_closed (the secure default)
returns 503 when the introspection endpoint is unreachable, and that fail_open
accepts the token but logs the opt-out loudly (security.revocation_fail_open).

The behaviour lives in fastapi-m8>=3.0.0 via build_auth_deps; these tests
assert the end-to-end path is intact through the media-service-m8 consumer
configuration and that every env example documents the setting.

References: plan item 5.5 (consumer side), auth-sdk-m8 2.0.1 / fastapi-m8 3.0.0.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from auth_sdk_m8.schemas.user import UserModel
from fastapi_m8 import ConsumerServiceSettings, build_auth_deps

# ── Isolated settings (constructor kwargs only — no ambient state) ───────────

# Fixed test credentials. `_BASE` and `_mint_token` must agree on the signing
# key, so it has to be one constant: reading it from the environment let a real
# ACCESS_SECRET_KEY in the developer's shell reach a test that mints its own
# tokens, and a real key that fails the SDK's strength check took the whole
# suite down at import.
_ACCESS_SECRET: str = "TestSecret!Key4UnitTests_onlyXYZ0987"
_REFRESH_SECRET: str = "TestRefresh!Key4UnitTests_onlyABC1234"
_DB_PASSWORD: str = "TestDb!Pass1secure"


class _IsolatedConsumerSettings(ConsumerServiceSettings):
    """ConsumerServiceSettings that reads ONLY from constructor kwargs.

    ``env_file=None`` suppresses the dotenv source alone. It does **not**
    suppress the process environment, the ``<FIELD>_FILE`` secret-file source,
    the secrets dir or Vault — ``CommonSettings.settings_customise_sources``
    wires all of those in below init kwargs. So a shell carrying, say,
    ``ACCESS_REVOCATION_FAILURE_MODE`` (exported by hand, or sourced from one of
    the workspace stack env files) leaked straight into the matrix below and
    flipped this file red on a machine whose only difference from CI was an
    exported variable.

    Restricting the source list to ``init_settings`` makes the first line of
    this docstring true: every field these tests depend on comes from ``_BASE``
    and the per-test overrides, and from nowhere else.
    """

    model_config = SettingsConfigDict(env_file=None)

    @classmethod
    def settings_customise_sources(  # type: ignore[override]
        cls,
        _settings_cls: type[ConsumerServiceSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        """Constructor kwargs only — drop env, dotenv and secret-file sources."""
        return (init_settings,)


_BASE: dict = {
    "DOMAIN": "localhost",
    "ENVIRONMENT": "local",
    "API_PREFIX": "/media",
    "PROJECT_NAME": "M8TestMedia",
    "STACK_NAME": "m8-test",
    "BACKEND_HOST": "http://localhost:9000",
    "FRONTEND_HOST": "http://localhost:5173",
    "BACKEND_CORS_ORIGINS": "http://localhost",
    "AUTH_SERVICE_ROLE": "consumer",
    "TOKEN_MODE": "stateless",
    "AUTH_PREFIX": "/user",
    "ACCESS_SECRET_KEY": SecretStr(_ACCESS_SECRET),
    "REFRESH_SECRET_KEY": SecretStr(_REFRESH_SECRET),
    "ACCESS_TOKEN_ALGORITHM": "HS256",
    "REFRESH_TOKEN_ALGORITHM": "HS256",
    "TOKEN_STRICT_VALIDATION": False,
    "EVENT_SIGNING_ENABLED": False,
    "DB_HOST": "127.0.0.1",
    "DB_PORT": 5432,
    "DB_DATABASE": "test_db",
    "DB_USER": "test",
    "DB_PASSWORD": SecretStr(_DB_PASSWORD),
    "SERVICE_VERSION": "0.0.11",
    "CONTRACT_VERSION": "0.0",
    "CONTRACT_RANGE": ">=0.0.11 <0.1.0",
}

_INTROSPECTION_URL = "http://auth:8000/user/private/v1/jti-status"
_PRIVATE_SECRET = "supersecret"


def _stateful_settings(**overrides: object) -> _IsolatedConsumerSettings:
    """Return a stateful ConsumerServiceSettings with revocation config."""
    return _IsolatedConsumerSettings(
        **{
            **_BASE,
            "TOKEN_MODE": "stateful",
            "INTROSPECTION_URL": _INTROSPECTION_URL,
            "PRIVATE_API_SECRET": SecretStr(_PRIVATE_SECRET),
            **overrides,
        }
    )


def _mint_token() -> str:
    """Mint a minimal HS256 access token accepted by the test settings."""
    now = datetime.now(UTC)
    payload = {
        "sub": "550e8400-e29b-41d4-a716-446655440000",
        "type": "access",
        "email": "test@example.com",
        "role": "user",
        "jti": "jti-revocation-test-0001",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "is_active": True,
        "email_verified": False,
        "is_superuser": False,
    }
    return jwt.encode(payload, _ACCESS_SECRET, algorithm="HS256")


# ── Isolation lock ───────────────────────────────────────────────────────────


def test_isolated_settings_ignore_the_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exported override must not reach the degradation matrix (regression).

    Before ``_IsolatedConsumerSettings`` pinned its source list, exporting
    ``ACCESS_REVOCATION_FAILURE_MODE=fail_open`` was enough to turn
    ``test_fail_closed_is_the_consumer_default`` red — the test that exists
    precisely to prove the default is *not* fail_open.
    """
    monkeypatch.setenv("ACCESS_REVOCATION_FAILURE_MODE", "fail_open")
    monkeypatch.setenv("AUTH_PREFIX", "/hijacked")

    settings = _stateful_settings()

    assert settings.ACCESS_REVOCATION_FAILURE_MODE == "fail_closed"
    assert settings.AUTH_PREFIX == "/user"


# ── Env-example audit ─────────────────────────────────────────────────────────

_ROOT = Path(__file__).parent.parent
_STACKS = ["dev_media_m8", "hardened_media_m8", "worspace_dev_media_m8"]
_MEDIA_ENV_EXAMPLES = [
    _ROOT / "docker_compose" / s / "media.env.example" for s in _STACKS
]


@pytest.mark.parametrize("env_file", _MEDIA_ENV_EXAMPLES, ids=_STACKS)
def test_env_example_documents_access_revocation_failure_mode(env_file: Path) -> None:
    """Every media.env.example must document ACCESS_REVOCATION_FAILURE_MODE (5.5)."""
    assert "ACCESS_REVOCATION_FAILURE_MODE" in env_file.read_text(), (
        f"ACCESS_REVOCATION_FAILURE_MODE not documented in {env_file.relative_to(_ROOT)}"
    )


def test_hardened_env_sets_fail_closed_explicitly() -> None:
    """hardened media.env.example must set ACCESS_REVOCATION_FAILURE_MODE=fail_closed."""
    hardened = _ROOT / "docker_compose" / "hardened_media_m8" / "media.env.example"
    for line in hardened.read_text().splitlines():
        stripped = line.strip()
        if stripped == "ACCESS_REVOCATION_FAILURE_MODE=fail_closed":
            return
    pytest.fail(
        "ACCESS_REVOCATION_FAILURE_MODE=fail_closed not set as an active (uncommented) "
        "line in hardened_media_m8/media.env.example"
    )


def test_dev_env_has_fail_closed_commented_out() -> None:
    """dev media.env.example must have ACCESS_REVOCATION_FAILURE_MODE commented out."""
    dev = _ROOT / "docker_compose" / "dev_media_m8" / "media.env.example"
    for line in dev.read_text().splitlines():
        stripped = line.strip()
        if "ACCESS_REVOCATION_FAILURE_MODE" in stripped and not stripped.startswith(
            "#"
        ):
            pytest.fail(
                f"ACCESS_REVOCATION_FAILURE_MODE must be commented out in "
                f"dev_media_m8/media.env.example (opt-in; default shown in comment): {line!r}"
            )


# ── End-to-end degradation matrix ─────────────────────────────────────────────

pytestmark = pytest.mark.anyio


async def test_fail_closed_introspection_down_returns_503() -> None:
    """fail_closed + unreachable introspection → 503 from get_current_user."""
    auth = build_auth_deps(
        _stateful_settings(ACCESS_REVOCATION_FAILURE_MODE="fail_closed")
    )
    assert auth.revocation_client is not None
    setattr(
        auth.revocation_client._client,
        "post",
        AsyncMock(side_effect=httpx.ConnectError("auth service down")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await auth.get_current_user(_mint_token())
    assert exc_info.value.status_code == 503
    await auth.close()


async def test_fail_closed_is_the_consumer_default() -> None:
    """Omitting ACCESS_REVOCATION_FAILURE_MODE → fail_closed → 503 on outage."""
    auth = build_auth_deps(_stateful_settings())  # no explicit mode — default applies
    assert auth.revocation_client is not None
    setattr(
        auth.revocation_client._client,
        "post",
        AsyncMock(side_effect=httpx.ConnectError("auth service down")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await auth.get_current_user(_mint_token())
    assert exc_info.value.status_code == 503
    await auth.close()


async def test_fail_open_introspection_down_accepts_token() -> None:
    """fail_open opt-out + unreachable introspection → token accepted (not 503)."""
    auth = build_auth_deps(
        _stateful_settings(ACCESS_REVOCATION_FAILURE_MODE="fail_open")
    )
    assert auth.revocation_client is not None
    setattr(
        auth.revocation_client._client,
        "post",
        AsyncMock(side_effect=httpx.ConnectError("auth service down")),
    )

    user = await auth.get_current_user(_mint_token())
    assert isinstance(user, UserModel)
    await auth.close()


async def test_fail_open_logs_security_revocation_fail_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """fail_open opt-out must log security.revocation_fail_open at WARNING or higher."""
    auth = build_auth_deps(
        _stateful_settings(ACCESS_REVOCATION_FAILURE_MODE="fail_open")
    )
    assert auth.revocation_client is not None
    setattr(
        auth.revocation_client._client,
        "post",
        AsyncMock(side_effect=httpx.ConnectError("auth service down")),
    )

    with caplog.at_level(logging.WARNING):
        await auth.get_current_user(_mint_token())

    assert "security.revocation_fail_open" in caplog.text
    await auth.close()
