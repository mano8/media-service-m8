"""Unit tests for the outbound-webhook SSRF guard (core.ssrf).

Covers the static (create-time) and resolved (send-time) passes, the
always-blocked ranges, the production-only private-range + HTTPS gates, the
exact-hostname allowlist, and the boolean guard built for the delivery worker.
"""

import ipaddress

import pytest

from media_service.core.ssrf import (
    WebhookPolicy,
    WebhookTargetError,
    build_url_guard,
    check_webhook_url,
)

LOCAL = WebhookPolicy(production=False, allowed_internal_hosts=frozenset())
PROD = WebhookPolicy(production=True, allowed_internal_hosts=frozenset())


def _resolver(*ips: str):
    """A fake resolver returning fixed IPs for any host (no real DNS)."""
    return lambda _host: list(ips)


# ── WebhookPolicy.from_settings ──────────────────────────────────────────────


class _Settings:
    def __init__(self, environment="local", strict=False, hosts=None):
        self.ENVIRONMENT = environment
        self.STRICT_PRODUCTION_MODE = strict
        self.MEDIA_WEBHOOK_ALLOWED_INTERNAL_HOSTS = hosts or []


def test_from_settings_local_is_not_production():
    policy = WebhookPolicy.from_settings(_Settings())
    assert policy.production is False
    assert policy.allowed_internal_hosts == frozenset()


@pytest.mark.parametrize(
    "settings",
    [_Settings(environment="production"), _Settings(strict=True)],
)
def test_from_settings_production_or_strict(settings):
    assert WebhookPolicy.from_settings(settings).production is True


def test_from_settings_normalises_and_drops_blank_hosts():
    policy = WebhookPolicy.from_settings(
        _Settings(hosts=["  Media_Worker ", "", "   "])
    )
    assert policy.allowed_internal_hosts == frozenset({"media_worker"})


def test_from_settings_defaults_when_attrs_absent():
    policy = WebhookPolicy.from_settings(object())
    assert policy.production is False
    assert policy.allowed_internal_hosts == frozenset()


# ── scheme + host parsing ────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["ftp://h/x", "gopher://h", "file:///etc/passwd"])
def test_rejects_non_http_scheme(url):
    with pytest.raises(WebhookTargetError, match="scheme"):
        check_webhook_url(url, policy=LOCAL)


def test_rejects_missing_host():
    with pytest.raises(WebhookTargetError, match="no host"):
        check_webhook_url("http:///just/a/path", policy=LOCAL)


# ── always-blocked ranges (every environment) ────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/h",
        "http://[::1]/h",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://0.0.0.0/h",
        "http://224.0.0.1/h",  # multicast
    ],
)
def test_blocks_dangerous_literals_even_in_local(url):
    with pytest.raises(WebhookTargetError, match="blocked address"):
        check_webhook_url(url, policy=LOCAL)


def test_localhost_name_resolving_to_loopback_blocked_at_send_time():
    with pytest.raises(WebhookTargetError, match="blocked address"):
        check_webhook_url(
            "http://localhost/h", policy=LOCAL, resolver=_resolver("127.0.0.1")
        )


# ── private ranges: allowed local, blocked production ────────────────────────


@pytest.mark.parametrize("ip", ["10.0.0.5", "172.16.3.4", "192.168.1.10"])
def test_private_literal_allowed_in_local(ip):
    check_webhook_url(f"http://{ip}/h", policy=LOCAL)  # no raise


@pytest.mark.parametrize("ip", ["10.0.0.5", "172.16.3.4", "192.168.1.10"])
def test_private_literal_blocked_in_production(ip):
    with pytest.raises(WebhookTargetError, match="private address"):
        check_webhook_url(f"https://{ip}/h", policy=PROD)


def test_internal_service_name_blocked_in_production_at_send_time():
    with pytest.raises(WebhookTargetError, match="private address"):
        check_webhook_url(
            "https://media_worker/h", policy=PROD, resolver=_resolver("172.18.0.4")
        )


