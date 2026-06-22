"""Static Redis per-service ACL policy tests (plan item 6.x.1).

These tests parse the compose YAML directly — no running Redis required.

Every media compose stack ships two Redis services and each must run a scoped
ACL bootstrap instead of the old open ``appuser ~* +@all``:

- ``redis_cache`` — the bundled **auth** service's Redis. The auth app
  authenticates as a scoped ``auth`` user restricted to its own key prefixes.
- ``media_redis_cache`` — the **media**-owned Redis (rate limits + the ``arq``
  queue). The media app authenticates as a scoped ``media`` user restricted to
  the ``media:*`` namespace and the ``arq:*`` queue keys.

In both, the ``default`` user is stripped of all data/admin access (``-@all``)
and keeps only connection commands so the healthcheck ``PING`` still works, and
the ``*_REDIS_USER`` env example is wired to the scoped username.

A source-linked guard re-derives the media key namespace (and the ARQ usage)
from the service code and fails if either drifts from the ACL patterns.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).parent.parent
_COMPOSE_DIR = _REPO_ROOT / "docker_compose"

# Every committed media example stack ships the two redis services with an ACL
# bootstrap. (The local-only `worspace_dev_media_m8` stack is untracked, so it
# is not asserted here — only stacks that exist on a fresh checkout.)
_STACKS = ["dev_media_m8", "hardened_media_m8"]

# (compose service, scoped ACL username, env file, env var)
_AUTH = ("redis_cache", "auth", "auth.env.example", "REDIS_USER")
_MEDIA = ("media_redis_cache", "media", "media.env.example", "MEDIA_REDIS_USER")
_SERVICES = [_AUTH, _MEDIA]

# Key prefixes the bundled auth service + auth-sdk write (mirrors fa-auth-m8).
_AUTH_KEY_PREFIXES = [
    "oauth_session:",
    "auth_code:",
    "login:attempts:",
    "refresh:attempts:",
    "exchange:attempts:",
    "rt:",
    "jwt:blacklist:",
    "rate:api:",
    "api_key:luat",
]
# Key prefixes the media app writes: everything under its namespace plus arq.
_MEDIA_KEY_PREFIXES = ["media:", "arq:"]


def _load(stack: str) -> dict:
    return yaml.safe_load((_COMPOSE_DIR / stack / "docker-compose.yml").read_text())


def _redis_command(stack: str, service: str) -> str:
    command = _load(stack)["services"][service]["command"]
    assert isinstance(command, list), f"{stack}/{service}: command must be a list"
    return "\n".join(str(part) for part in command)


def _setuser_line(script: str, username: str) -> str:
    matches = [
        line
        for line in script.splitlines()
        if re.search(rf"\bACL SETUSER {re.escape(username)}\b", line)
    ]
    assert len(matches) == 1, (
        f"expected exactly one SETUSER for {username!r}, got {matches}"
    )
    return matches[0]


def _acl_key_patterns(setuser_line: str) -> list[str]:
    return re.findall(r'~([^"\s]+)', setuser_line)


# ── the open appuser ACL is gone in every redis service ──────────────────────


@pytest.mark.parametrize("stack", _STACKS)
@pytest.mark.parametrize("service,_user,_env,_var", _SERVICES)
def test_no_open_appuser_acl(
    stack: str, service: str, _user: str, _env: str, _var: str
) -> None:
    script = _redis_command(stack, service)
    assert "appuser" not in script, (
        f"{stack}/{service}: the open 'appuser' ACL must be replaced by a scoped user"
    )
    assert "+@all" not in script, f"{stack}/{service}: no ACL user may be granted +@all"


# ── scoped, non-wildcard users ───────────────────────────────────────────────


@pytest.mark.parametrize("stack", _STACKS)
@pytest.mark.parametrize("service,user,_env,_var", _SERVICES)
def test_user_is_scoped_not_wildcard(
    stack: str, service: str, user: str, _env: str, _var: str
) -> None:
    line = _setuser_line(_redis_command(stack, service), user)
    patterns = _acl_key_patterns(line)
    assert patterns, f"{stack}/{service}: {user} must declare explicit ~key patterns"
    assert "*" not in patterns, (
        f"{stack}/{service}: {user} must not have the '~*' wildcard keyspace"
    )
    assert "+@all" not in line, f"{stack}/{service}: {user} must not be granted +@all"


@pytest.mark.parametrize("stack", _STACKS)
@pytest.mark.parametrize("service,user,_env,_var", _SERVICES)
def test_user_grants_only_needed_categories(
    stack: str, service: str, user: str, _env: str, _var: str
) -> None:
    line = _setuser_line(_redis_command(stack, service), user)
    for grant in (
        "+@read",
        "+@write",
        "+@transaction",
        "+@connection",
        "+eval",
        "-@dangerous",
    ):
        assert grant in line, (
            f"{stack}/{service}: {user} missing required grant {grant!r}"
        )
    for forbidden in ("+@admin", "+@dangerous", "+@scripting", "+@pubsub"):
        assert forbidden not in line, (
            f"{stack}/{service}: {user} must not grant {forbidden!r}"
        )


@pytest.mark.parametrize("stack", _STACKS)
def test_auth_acl_covers_every_source_key_prefix(stack: str) -> None:
    patterns = _acl_key_patterns(
        _setuser_line(_redis_command(stack, "redis_cache"), "auth")
    )
    globs = [p[:-1] if p.endswith("*") else p for p in patterns]
    for prefix in _AUTH_KEY_PREFIXES:
        assert any(prefix.startswith(g) for g in globs), (
            f"{stack}: auth key prefix {prefix!r} not covered by ACL {patterns}"
        )


@pytest.mark.parametrize("stack", _STACKS)
def test_media_acl_covers_every_source_key_prefix(stack: str) -> None:
    patterns = _acl_key_patterns(
        _setuser_line(_redis_command(stack, "media_redis_cache"), "media")
    )
    globs = [p[:-1] if p.endswith("*") else p for p in patterns]
    for prefix in _MEDIA_KEY_PREFIXES:
        assert any(prefix.startswith(g) for g in globs), (
            f"{stack}: media key prefix {prefix!r} not covered by ACL {patterns}"
        )


# ── default user locked down on both redis services ──────────────────────────


@pytest.mark.parametrize("stack", _STACKS)
@pytest.mark.parametrize("service,_user,_env,_var", _SERVICES)
def test_default_user_is_restricted(
    stack: str, service: str, _user: str, _env: str, _var: str
) -> None:
    line = _setuser_line(_redis_command(stack, service), "default")
    assert "-@all" in line, (
        f"{stack}/{service}: default user must be stripped with -@all"
    )
    assert "+@all" not in line, (
        f"{stack}/{service}: default user must not be granted +@all"
    )
    assert "*" not in _acl_key_patterns(line), (
        f"{stack}/{service}: default user must not retain '~*' (use resetkeys)"
    )


# ── env examples wire the apps to the scoped users ───────────────────────────


@pytest.mark.parametrize("stack", _STACKS)
@pytest.mark.parametrize("service,user,env_file,var", _SERVICES)
def test_redis_user_env_matches_scoped_acl(
    stack: str, service: str, user: str, env_file: str, var: str
) -> None:
    text = (_COMPOSE_DIR / stack / env_file).read_text(encoding="utf-8")
    expected = f"{var}={user}"
    assert f"{expected}\n" in text or text.endswith(expected), (
        f"{stack}/{env_file}: must set {expected} to authenticate as the scoped ACL user"
    )
    assert f"{var}=appuser" not in text, f"{stack}/{env_file}: stale {var}=appuser"


# ── the code-linked guard stays honest ───────────────────────────────────────


def test_media_namespace_default_matches_acl() -> None:
    """The ``media:*`` ACL glob must match the service's default namespace.

    Re-derives ``MEDIA_REDIS_NAMESPACE``'s default from core/config.py so the
    compose ACL cannot silently diverge from the namespace the app writes to.
    """
    config_src = (_REPO_ROOT / "media_service" / "core" / "config.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'MEDIA_REDIS_NAMESPACE:\s*str\s*=\s*"([^"]+)"', config_src)
    assert match, "could not find MEDIA_REDIS_NAMESPACE default in core/config.py"
    namespace = match.group(1).strip(":")
    assert f"{namespace}:" in _MEDIA_KEY_PREFIXES, (
        f"namespace default {namespace!r} not represented in _MEDIA_KEY_PREFIXES — "
        "update the audited list and the compose media ACL (~namespace:*)"
    )


def test_media_uses_arq_queue() -> None:
    """The media service enqueues ARQ jobs, so the ACL must cover ``arq:*``.

    Guards against dropping the ``arq:`` prefix from the audited list if the
    service ever stopped using ARQ (it must not, while core/arq.py exists).
    """
    arq_src = (_REPO_ROOT / "media_service" / "core" / "arq.py").read_text(
        encoding="utf-8"
    )
    assert "create_pool" in arq_src or "ArqRedis" in arq_src, (
        "core/arq.py no longer uses ARQ — revisit the ~arq:* media ACL pattern"
    )
    assert "arq:" in _MEDIA_KEY_PREFIXES


@pytest.mark.parametrize("stack", _STACKS)
def test_media_acl_grants_info_after_dangerous_strip(stack: str) -> None:
    """ARQ issues ``INFO server`` on startup to read the Redis version.

    ``INFO`` lives in the ``@dangerous`` category, which the media user strips,
    so ``+info`` must be re-granted *after* ``-@dangerous`` — Redis ACL rules
    apply left-to-right, so the order is load-bearing. Without it the worker
    dies with ``NoPermissionError`` running 'info'.
    """
    line = _setuser_line(_redis_command(stack, "media_redis_cache"), "media")
    info = re.search(r"\+info\b", line)
    dangerous = re.search(r"-@dangerous\b", line)
    assert info, f"{stack}: media user must grant +info for ARQ's INFO command"
    assert dangerous, f"{stack}: media user must still strip -@dangerous"
    assert info.start() > dangerous.start(), (
        f"{stack}: +info must come after -@dangerous or it stays stripped "
        "(Redis ACL rules apply left-to-right)"
    )
