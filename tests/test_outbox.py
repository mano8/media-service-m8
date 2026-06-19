"""Tests for the transactional outbox: enqueue helper, transactional writes at
state changes, and the ``OutboxDeliveryController`` (HMAC signing, retry/backoff,
poison-message cap, subscriber matching)."""

import uuid
from datetime import timedelta

import httpx
import pytest
from sqlmodel import Session, select

from media_sdk_m8 import OutboxEventPayload

from media_service.controllers.objects import ObjectsController
from media_service.controllers.outbox import (
    OutboxDeliveryController,
    OutboxDeliveryReport,
    sign_payload,
)
from media_service.controllers.variants import VariantsController
from media_service.core.outbox import record_event
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
    utcnow,
)
from media_service.db_models.outbox import OutboxEvent, OutboxStatus, Subscription
from media_service.schemas.variants import VariantRegisterRequest

SECRET = "subscriber-signing-secret-0123456789"


# ── fixtures / helpers ───────────────────────────────────────────────────────


def _object(
    session: Session,
    *,
    owner_user_id: uuid.UUID | None = None,
    status: MediaObjectStatus = MediaObjectStatus.UPLOADED,
    visibility: MediaVisibility = MediaVisibility.PRIVATE,
) -> MediaObject:
    obj = MediaObject(
        owner_user_id=owner_user_id or uuid.uuid4(),
        category="document",
        visibility=visibility,
        storage_bucket="private-media",
        object_key=f"k/{uuid.uuid4()}",
        mime_type="application/pdf",
        size_bytes=10,
        status=status,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _events(session: Session) -> list[OutboxEvent]:
    return list(session.exec(select(OutboxEvent)).all())


def _naive(dt):
    """Drop tzinfo so SQLite's naive read-back compares to an aware reference."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _subscription(
    session: Session,
    *,
    event_types: list[str] | None = None,
    active: bool = True,
    url: str = "https://hook.example.com/events",
) -> Subscription:
    sub = Subscription(
        url=url,
        secret=SECRET,
        event_types=event_types if event_types is not None else [],
        active=active,
    )
    session.add(sub)
    session.commit()
    session.refresh(sub)
    return sub


def _pending_event(
    session: Session,
    *,
    event_type: str = "object.ready",
    attempts: int = 0,
    next_attempt_at=None,
) -> OutboxEvent:
    event = OutboxEvent(
        event_type=event_type,
        object_id=uuid.uuid4(),
        payload={"hello": "world"},
        attempts=attempts,
        next_attempt_at=next_attempt_at or utcnow(),
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def _client(handler) -> httpx.Client:
    """An httpx.Client whose requests are served by ``handler`` (no network)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _recorder(calls: list, status: int = 200):
    """A MockTransport handler that records each request and returns ``status``."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status)

    return handler


def _allow_all(_url: str) -> bool:
    """Permissive SSRF guard for delivery tests not exercising the guard itself."""
    return True


def _deliver(
    session: Session,
    client: httpx.Client,
    *,
    now=None,
    limit: int = 50,
    max_attempts: int = 5,
    backoff_base_seconds: int = 30,
    url_guard=_allow_all,
) -> OutboxDeliveryReport:
    return OutboxDeliveryController.deliver_pending(
        session=session,
        client=client,
        now=now or utcnow(),
        limit=limit,
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        url_guard=url_guard,
    )


# ── record_event (enqueue helper) ────────────────────────────────────────────


def test_record_event_stages_without_commit(session: Session):
    event = record_event(
        session, event_type="object.ready", object_id=uuid.uuid4(), payload={"a": 1}
    )
    assert event in session.new
    # A rolled-back transaction drops the staged event — nothing persists.
    session.rollback()
    assert _events(session) == []


def test_record_event_defaults_payload_and_status(session: Session):
    oid = uuid.uuid4()
    record_event(session, event_type="variant.ready", object_id=oid)
    session.commit()
    rows = _events(session)
    assert len(rows) == 1
    assert rows[0].event_type == "variant.ready"
    assert rows[0].object_id == oid
    assert rows[0].payload == {}
    assert rows[0].status == OutboxStatus.PENDING
    assert rows[0].attempts == 0


# ── transactional writes at state changes ────────────────────────────────────


def test_apply_scan_result_clean_writes_object_ready(session: Session):
    obj = _object(session)
    ObjectsController.apply_scan_result(
        session=session, object_id=obj.id, scan_status=ScanStatus.CLEAN
    )
    rows = _events(session)
    assert [r.event_type for r in rows] == ["object.ready"]
    assert rows[0].object_id == obj.id
    assert rows[0].payload["status"] == MediaObjectStatus.READY.value


def test_apply_scan_result_infected_writes_scan_failed(session: Session):
    obj = _object(session)
    ObjectsController.apply_scan_result(
        session=session, object_id=obj.id, scan_status=ScanStatus.INFECTED
    )
    rows = _events(session)
    assert [r.event_type for r in rows] == ["scan.failed"]
    assert rows[0].payload["scan_status"] == ScanStatus.QUARANTINED.value


def test_delete_object_writes_object_deleted(
    session: Session, mock_storage, current_user
):
    obj = _object(session, owner_user_id=uuid.UUID(str(current_user.id)))
    ObjectsController.delete_object(
        session=session,
        current_user=current_user,
        object_id=obj.id,
        storage=mock_storage,
    )
    rows = _events(session)
    assert [r.event_type for r in rows] == ["object.deleted"]
    assert rows[0].payload["visibility"] == MediaVisibility.PRIVATE.value


def test_delete_object_idempotent_does_not_double_emit(
    session: Session, mock_storage, current_user
):
    obj = _object(session, owner_user_id=uuid.UUID(str(current_user.id)))
    for _ in range(2):
        ObjectsController.delete_object(
            session=session,
            current_user=current_user,
            object_id=obj.id,
            storage=mock_storage,
        )
    assert len(_events(session)) == 1


def test_register_variant_writes_variant_ready(session: Session):
    obj = _object(session)
    VariantsController.register_variant(
        session=session,
        object_id=obj.id,
        req=VariantRegisterRequest(
            variant_name="thumb",
            storage_bucket="public-media",
            object_key="k/thumb.webp",
            size_bytes=42,
            format="WEBP",
        ),
    )
    rows = _events(session)
    assert [r.event_type for r in rows] == ["variant.ready"]
    assert rows[0].payload == {"variant_name": "thumb", "format": "WEBP"}


# ── sign_payload ─────────────────────────────────────────────────────────────


def test_sign_payload_is_deterministic_and_prefixed():
    sig = sign_payload(SECRET, b"body")
    assert sig.startswith("sha256=")
    assert sig == sign_payload(SECRET, b"body")
    assert sig != sign_payload("other-secret", b"body")


# ── delivery: happy path + signature verified by a fake subscriber ───────────


def test_deliver_signs_and_marks_delivered(session: Session):
    sub = _subscription(session, event_types=["object.ready"])
    event = _pending_event(session, event_type="object.ready")
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        # The fake subscriber verifies the HMAC over the exact body it received.
        assert request.headers["X-Signature"] == sign_payload(SECRET, request.content)
        payload = OutboxEventPayload.model_validate_json(request.content)
        assert payload.event_id == event.id
        assert payload.event_type == "object.ready"
        return httpx.Response(200)

    report = _deliver(session, _client(handler))

    assert report.delivered == 1
    assert len(received) == 1
    assert str(received[0].url) == sub.url
    session.refresh(event)
    assert event.status == OutboxStatus.DELIVERED
    assert event.delivered_at is not None


def test_deliver_retries_with_backoff_on_non_2xx(session: Session):
    _subscription(session)
    event = _pending_event(session)
    now = utcnow()

    report = _deliver(
        session,
        _client(lambda r: httpx.Response(500)),
        now=now,
        backoff_base_seconds=30,
    )

    assert report.retried == 1
    session.refresh(event)
    assert event.status == OutboxStatus.PENDING
    assert event.attempts == 1
    # First retry waits base * 2**0 = 30s.
    assert _naive(event.next_attempt_at) >= _naive(now) + timedelta(seconds=30)


def test_deliver_retries_on_connection_error(session: Session):
    _subscription(session)
    event = _pending_event(session)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("subscriber down")

    report = _deliver(session, _client(handler))

    assert report.retried == 1
    session.refresh(event)
    assert event.attempts == 1
    assert event.status == OutboxStatus.PENDING


def test_deliver_marks_failed_at_max_attempts(session: Session):
    _subscription(session)
    # One short of the cap; the next failure makes it terminal.
    event = _pending_event(session, attempts=4)

    report = _deliver(session, _client(lambda r: httpx.Response(503)), max_attempts=5)

    assert report.failed == 1
    session.refresh(event)
    assert event.status == OutboxStatus.FAILED
    assert event.attempts == 5


def test_deliver_backoff_grows_with_attempts(session: Session):
    _subscription(session)
    event = _pending_event(session, attempts=2)
    now = utcnow()

    _deliver(
        session,
        _client(lambda r: httpx.Response(500)),
        now=now,
        backoff_base_seconds=10,
    )

    session.refresh(event)
    # attempts becomes 3 → delay = 10 * 2**(3-1) = 40s.
    assert _naive(event.next_attempt_at) >= _naive(now) + timedelta(seconds=40)


# ── delivery: subscriber matching ────────────────────────────────────────────


def test_deliver_no_matching_subscriber_settles_delivered(session: Session):
    _subscription(session, event_types=["object.ready"])
    event = _pending_event(session, event_type="variant.ready")
    calls: list[httpx.Request] = []

    report = _deliver(session, _client(_recorder(calls)))

    assert report.delivered == 1
    assert calls == []  # nothing to deliver — never POSTed
    session.refresh(event)
    assert event.status == OutboxStatus.DELIVERED


def test_deliver_wildcard_subscription_receives_any_event(session: Session):
    _subscription(session, event_types=[])  # empty filter == all event types
    _pending_event(session, event_type="scan.failed")

    report = _deliver(session, _client(lambda r: httpx.Response(204)))

    assert report.delivered == 1


def test_deliver_skips_inactive_subscription(session: Session):
    _subscription(session, active=False)
    event = _pending_event(session)
    calls: list[httpx.Request] = []

    report = _deliver(session, _client(_recorder(calls)))

    assert report.delivered == 1
    assert calls == []
    session.refresh(event)
    assert event.status == OutboxStatus.DELIVERED


def test_deliver_requires_all_subscribers_to_accept(session: Session):
    _subscription(session, url="https://ok.example.com/h")
    _subscription(session, url="https://bad.example.com/h")
    event = _pending_event(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if "ok" in str(request.url) else 500)

    report = _deliver(session, _client(handler))

    assert report.retried == 1
    session.refresh(event)
    assert event.status == OutboxStatus.PENDING


# ── delivery: claiming (status / due / limit) ────────────────────────────────


def test_deliver_skips_future_and_settled_events(session: Session):
    _subscription(session)
    future = _pending_event(session, next_attempt_at=utcnow() + timedelta(hours=1))
    delivered = _pending_event(session)
    delivered.status = OutboxStatus.DELIVERED
    session.add(delivered)
    session.commit()
    calls: list[httpx.Request] = []

    report = _deliver(session, _client(_recorder(calls)))

    assert report.delivered == 0
    assert calls == []
    session.refresh(future)
    assert future.status == OutboxStatus.PENDING


def test_deliver_limit_bounds_the_batch(session: Session):
    _subscription(session)
    _pending_event(session)
    _pending_event(session)

    report = _deliver(session, _client(lambda r: httpx.Response(200)), limit=1)

    assert report.delivered == 1
    remaining = [e for e in _events(session) if e.status == OutboxStatus.PENDING]
    assert len(remaining) == 1


@pytest.mark.parametrize(
    ("event_types", "event_type", "should_match"),
    [
        (["object.ready"], "object.ready", True),
        (["object.ready"], "object.deleted", False),
        ([], "variant.ready", True),
    ],
)
def test_subscription_event_filter_matrix(
    session: Session, event_types, event_type, should_match
):
    _subscription(session, event_types=event_types)
    _pending_event(session, event_type=event_type)
    calls: list[httpx.Request] = []

    _deliver(session, _client(_recorder(calls)))

    assert (len(calls) == 1) is should_match


# ── delivery: SSRF guard (send-time) ─────────────────────────────────────────


def test_deliver_ssrf_blocked_target_is_not_posted(session: Session):
    """A target the guard rejects is never POSTed and settles via retry/backoff."""
    _subscription(session, url="https://blocked.example.com/h")
    event = _pending_event(session)
    calls: list[httpx.Request] = []

    report = _deliver(session, _client(_recorder(calls)), url_guard=lambda _url: False)

    assert calls == []  # blocked before any request left the process
    assert report.retried == 1
    session.refresh(event)
    assert event.status == OutboxStatus.PENDING
    assert event.attempts == 1


def test_deliver_ssrf_guard_consulted_per_target_url(session: Session):
    """The guard sees each subscriber URL; only allowed ones are delivered."""
    _subscription(session, url="https://ok.example.com/h")
    _subscription(session, url="https://blocked.example.com/h")
    _pending_event(session)
    seen: list[str] = []

    def guard(url: str) -> bool:
        seen.append(url)
        return "ok" in url

    report = _deliver(session, _client(lambda r: httpx.Response(200)), url_guard=guard)

    assert set(seen) == {"https://ok.example.com/h", "https://blocked.example.com/h"}
    # One target blocked → event not fully delivered → retried.
    assert report.retried == 1
