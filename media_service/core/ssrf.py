"""SSRF guard for outbound webhook delivery.

Webhook subscriber URLs are operator-supplied (the admin subscription API) and
POSTed to from the service-owned worker. Without a guard a subscriber could aim
delivery at the cloud-metadata endpoint, a loopback admin port, or an internal
service — turning the transactional outbox into a server-side request-forgery
primitive. This module validates a target URL by **resolving its host at send
time and inspecting every resolved IP**, so a name that resolves to an internal
address (DNS-rebinding included) is caught.

Posture (honours the M8 home-lab rule — degrade gracefully, fail closed only in
production):

- **Always blocked, every environment:** loopback, link-local (including the
  ``169.254.169.254`` cloud-metadata address), multicast, reserved, and the
  unspecified address. No real home-lab subscriber lives at these and they are
  the classic SSRF sinks.
- **Blocked only under production/strict:** RFC1918 / ULA / CGNAT private ranges,
  so a dev stack can still deliver to a Docker-network subscriber
  (``http://media_worker:8000``) while production refuses internal targets.
- **HTTPS** is required under production/strict; ``http://`` stays allowed in
  local/dev.
- ``MEDIA_WEBHOOK_ALLOWED_INTERNAL_HOSTS`` is an exact-hostname allowlist that
  exempts a trusted internal subscriber from the private-network and HTTPS gates.

``check_webhook_url`` is used at two points: a static pass at subscription create
time (no ``resolver`` — literal IPs, scheme, and the HTTPS rule are checked while
name resolution is deferred) and the authoritative resolved-IP pass at send time
(a ``resolver`` is supplied) via :func:`build_url_guard`.
"""

import ipaddress
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

_logger = logging.getLogger(__name__)

#: Resolve a hostname to the list of IP strings it points at.
Resolver = Callable[[str], list[str]]

#: Predicate used by the delivery worker: True iff ``url`` is a safe target.
WebhookUrlGuard = Callable[[str], bool]

_IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class WebhookTargetError(ValueError):
    """A webhook URL was rejected by the SSRF guard (operator-actionable)."""


@dataclass(frozen=True)
class WebhookPolicy:
    """Resolved SSRF posture for the current environment."""

    production: bool
    allowed_internal_hosts: frozenset[str]

    @classmethod
    def from_settings(cls, settings: object) -> "WebhookPolicy":
        """Derive the policy from a media-service ``Settings`` instance."""
        environment = getattr(settings, "ENVIRONMENT", "local")
        strict = bool(getattr(settings, "STRICT_PRODUCTION_MODE", False))
        hosts = getattr(settings, "MEDIA_WEBHOOK_ALLOWED_INTERNAL_HOSTS", [])
        return cls(
            production=environment == "production" or strict,
            allowed_internal_hosts=frozenset(
                host.strip().lower() for host in hosts if host.strip()
            ),
        )


def _default_resolver(host: str) -> list[str]:
    """Resolve ``host`` to every IP it points at (A/AAAA), via the OS resolver."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def _as_ip(host: str) -> _IpAddress | None:
    """Parse ``host`` as an IP literal, or return None when it is a name."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_always_blocked(ip: _IpAddress) -> bool:
    """Ranges that are never a legitimate webhook target, in any environment."""
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _reject_if_blocked(ip: _IpAddress, host: str, *, production: bool) -> None:
    """Raise when ``ip`` is a forbidden target for the current posture."""
    if _is_always_blocked(ip):
        raise WebhookTargetError(
            f"webhook host {host!r} resolves to a blocked address ({ip})"
        )
    if production and ip.is_private:
        raise WebhookTargetError(
            f"webhook host {host!r} resolves to a private address ({ip}); "
            "add it to MEDIA_WEBHOOK_ALLOWED_INTERNAL_HOSTS to allow"
        )


def _split_target(url: str) -> tuple[str, str]:
    """Return ``(host, scheme)`` for ``url``; raise on a missing/bad component."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise WebhookTargetError("webhook URL scheme must be http or https")
    if not parts.hostname:
        raise WebhookTargetError("webhook URL has no host")
    return parts.hostname, scheme


def _resolve_ips(host: str, resolver: Resolver) -> list[str]:
    """Resolve ``host``; an empty or failing resolution is itself a rejection."""
    try:
        addrs = resolver(host)
    except OSError as exc:
        raise WebhookTargetError(f"could not resolve webhook host {host!r}") from exc
    if not addrs:
        raise WebhookTargetError(f"could not resolve webhook host {host!r}")
    return addrs


def check_webhook_url(
    url: str, *, policy: WebhookPolicy, resolver: Resolver | None = None
) -> None:
    """Validate ``url`` as a webhook target; raise :class:`WebhookTargetError`.

    With ``resolver=None`` (subscription create time) only the scheme, the
    production HTTPS rule, and any literal-IP host are checked — name resolution
    is deferred to send time. With a ``resolver`` (send time) the host is
    resolved and **every** returned IP is validated.
    """
    host, scheme = _split_target(url)
    if host.lower() in policy.allowed_internal_hosts:
        return
    if policy.production and scheme != "https":
        raise WebhookTargetError("webhook URL must use https under production")
    literal = _as_ip(host)
    if literal is not None:
        _reject_if_blocked(literal, host, production=policy.production)
        return
    if resolver is None:
        return
    for raw in _resolve_ips(host, resolver):
        ip = ipaddress.ip_address(raw.split("%")[0])
        _reject_if_blocked(ip, host, production=policy.production)


def build_url_guard(
    policy: WebhookPolicy, resolver: Resolver | None = None
) -> WebhookUrlGuard:
    """Build the send-time guard predicate the delivery worker threads in.

    The returned callable resolves the host (default: OS resolver) and returns
    False — logging the reason — when the target is blocked, so a poisoned
    subscriber is never POSTed to.
    """

    def guard(url: str) -> bool:
        try:
            check_webhook_url(
                url, policy=policy, resolver=resolver or _default_resolver
            )
        except WebhookTargetError as exc:
            _logger.warning("webhook target blocked by SSRF guard: %s", exc)
            return False
        return True

    return guard
