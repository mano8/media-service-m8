"""Outbound webhook delivery — drain the transactional outbox to subscribers.

Sync controller (the only async surface is ``maintenance_worker``).
``deliver_pending`` claims due ``PENDING`` events, POSTs each as an HMAC-signed
:class:`OutboxEventPayload` to every active subscription matching the event type,
and settles each row: ``DELIVERED`` on success, otherwise ``attempts`` is
incremented with exponential backoff until ``OUTBOX_MAX_ATTEMPTS`` marks it
terminally ``FAILED`` (poison-message guard). Delivery is at-least-once;
subscribers dedupe on the event id and verify the ``X-Signature`` header.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlmodel import Session, col, select

from media_sdk_m8 import OutboxEventPayload

from media_service.core.ssrf import WebhookUrlGuard
from media_service.db_models.outbox import OutboxEvent, OutboxStatus, Subscription

_logger = logging.getLogger(__name__)

#: Header carrying ``sha256=<hex HMAC-SHA256(body, subscription.secret)>``.
SIGNATURE_HEADER = "X-Signature"


@dataclass(frozen=True)
class OutboxDeliveryReport:
    """Per-run outcome counts (delivered / retried / terminally failed)."""

    delivered: int
    retried: int
    failed: int


def sign_payload(secret: str, body: bytes) -> str:
    """Return the ``X-Signature`` value for ``body`` under ``secret``."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _claim_due(session: Session, *, now: datetime, limit: int) -> list[OutboxEvent]:
    """Claim up to ``limit`` PENDING events whose ``next_attempt_at`` has passed."""
    return list(
        session.exec(
            select(OutboxEvent)
            .where(
                OutboxEvent.status == OutboxStatus.PENDING,  # type: ignore[arg-type]
                col(OutboxEvent.next_attempt_at) <= now,
            )
            .order_by(col(OutboxEvent.created_at))
            .limit(limit)
        ).all()
    )


def _active_subscriptions(session: Session) -> list[Subscription]:
    """Load every active subscription once per delivery run."""
    return list(
        session.exec(
            select(Subscription).where(col(Subscription.active).is_(True))
        ).all()
    )


def _wants(sub: Subscription, event_type: str) -> bool:
    """Whether ``sub`` should receive ``event_type`` (empty filter = all types)."""
    return not sub.event_types or event_type in sub.event_types


def _serialize(event: OutboxEvent) -> bytes:
    """Render an outbox row as the on-the-wire OutboxEventPayload JSON body."""
    payload = OutboxEventPayload(
        event_id=event.id,
        event_type=event.event_type,
        object_id=event.object_id,
        payload=event.payload,
        created_at=event.created_at,
    )
    return payload.model_dump_json().encode()


def _post_one(
    client: httpx.Client,
    sub: Subscription,
    event: OutboxEvent,
    body: bytes,
    url_guard: WebhookUrlGuard,
) -> bool:
    """POST one signed event to one subscriber; True only on a 2xx response.

    The target URL is re-validated against the SSRF guard immediately before the
    request (host resolved + every IP inspected), so a subscriber that points at
    an internal address is never POSTed to and settles via the retry/backoff path.
    """
    if not url_guard(sub.url):
        _logger.warning(
            "outbox delivery blocked by SSRF guard event=%s url=%s", event.id, sub.url
        )
        return False
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: sign_payload(sub.secret, body),
    }
    try:
        response = client.post(sub.url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        _logger.warning(
            "outbox delivery error event=%s url=%s: %s", event.id, sub.url, exc
        )
        return False
    if 200 <= response.status_code < 300:
        return True
    _logger.warning(
        "outbox delivery non-2xx event=%s url=%s status=%s",
        event.id,
        sub.url,
        response.status_code,
    )
    return False


def _deliver_event(
    client: httpx.Client,
    event: OutboxEvent,
    subs: list[Subscription],
    url_guard: WebhookUrlGuard,
) -> bool:
    """Deliver one event to every matching subscriber; True iff all accepted.

    An event with no matching active subscriber settles as delivered (there is
    nothing to send) so it never lingers PENDING forever.
    """
    targets = [s for s in subs if _wants(s, event.event_type)]
    if not targets:
        return True
    body = _serialize(event)
    return all([_post_one(client, sub, event, body, url_guard) for sub in targets])


class OutboxDeliveryController:
    """Drain the transactional outbox to subscriber URLs (at-least-once)."""

    @staticmethod
    def deliver_pending(
        *,
        session: Session,
        client: httpx.Client,
        now: datetime,
        limit: int,
        max_attempts: int,
        backoff_base_seconds: int,
        url_guard: WebhookUrlGuard,
    ) -> OutboxDeliveryReport:
        """Claim, deliver, and settle due PENDING outbox events in one run."""
        events = _claim_due(session, now=now, limit=limit)
        subs = _active_subscriptions(session)
        delivered = retried = failed = 0
        for event in events:
            if _deliver_event(client, event, subs, url_guard):
                event.status = OutboxStatus.DELIVERED
                event.delivered_at = now
                delivered += 1
            else:
                event.attempts += 1
                if event.attempts >= max_attempts:
                    event.status = OutboxStatus.FAILED
                    failed += 1
                else:
                    delay = backoff_base_seconds * (2 ** (event.attempts - 1))
                    event.next_attempt_at = now + timedelta(seconds=delay)
                    retried += 1
            session.add(event)
        session.commit()
        return OutboxDeliveryReport(delivered=delivered, retried=retried, failed=failed)
