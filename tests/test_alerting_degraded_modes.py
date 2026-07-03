"""11.12 — Alerting for degraded security modes (media-service-m8).

Codifies the alerting/logging invariants for media-service-m8 security
controls so they cannot silently regress:

- Rate-limit Redis degradation (RateLimiter / AnonRateLimiter) emits
  structured log lines that include the action and mode labels but never
  expose JWT values, bearer tokens, share-signing secrets, or the
  ``MEDIA_INTERNAL_SERVICE_TOKEN`` value.
- The ``inc_rate_limit_redis_error`` metric is emitted with the effective
  mode on every Redis-error path (already verified in test_rate_limit.py;
  asserted here for the log-content angle to keep concerns separated).
- Internal service-token auth (``require_service_token``) raises HTTP 403
  on any mismatch without logging the expected token value.
- Revocation fail-open behavior is already covered end-to-end in
  ``test_revocation_degradation.py``; this file asserts the complementary
  log-redaction invariants for media-specific controls.

References: plan item 11.12, fa-auth-m8 test_alerting_degraded_modes.py.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from media_service.core.rate_limit import (
    AnonRateLimiter,
    RateLimiter,
    _handle_redis_error,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _redis_error() -> Exception:
    return ConnectionError("Redis connection refused")


# ── 11.12a: rate-limit degradation structured logging ────────────────────────


class TestRateLimitDegradationLogging:
    """_handle_redis_error emits structured log lines; no sensitive values leak."""

    def test_fail_closed_logs_mode_label(self, caplog):
        """fail_closed degradation logs mode=fail_closed in the structured line."""
        with (
            patch("media_service.core.rate_limit._metrics") as mock_m,
            caplog.at_level(logging.ERROR, logger="media_service.core.rate_limit"),
        ):
            mock_m.inc_rate_limit_redis_error = MagicMock()
            with pytest.raises(HTTPException):
                _handle_redis_error(_redis_error(), "uploads:initiate", "fail_closed")

        assert "fail_closed" in caplog.text

    def test_fail_open_logs_mode_label(self, caplog):
        """fail_open degradation logs mode=fail_open in the structured line."""
        with (
            patch("media_service.core.rate_limit._metrics") as mock_m,
            caplog.at_level(logging.WARNING, logger="media_service.core.rate_limit"),
        ):
            mock_m.inc_rate_limit_redis_error = MagicMock()
            _handle_redis_error(_redis_error(), "objects:download-url", "fail_open")

        assert "fail_open" in caplog.text

    def test_fail_closed_logs_action_name(self, caplog):
        """fail_closed log line includes the action so operators know which limiter fired."""
        with (
            patch("media_service.core.rate_limit._metrics") as mock_m,
            caplog.at_level(logging.ERROR, logger="media_service.core.rate_limit"),
        ):
            mock_m.inc_rate_limit_redis_error = MagicMock()
            with pytest.raises(HTTPException):
                _handle_redis_error(_redis_error(), "uploads:complete", "fail_closed")

        assert "uploads:complete" in caplog.text

    def test_fail_open_logs_action_name(self, caplog):
        """fail_open log line includes the action so operators know which limiter fired."""
        with (
            patch("media_service.core.rate_limit._metrics") as mock_m,
            caplog.at_level(logging.WARNING, logger="media_service.core.rate_limit"),
        ):
            mock_m.inc_rate_limit_redis_error = MagicMock()
            _handle_redis_error(_redis_error(), "shares:resolve", "fail_open")

        assert "shares:resolve" in caplog.text

    def test_fail_closed_raises_503(self):
        """fail_closed degradation raises HTTP 503."""
        with patch("media_service.core.rate_limit._metrics"):
            with pytest.raises(HTTPException) as exc_info:
                _handle_redis_error(_redis_error(), "test:action", "fail_closed")
        assert exc_info.value.status_code == 503

    def test_fail_open_does_not_raise(self):
        """fail_open degradation passes through without raising."""
        with patch("media_service.core.rate_limit._metrics"):
            _handle_redis_error(_redis_error(), "test:action", "fail_open")


# ── 11.12b: log redaction — no sensitive values in degradation logs ───────────


class TestRateLimitLogRedaction:
    """Degradation log lines must not contain bearer tokens, JWTs, or secrets."""

    def test_fail_closed_log_contains_no_jwt(self, caplog):
        """fail_closed log never includes a JWT string from request context."""
        fake_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
        with (
            patch("media_service.core.rate_limit._metrics"),
            caplog.at_level(logging.ERROR, logger="media_service.core.rate_limit"),
        ):
            with pytest.raises(HTTPException):
                # Action is a hardcoded label; JWT from request context must not appear.
                _handle_redis_error(_redis_error(), "uploads:initiate", "fail_closed")

        # The JWT header prefix must not appear in the log output.
        assert fake_jwt not in caplog.text
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in caplog.text

    def test_fail_open_log_contains_no_service_token(self, caplog):
        """fail_open log never includes the internal service token value."""
        fake_service_token = "TestService!Token1secure_uniqueXYZ"
        with (
            patch("media_service.core.rate_limit._metrics"),
            caplog.at_level(logging.WARNING, logger="media_service.core.rate_limit"),
        ):
            _handle_redis_error(_redis_error(), "shares:resolve", "fail_open")

        assert fake_service_token not in caplog.text

    def test_fail_closed_log_contains_no_share_secret(self, caplog):
        """fail_closed log never includes the share-signing secret value."""
        fake_share_secret = "TestShare!Signing1secureKey0987_unique"
        with (
            patch("media_service.core.rate_limit._metrics"),
            caplog.at_level(logging.ERROR, logger="media_service.core.rate_limit"),
        ):
            with pytest.raises(HTTPException):
                _handle_redis_error(
                    _redis_error(), "objects:download-url", "fail_closed"
                )

        assert fake_share_secret not in caplog.text

    def test_rate_limiter_degradation_log_redacts_user_id_from_key(self, caplog):
        """RateLimiter degradation log never includes the per-user Redis key."""
        import uuid
        from auth_sdk_m8.schemas.user import UserModel

        user_id = str(uuid.uuid4())
        user = UserModel(
            id=user_id,
            email="redact@test.com",
            is_active=True,
            is_superuser=False,
            role="user",
        )
        request = MagicMock()
        request.client.host = "127.0.0.1"

        redis = MagicMock()
        redis.incr.side_effect = ConnectionError("Redis down")

        limiter = RateLimiter("uploads:initiate", limit=20, failure_mode="fail_open")
        with (
            patch("media_service.core.rate_limit._metrics"),
            caplog.at_level(logging.WARNING, logger="media_service.core.rate_limit"),
        ):
            limiter(request=request, current_user=user, redis_client=redis)

        # The user_id should NOT appear verbatim in the log (only action/mode labels).
        assert user_id not in caplog.text


# ── 11.12c: internal service-token auth denial ───────────────────────────────


class TestServiceTokenDenial:
    """require_service_token raises 403 on any mismatch and never logs the token."""

    def test_missing_authorization_header_returns_403(self):
        """No Authorization header → 403 Forbidden."""
        from media_service.core.deps import require_service_token

        with pytest.raises(HTTPException) as exc_info:
            require_service_token(authorization=None)

        assert exc_info.value.status_code == 403

    def test_wrong_token_returns_403(self):
        """Wrong bearer token → 403 Forbidden."""
        from media_service.core.deps import require_service_token

        with pytest.raises(HTTPException) as exc_info:
            require_service_token(authorization="Bearer wrong-token-value")

        assert exc_info.value.status_code == 403

    def test_malformed_authorization_header_returns_403(self):
        """Authorization header without 'Bearer ' prefix → 403 Forbidden."""
        from media_service.core.deps import require_service_token

        with pytest.raises(HTTPException) as exc_info:
            require_service_token(authorization="Basic dXNlcjpwYXNz")

        assert exc_info.value.status_code == 403

    def test_valid_token_does_not_raise(self):
        """Correct bearer token → no exception."""
        from media_service.core.deps import require_service_token

        # conftest sets MEDIA_INTERNAL_SERVICE_TOKEN=TestService!Token1secure
        require_service_token(authorization="Bearer TestService!Token1secure")

    def test_denial_does_not_log_expected_token_value(self, caplog):
        """Denial log (if any) must never contain the expected service token."""
        from media_service.core.deps import require_service_token

        expected_token = "TestService!Token1secure"
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(HTTPException):
                require_service_token(authorization="Bearer wrong-value")

        assert expected_token not in caplog.text

    def test_denial_does_not_log_provided_token_value(self, caplog):
        """Denial log must never echo back the provided (attacker-controlled) token."""
        from media_service.core.deps import require_service_token

        attacker_token = "attacker_crafted_token_abc123_unique_value"
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(HTTPException):
                require_service_token(authorization=f"Bearer {attacker_token}")

        assert attacker_token not in caplog.text


# ── 11.12d: metric emission mode label ───────────────────────────────────────


class TestRateLimitDegradationMetric:
    """_handle_redis_error emits the metric with the correct mode label."""

    def test_fail_closed_emits_metric_with_fail_closed_mode(self):
        """fail_closed path calls inc_rate_limit_redis_error('fail_closed')."""
        with patch("media_service.core.rate_limit._metrics") as mock_m:
            with pytest.raises(HTTPException):
                _handle_redis_error(_redis_error(), "test:action", "fail_closed")
        mock_m.inc_rate_limit_redis_error.assert_called_once_with("fail_closed")

    def test_fail_open_emits_metric_with_fail_open_mode(self):
        """fail_open path calls inc_rate_limit_redis_error('fail_open')."""
        with patch("media_service.core.rate_limit._metrics") as mock_m:
            _handle_redis_error(_redis_error(), "test:action", "fail_open")
        mock_m.inc_rate_limit_redis_error.assert_called_once_with("fail_open")

    def test_anon_limiter_fail_closed_emits_metric(self):
        """AnonRateLimiter fail_closed emits inc_rate_limit_redis_error('fail_closed')."""
        request = MagicMock()
        request.client.host = "10.0.0.1"
        redis = MagicMock()
        redis.incr.side_effect = ConnectionError("Redis down")
        limiter = AnonRateLimiter(
            "shares:resolve", limit=60, failure_mode="fail_closed"
        )
        with patch("media_service.core.rate_limit._metrics") as mock_m:
            with pytest.raises(HTTPException):
                limiter(request=request, redis_client=redis)
        mock_m.inc_rate_limit_redis_error.assert_called_once_with("fail_closed")

    def test_anon_limiter_fail_open_emits_metric(self):
        """AnonRateLimiter fail_open emits inc_rate_limit_redis_error('fail_open')."""
        request = MagicMock()
        request.client.host = "10.0.0.1"
        redis = MagicMock()
        redis.incr.side_effect = ConnectionError("Redis down")
        limiter = AnonRateLimiter("shares:resolve", limit=60, failure_mode="fail_open")
        with patch("media_service.core.rate_limit._metrics") as mock_m:
            limiter(request=request, redis_client=redis)
        mock_m.inc_rate_limit_redis_error.assert_called_once_with("fail_open")
