"""Transactional-outbox enqueue helper.

``record_event`` stages an :class:`OutboxEvent` on the caller's session **without
committing**, so the caller's existing ``session.commit()`` flushes the event
atomically with the state change it describes — and a rolled-back transaction
drops the event too. This guarantees at-least-once notification: no committed
state change is ever silently un-notified, and no event is ever delivered for a
change that did not commit.

The module is deliberately named ``outbox`` (not ``events``) so it never collides
with ``core/events.py``, which owns the unrelated inbound auth event-stream.
"""

from typing import Any
import uuid

from sqlmodel import Session

from media_service.db_models.outbox import OutboxEvent

# Canonical dotted event-type names carried in the OutboxEventPayload contract.
EVENT_OBJECT_READY = "object.ready"
EVENT_OBJECT_DELETED = "object.deleted"
EVENT_SCAN_FAILED = "scan.failed"
EVENT_VARIANT_READY = "variant.ready"


def record_event(
    session: Session,
    *,
    event_type: str,
    object_id: uuid.UUID,
    payload: dict[str, Any] | None = None,
) -> OutboxEvent:
    """Stage an outbox event on ``session`` (no commit — the caller's txn owns it).

    The event lands ``PENDING`` and immediately due; the maintenance worker's
    ``deliver_outbox`` job claims and delivers it. Returns the staged row so a
    caller can assert on it in tests.
    """
    event = OutboxEvent(
        event_type=event_type,
        object_id=object_id,
        payload=payload or {},
    )
    session.add(event)
    return event