def test_internal_service_name_allowed_in_local_at_send_time():
    check_webhook_url(
        "http://media_worker/h", policy=LOCAL, resolver=_resolver("172.18.0.4")
    )  # no raise


def test_public_target_allowed_in_production():
    check_webhook_url(
        "https://hook.example.com/h", policy=PROD, resolver=_resolver("93.184.216.34")
    )  # no raise


# ── HTTPS gate (production) ──────────────────────────────────────────────────


def test_http_rejected_in_production():
    with pytest.raises(WebhookTargetError, match="https"):
        check_webhook_url("http://hook.example.com/h", policy=PROD)


def test_http_allowed_in_local():
    check_webhook_url(
        "http://hook.example.com/h", policy=LOCAL, resolver=_resolver("93.184.216.34")
    )  # no raise


# ── name resolution failures ─────────────────────────────────────────────────


def test_unresolvable_host_is_rejected():
    def boom(_host):
        raise OSError("nxdomain")

    with pytest.raises(WebhookTargetError, match="could not resolve"):
        check_webhook_url("https://nope.invalid/h", policy=PROD, resolver=boom)


def test_empty_resolution_is_rejected():
    with pytest.raises(WebhookTargetError, match="could not resolve"):
        check_webhook_url("https://nope.invalid/h", policy=PROD, resolver=_resolver())


def test_any_blocked_ip_among_many_rejects_in_production():
    with pytest.raises(WebhookTargetError, match="private address"):
        check_webhook_url(
            "https://rebind.example.com/h",
            policy=PROD,
            resolver=_resolver("93.184.216.34", "10.1.2.3"),
        )


def test_ipv6_scope_id_is_stripped_before_classification():
    with pytest.raises(WebhookTargetError, match="blocked address"):
        check_webhook_url(
            "https://h/x", policy=PROD, resolver=_resolver("fe80::1%eth0")
        )


# ── allowlist override ───────────────────────────────────────────────────────


def test_allowlisted_host_bypasses_all_gates():
    policy = WebhookPolicy(
        production=True, allowed_internal_hosts=frozenset({"media_worker"})
    )
    # http + would resolve to a private IP, yet allowed because allowlisted.
    check_webhook_url("http://media_worker/h", policy=policy)  # no raise


def test_allowlist_match_is_case_insensitive():
    policy = WebhookPolicy(
        production=True, allowed_internal_hosts=frozenset({"media_worker"})
    )
    check_webhook_url("http://Media_Worker/h", policy=policy)  # no raise


# ── name deferred at create time (no resolver) ───────────────────────────────


def test_name_is_deferred_when_no_resolver():
    # A name is not resolved in the static pass — even an internal-looking one.
    check_webhook_url("https://media_worker/h", policy=PROD)  # no raise


# ── build_url_guard (send-time boolean) ──────────────────────────────────────


def test_guard_returns_true_for_allowed_target():
    guard = build_url_guard(LOCAL, resolver=_resolver("93.184.216.34"))
    assert guard("https://hook.example.com/h") is True


def test_guard_returns_false_and_logs_for_blocked_target(caplog):
    guard = build_url_guard(PROD, resolver=_resolver("10.0.0.9"))
    with caplog.at_level("WARNING"):
        assert guard("https://internal.example.com/h") is False
    assert "SSRF guard" in caplog.text


def test_guard_uses_default_resolver_when_unset(monkeypatch):
    import media_service.core.ssrf as ssrf

    monkeypatch.setattr(ssrf, "_default_resolver", lambda _host: ["10.0.0.1"])
    guard = build_url_guard(PROD)
    assert guard("https://internal.example.com/h") is False


def test_default_resolver_returns_ip_strings():
    import media_service.core.ssrf as ssrf

    addrs = ssrf._default_resolver("127.0.0.1")
    assert addrs
    assert all(ipaddress.ip_address(a.split("%")[0]) for a in addrs)
